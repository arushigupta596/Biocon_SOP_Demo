"""
SOP Gap Detector — parses SOP clauses, retrieves regulatory context via RAG,
and calls Claude via OpenRouter to identify compliance gaps.

Usage:
    python -m src.gap_engine.detector --sop sops/BC-MFG-UC-047.docx
    python -m src.gap_engine.detector --all --sops sops/ [--workers 4]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import docx
import fitz  # PyMuPDF
from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings
from openai import OpenAI
from pydantic import ValidationError

from src.schemas import GapRegistry, GapResult, SOPScanResult

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

COLLECTION_NAME = "regulatory_corpus"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "anthropic/claude-sonnet-4-5"
TOP_K = 8
MIN_RELEVANCE_SCORE = 0.30

# ---------------------------------------------------------------------------
# System prompt — enforces JSON-only output
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a regulatory compliance specialist for pharmaceutical biologics manufacturing.
Your task: analyse a single SOP clause against provided regulatory context, identify
compliance gaps, and return ONLY a valid JSON array of gap findings.

SEVERITY DEFINITIONS:
- CRITICAL: Missing required element that will cause regulatory non-conformance at FDA/EMA inspection
- MAJOR: Deficient element that is likely to be cited at inspection
- MINOR: Improvement recommended; low regulatory risk; not typically cited

OUTPUT CONTRACT — you MUST return ONLY a raw JSON array, with no markdown, no prose, no code fences.
Schema for each element in the array:
{
  "sop_clause":         "<section reference, e.g. §7.3>",
  "sop_clause_text":    "<verbatim excerpt of the SOP clause text that is deficient>",
  "regulation_ref":     "<citation, e.g. EMA CHMP/437/04 Rev1 §5.2.3>",
  "regulation_excerpt": "<verbatim excerpt from the regulatory text establishing the requirement>",
  "gap_description":    "<plain-English description of the compliance gap>",
  "severity":           "<CRITICAL | MAJOR | MINOR>",
  "remediation":        "<actionable steps to close the gap>",
  "confidence":         <float 0.0-1.0>
}

If no gaps are found for this clause, return an empty array: []
Do NOT invent gaps. Do NOT cite regulations not present in the provided context.
Confidence should be ~0.90 if the regulation explicitly states the requirement,
~0.70 if the regulation implies it, ~0.60 if it is a reasonable interpretation.
"""

USER_PROMPT_TEMPLATE = """\
SOP ID: {sop_id}
SOP CLAUSE: {section_id} — {heading}

--- SOP CLAUSE TEXT ---
{clause_body}

--- REGULATORY CONTEXT (retrieved from corpus) ---
{regulatory_context}

Identify ALL compliance gaps in this SOP clause relative to the regulatory requirements above.
Return ONLY the JSON array of gap findings as specified. No other text.
"""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SOPClause:
    section_id: str
    heading: str
    body: str


# ---------------------------------------------------------------------------
# Main detector class
# ---------------------------------------------------------------------------

class SOPGapDetector:
    def __init__(
        self,
        chroma_path: Path,
        collection_name: str = COLLECTION_NAME,
        top_k: int = TOP_K,
    ) -> None:
        self.top_k = top_k
        embeddings = OpenAIEmbeddings(
            model="openai/text-embedding-3-large",
            openai_api_key=os.environ["OPENROUTER_API_KEY"],
            openai_api_base="https://openrouter.ai/api/v1",
        )
        self.vectorstore = Chroma(
            collection_name=collection_name,
            embedding_function=embeddings,
            persist_directory=str(chroma_path),
        )
        self.client = OpenAI(
            api_key=os.environ["OPENROUTER_API_KEY"],
            base_url=OPENROUTER_BASE_URL,
        )

    # ------------------------------------------------------------------
    # Public: scan a single SOP
    # ------------------------------------------------------------------

    def scan_sop(self, sop_path: Path) -> SOPScanResult:
        sop_id = self._extract_sop_id(sop_path)
        logger.info("Scanning %s (%s) …", sop_path.name, sop_id)

        if sop_path.suffix.lower() == ".pdf":
            clauses = self._parse_sop_pdf(sop_path)
        else:
            clauses = self._parse_sop(sop_path)
        logger.info("  %d clauses parsed", len(clauses))

        all_findings: list[GapResult] = []
        for clause in clauses:
            reg_chunks = self._retrieve_regulatory_context(clause)
            if not reg_chunks:
                continue
            findings = self._detect_gaps(sop_id, clause, reg_chunks)
            all_findings.extend(findings)
            if findings:
                logger.info(
                    "  %s %s — %d gap(s) found", sop_id, clause.section_id, len(findings)
                )

        result = SOPScanResult(
            sop_id=sop_id,
            sop_file=sop_path.name,
            scan_timestamp=datetime.now(timezone.utc).isoformat(),
            total_clauses_scanned=len(clauses),
            gaps_found=len(all_findings),
            findings=all_findings,
        )

        # Write per-SOP output
        out_dir = Path("output")
        out_dir.mkdir(exist_ok=True)
        per_sop_path = out_dir / f"gap_registry_{sop_id}.json"
        per_sop_path.write_text(result.model_dump_json(indent=2))
        logger.info("  Written: %s (%d gaps)", per_sop_path, len(all_findings))

        return result

    # ------------------------------------------------------------------
    # Public: scan all SOPs in a directory
    # ------------------------------------------------------------------

    def scan_all(self, sops_dir: Path, workers: int = 4) -> GapRegistry:
        sop_files = sorted(
            [*sops_dir.glob("*.docx"), *sops_dir.glob("*.pdf")]
        )
        if not sop_files:
            raise FileNotFoundError(f"No .docx or .pdf files found in {sops_dir}")
        logger.info("Found %d SOP files in %s", len(sop_files), sops_dir)

        results: list[SOPScanResult] = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(self.scan_sop, f): f for f in sop_files}
            for future in as_completed(futures):
                path = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    logger.error("Scan failed for %s: %s", path.name, exc)

        registry = GapRegistry(
            registry_timestamp=datetime.now(timezone.utc).isoformat(),
            total_sops_scanned=len(results),
            total_gaps_found=sum(r.gaps_found for r in results),
            scans=results,
        )

        out_path = Path("output") / "gap_registry.json"
        out_path.parent.mkdir(exist_ok=True)
        out_path.write_text(registry.model_dump_json(indent=2))
        logger.info(
            "Master registry written to %s (%d gaps across %d SOPs)",
            out_path, registry.total_gaps_found, registry.total_sops_scanned,
        )
        return registry

    # ------------------------------------------------------------------
    # SOP parsing
    # ------------------------------------------------------------------

    HEADING_STYLES = frozenset({
        "Heading 1", "Heading 2", "Heading 3",
        "heading 1", "heading 2", "heading 3",
    })
    SECTION_RE = re.compile(r'^(\d+(?:\.\d+)*\.?)\s+(.+)$')

    def _parse_sop(self, docx_path: Path) -> list[SOPClause]:
        doc = docx.Document(str(docx_path))
        clauses: list[SOPClause] = []
        current_section: Optional[str] = None
        current_heading: Optional[str] = None
        body_paras: list[str] = []

        def flush() -> None:
            if current_heading and body_paras:
                clauses.append(SOPClause(
                    section_id=current_section or "§?",
                    heading=current_heading,
                    body=" ".join(body_paras),
                ))

        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue

            style_name = para.style.name if para.style else ""
            is_heading = (
                style_name in self.HEADING_STYLES
                or bool(self.SECTION_RE.match(text))
            )

            if is_heading:
                flush()
                m = self.SECTION_RE.match(text)
                if m:
                    num = m.group(1).rstrip(".")
                    current_section = f"§{num}"
                    current_heading = m.group(2).strip()
                else:
                    current_section = "§?"
                    current_heading = text
                body_paras = []
            else:
                body_paras.append(text)

        flush()
        return clauses

    def _parse_sop_pdf(self, pdf_path: Path) -> list[SOPClause]:
        """Parse a PDF SOP into clauses using regex section detection."""
        doc = fitz.open(str(pdf_path))
        lines: list[str] = []
        for page in doc:
            for line in page.get_text().splitlines():
                line = line.strip()
                if line:
                    lines.append(line)

        clauses: list[SOPClause] = []
        current_section: Optional[str] = None
        current_heading: Optional[str] = None
        body_lines: list[str] = []

        def flush() -> None:
            if current_heading and body_lines:
                clauses.append(SOPClause(
                    section_id=current_section or "§?",
                    heading=current_heading,
                    body=" ".join(body_lines),
                ))

        for line in lines:
            m = self.SECTION_RE.match(line)
            if m:
                flush()
                num = m.group(1).rstrip(".")
                current_section = f"§{num}"
                current_heading = m.group(2).strip()
                body_lines = []
            else:
                body_lines.append(line)

        flush()
        return clauses

    # ------------------------------------------------------------------
    # RAG retrieval
    # ------------------------------------------------------------------

    def _retrieve_regulatory_context(self, clause: SOPClause) -> list[dict]:
        query = f"{clause.heading}: {clause.body[:400]}"
        try:
            results = self.vectorstore.similarity_search_with_relevance_scores(
                query=query, k=self.top_k
            )
        except Exception as exc:
            logger.warning("Retrieval failed for clause %s: %s", clause.section_id, exc)
            return []

        chunks = []
        for doc_obj, score in results:
            if score < MIN_RELEVANCE_SCORE:
                continue
            chunks.append({
                "text": doc_obj.page_content,
                "source_file": doc_obj.metadata.get("source_file", ""),
                "regulation_ref": doc_obj.metadata.get("regulation_ref", ""),
                "score": score,
            })
        return chunks

    # ------------------------------------------------------------------
    # Claude gap detection via OpenRouter
    # ------------------------------------------------------------------

    def _detect_gaps(
        self,
        sop_id: str,
        clause: SOPClause,
        reg_chunks: list[dict],
    ) -> list[GapResult]:
        if not reg_chunks:
            return []

        reg_context = "\n\n".join(
            f"[SOURCE {i + 1}] {c['regulation_ref']} (relevance: {c['score']:.2f})\n{c['text']}"
            for i, c in enumerate(reg_chunks)
        )

        user_msg = USER_PROMPT_TEMPLATE.format(
            sop_id=sop_id,
            section_id=clause.section_id,
            heading=clause.heading,
            clause_body=clause.body[:2000],
            regulatory_context=reg_context,
        )

        try:
            response = self.client.chat.completions.create(
                model=MODEL,
                max_tokens=4096,
                temperature=0,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
            )
        except Exception as exc:
            logger.error(
                "Claude API call failed for %s %s: %s",
                sop_id, clause.section_id, exc,
            )
            return []

        raw = response.choices[0].message.content.strip()

        # Strip accidental markdown fences
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.error(
                "JSON parse failed for %s %s. Raw output: %s",
                sop_id, clause.section_id, raw[:300],
            )
            return []

        if not isinstance(data, list):
            logger.error(
                "Expected JSON array for %s %s, got %s",
                sop_id, clause.section_id, type(data).__name__,
            )
            return []

        findings: list[GapResult] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            item["sop_id"] = sop_id  # inject sop_id (not part of Claude's output)
            try:
                findings.append(GapResult.model_validate(item))
            except ValidationError as exc:
                logger.warning(
                    "Schema validation failed for %s %s: %s",
                    sop_id, clause.section_id, exc,
                )
        return findings

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_sop_id(path: Path) -> str:
        """Extract SOP ID from filename, e.g. BC-MFG-UC-047 from BC-MFG-UC-047_..."""
        stem = path.stem
        parts = stem.split("_")
        # SOP IDs match the pattern XX-XXX-XX-000
        for i, part in enumerate(parts):
            if re.match(r'^[A-Z]{2}-[A-Z]{2,3}-[A-Z]{2}-\d+$', part):
                return part
        return parts[0] if parts else stem


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detect regulatory compliance gaps in SOP files"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sop", type=Path, help="Path to a single SOP .docx file")
    group.add_argument("--all", action="store_true", help="Scan all SOPs in --sops directory")
    parser.add_argument(
        "--sops",
        type=Path,
        default=Path("sops"),
        help="Directory containing SOP .docx files (used with --all)",
    )
    parser.add_argument(
        "--chroma-path",
        type=Path,
        default=Path(os.environ.get("CHROMA_PATH", "./chroma_db")),
    )
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    detector = SOPGapDetector(chroma_path=args.chroma_path)

    if args.all:
        registry = detector.scan_all(args.sops, workers=args.workers)
        print(
            f"\nScan complete — {registry.total_gaps_found} gaps found across "
            f"{registry.total_sops_scanned} SOPs."
        )
    else:
        result = detector.scan_sop(args.sop)
        print(f"\nScan complete — {result.gaps_found} gaps found in {result.sop_id}.")


if __name__ == "__main__":
    main()
