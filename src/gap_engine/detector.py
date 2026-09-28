"""
SOP Gap Detector — parses SOP clauses, retrieves regulatory context via RAG,
and calls Claude via OpenRouter to identify compliance gaps.

Usage:
    python -m src.gap_engine.detector --sop sops/BC-MFG-UC-047.docx
    python -m src.gap_engine.detector --all --sops sops/ [--workers 4]

GUARDRAIL CONTRACT
------------------
A clause never silently becomes "compliant". Every failure path produces a
typed UnscannedClause record instead of an empty finding list, so the absence
of a finding means one of exactly two things: the model assessed the clause and
found nothing, or the clause is listed as UNSCANNED.

Exit codes:
    0  scan completed, including a partially degraded one (app.py discards all
       results on a non-zero code, so degradation must not be signalled here)
    2  nothing at all could be analysed, or a fatal ScanAbort
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings
from openai import OpenAI
from pydantic import ValidationError

from src.gap_engine import guardrails as gr
from src.gap_engine.budget import BudgetExceeded, ScanAbort, ScanBudget
from src.gap_engine.parsing import ParseError, SOPClause, parse_clauses
from src.gap_engine.prompts import (
    CLAUSE_CHAR_LIMIT,
    COLLECTION_NAME,
    EMBEDDING_MODEL,
    HEADING_CHAR_LIMIT,
    MAX_TOKENS,
    MAX_TOKENS_RETRY,
    MIN_RELEVANCE_SCORE,
    MODEL,
    OPENROUTER_BASE_URL,
    REPAIR_NUDGE,
    RETRIEVAL_QUERY_CHARS,
    SECTION_ID_CHAR_LIMIT,
    SYSTEM_PROMPT,
    TOP_K,
    USER_PROMPT_TEMPLATE,
)
from src.schemas import (
    GUARDRAILS_VERSION,
    GapRegistry,
    GapResult,
    SOPScanResult,
    UnscannedClause,
    UnscannedReason,
    VerificationFlag,
    VerificationStatus,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
SOP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


# ---------------------------------------------------------------------------
# Per-clause outcome
# ---------------------------------------------------------------------------

ClauseStatus = Literal["ANALYSED", "NO_GAPS", "UNSCANNED", "NO_CONTEXT", "SKIPPED_CAP"]


@dataclass
class ClauseOutcome:
    section_id: str
    heading: str
    status: ClauseStatus
    findings: list[GapResult] = field(default_factory=list)
    reason: Optional[UnscannedReason] = None
    detail: str = ""
    attempts: int = 0

    @property
    def is_unscanned(self) -> bool:
        return self.status in ("UNSCANNED", "NO_CONTEXT", "SKIPPED_CAP")


@dataclass
class LLMOutcome:
    items: Optional[list]
    reason: Optional[UnscannedReason]
    detail: str = ""
    attempts: int = 0


# ---------------------------------------------------------------------------
# Main detector class
# ---------------------------------------------------------------------------

class SOPGapDetector:
    def __init__(
        self,
        chroma_path: Path,
        collection_name: str = COLLECTION_NAME,
        top_k: int = TOP_K,
        budget: Optional[ScanBudget] = None,
        enable_guardrails: bool = True,
    ) -> None:
        self.top_k = top_k
        self.budget = budget or ScanBudget()
        self.enable_guardrails = enable_guardrails

        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise ScanAbort(
                "OPENROUTER_API_KEY is not set. The scanner cannot start."
            )

        embeddings = OpenAIEmbeddings(
            model=EMBEDDING_MODEL,
            openai_api_key=api_key,
            openai_api_base=OPENROUTER_BASE_URL,
            # The embedding call was previously as unbounded as the completion
            # call, and equally capable of hanging a live demo.
            timeout=self.budget.per_call_timeout_s,
            max_retries=2,
        )
        self.vectorstore = Chroma(
            collection_name=collection_name,
            embedding_function=embeddings,
            persist_directory=str(chroma_path),
        )
        self.client = OpenAI(
            api_key=api_key,
            base_url=OPENROUTER_BASE_URL,
            timeout=self.budget.per_call_timeout_s,
            # Must be 0: the SDK's own retries would multiply against ours and
            # blow through the scan deadline.
            max_retries=0,
        )

    # ------------------------------------------------------------------
    # Public: scan a single SOP
    # ------------------------------------------------------------------

    def scan_sop(
        self,
        sop_path: Path,
        out_dir: Path = Path("output"),
        max_clauses: int = 0,
    ) -> SOPScanResult:
        sop_id = self._extract_sop_id(sop_path)
        logger.info("Scanning %s (%s) …", sop_path.name, sop_id)

        self._check_file_limits(sop_path)

        try:
            clauses = parse_clauses(sop_path)
        except ParseError as exc:
            raise ScanAbort(str(exc)) from exc
        logger.info("  %d clauses parsed", len(clauses))

        if not clauses:
            raise ScanAbort(
                f"No numbered clauses found in {sop_path.name}. The engine needs "
                f"sections such as '7.3 Comparability'."
            )

        cap = max_clauses or self.budget.max_clauses_per_sop
        analysed_clauses, capped_clauses = clauses[:cap], clauses[cap:]
        if capped_clauses:
            logger.warning(
                "Clause cap reached: analysing %d of %d clauses; %d marked UNSCANNED",
                len(analysed_clauses), len(clauses), len(capped_clauses),
            )

        outcomes: list[ClauseOutcome] = []
        budget_stop: Optional[BudgetExceeded] = None

        for clause in analysed_clauses:
            if budget_stop is None:
                try:
                    self.budget.check_deadline()
                except BudgetExceeded as exc:
                    budget_stop = exc
                    logger.error(
                        "Budget exceeded (%s); remaining clauses marked UNSCANNED. %s",
                        exc.reason.value, self.budget.snapshot(),
                    )
            if budget_stop is not None:
                outcomes.append(ClauseOutcome(
                    clause.section_id, clause.heading, "SKIPPED_CAP",
                    reason=budget_stop.reason, detail=budget_stop.detail,
                ))
                continue

            try:
                outcomes.append(self._analyse_clause(sop_id, clause))
            except BudgetExceeded as exc:
                budget_stop = exc
                logger.error(
                    "Budget exceeded (%s); remaining clauses marked UNSCANNED. %s",
                    exc.reason.value, self.budget.snapshot(),
                )
                outcomes.append(ClauseOutcome(
                    clause.section_id, clause.heading, "SKIPPED_CAP",
                    reason=exc.reason, detail=exc.detail,
                ))

        for clause in capped_clauses:
            outcomes.append(ClauseOutcome(
                clause.section_id, clause.heading, "SKIPPED_CAP",
                reason=UnscannedReason.CLAUSE_CAP,
                detail=f"clause cap of {cap} reached",
            ))

        result = self._build_result(sop_id, sop_path, clauses, outcomes)
        self._write_result(result, out_dir, sop_id)

        logger.info(
            "  %s — %d/%d clauses analysed, %d UNSCANNED%s, %d gaps (%d flagged)",
            sop_id, result.analysed, result.total_clauses_scanned,
            result.unscanned_count,
            f" ({self._fmt_counts(result.error_counts)})" if result.error_counts else "",
            result.gaps_found, result.flagged_findings_count,
        )
        return result

    # ------------------------------------------------------------------
    # Public: scan all SOPs in a directory
    # ------------------------------------------------------------------

    def scan_all(
        self,
        sops_dir: Path,
        workers: int = 4,
        out_dir: Path = Path("output"),
        max_clauses: int = 0,
    ) -> GapRegistry:
        sop_files = sorted([*sops_dir.glob("*.docx"), *sops_dir.glob("*.pdf")])
        if not sop_files:
            raise ScanAbort(f"No .docx or .pdf files found in {sops_dir}")
        logger.info("Found %d SOP files in %s", len(sop_files), sops_dir)

        results: list[SOPScanResult] = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self.scan_sop, f, out_dir, max_clauses): f
                for f in sop_files
            }
            for future in as_completed(futures):
                path = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    logger.error("Scan failed for %s: %s", path.name, exc)

        registry = self._build_registry(results)
        out_path = out_dir / "gap_registry.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(registry.model_dump_json(indent=2))
        logger.info(
            "Master registry written to %s (%d gaps across %d SOPs, "
            "%d unscanned clause(s), %d finding(s) need verification)",
            out_path, registry.total_gaps_found, registry.total_sops_scanned,
            registry.total_unscanned_clauses, registry.total_flagged_findings,
        )
        return registry

    # ------------------------------------------------------------------
    # Per-clause analysis
    # ------------------------------------------------------------------

    def _analyse_clause(self, sop_id: str, clause: SOPClause) -> ClauseOutcome:
        # --- sanitise untrusted document text before it touches the prompt --
        if self.enable_guardrails:
            body, hits = gr.sanitise_untrusted(clause.body)
            heading, h_hits = gr.sanitise_untrusted(clause.heading)
            section_id, s_hits = gr.sanitise_untrusted(clause.section_id)
            hits += h_hits + s_hits
        else:
            body, heading, section_id, hits = clause.body, clause.heading, clause.section_id, 0

        clause_flags: list[str] = []
        if hits:
            clause_flags.append(VerificationFlag.INJECTION_SUSPECTED.value)
            logger.warning(
                "  %s %s — %d injection-shaped pattern(s) removed from clause text",
                sop_id, clause.section_id, hits,
            )
        if len(body) > CLAUSE_CHAR_LIMIT:
            clause_flags.append(VerificationFlag.CLAUSE_TRUNCATED.value)
            logger.warning(
                "  %s %s — clause is %d chars, analysing first %d",
                sop_id, clause.section_id, len(body), CLAUSE_CHAR_LIMIT,
            )

        # --- retrieval ------------------------------------------------------
        try:
            reg_chunks = self._retrieve_regulatory_context(heading, body)
        except Exception as exc:
            logger.error("  Retrieval failed for %s: %s", clause.section_id, exc)
            return ClauseOutcome(
                clause.section_id, clause.heading, "UNSCANNED",
                reason=UnscannedReason.RETRIEVAL_FAILED, detail=str(exc)[:200],
            )

        if not reg_chunks:
            # Neither compliance nor an error: no regulatory basis was retrieved.
            return ClauseOutcome(
                clause.section_id, clause.heading, "NO_CONTEXT",
                reason=UnscannedReason.NO_REGULATORY_CONTEXT,
                detail=f"no chunk scored >= {MIN_RELEVANCE_SCORE}",
            )

        # --- prompt ---------------------------------------------------------
        reg_context = "\n\n".join(
            f"[SOURCE {i + 1}] {c['regulation_ref']} (relevance: {c['score']:.2f})\n{c['text']}"
            for i, c in enumerate(reg_chunks)
        )
        user_msg = USER_PROMPT_TEMPLATE.format(
            sop_id=sop_id,
            section_id=section_id[:SECTION_ID_CHAR_LIMIT],
            heading=heading[:HEADING_CHAR_LIMIT],
            clause_body=body[:CLAUSE_CHAR_LIMIT],
            regulatory_context=reg_context,
        )

        llm = self._call_llm(sop_id, clause.section_id, user_msg)
        if llm.items is None:
            return ClauseOutcome(
                clause.section_id, clause.heading, "UNSCANNED",
                reason=llm.reason or UnscannedReason.API_ERROR,
                detail=llm.detail, attempts=llm.attempts,
            )

        # --- validate + verify ---------------------------------------------
        findings: list[GapResult] = []
        invalid = 0
        for item in llm.items:
            if not isinstance(item, dict):
                invalid += 1
                continue
            coerced, coerce_flags = (
                gr.coerce_item(item) if self.enable_guardrails else (dict(item), [])
            )
            coerced["sop_id"] = sop_id
            try:
                finding = GapResult.model_validate(coerced)
            except ValidationError as exc:
                invalid += 1
                logger.warning(
                    "  Schema validation failed for %s %s: %s",
                    sop_id, clause.section_id, str(exc)[:200],
                )
                continue

            if self.enable_guardrails:
                v = gr.verify_finding(
                    finding, clause.body, clause.section_id, reg_chunks,
                    extra_flags=clause_flags + coerce_flags,
                )
                finding.verification_status = v.status
                finding.verification_flags = v.flags
                finding.grounding_score = v.grounding_score
                finding.grounded_source_file = v.grounded_source_file
            findings.append(finding)

        # Only a total validation wipe demotes the clause. A partly-good clause
        # must not be misreported as unassessed.
        if invalid and not findings and llm.items:
            return ClauseOutcome(
                clause.section_id, clause.heading, "UNSCANNED",
                reason=UnscannedReason.SCHEMA_INVALID,
                detail=f"{invalid} item(s) failed schema validation",
                attempts=llm.attempts,
            )

        if findings:
            flagged = sum(1 for f in findings if f.is_flagged)
            logger.info(
                "  %s %s — %d gap(s) found%s",
                sop_id, clause.section_id, len(findings),
                f", {flagged} need verification" if flagged else "",
            )
            return ClauseOutcome(
                clause.section_id, clause.heading, "ANALYSED",
                findings=findings, attempts=llm.attempts,
            )

        return ClauseOutcome(
            clause.section_id, clause.heading, "NO_GAPS", attempts=llm.attempts,
        )

    # ------------------------------------------------------------------
    # LLM call with retry, truncation and refusal handling
    # ------------------------------------------------------------------

    def _call_llm(self, sop_id: str, section_id: str, user_msg: str) -> LLMOutcome:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]
        max_tokens = MAX_TOKENS
        last_reason = UnscannedReason.API_ERROR
        last_detail = ""
        attempt = 0

        while attempt < MAX_ATTEMPTS:
            attempt += 1
            self.budget.check_deadline()
            self.budget.reserve_call()

            try:
                response = self.client.chat.completions.create(
                    model=MODEL,
                    max_tokens=max_tokens,
                    temperature=0,          # CLAUDE.md rule 5: deterministic
                    messages=messages,
                    timeout=self.budget.per_call_timeout_s,
                )
            except Exception as exc:
                kind = type(exc).__name__
                # A bad or expired key is the highest-probability demo failure,
                # and previously produced "successful scan, 0 gaps". Be loud.
                if kind in ("AuthenticationError", "PermissionDeniedError"):
                    raise ScanAbort(
                        f"OpenRouter rejected the API key ({kind}). "
                        f"Check OPENROUTER_API_KEY."
                    ) from exc
                if kind == "BadRequestError":
                    return LLMOutcome(None, UnscannedReason.API_ERROR,
                                      str(exc)[:200], attempt)

                last_reason = self._classify_error(kind, exc)
                last_detail = f"{kind}: {exc}"[:200]
                logger.warning(
                    "  %s %s — attempt %d/%d failed (%s)",
                    sop_id, section_id, attempt, MAX_ATTEMPTS, kind,
                )
                if attempt < MAX_ATTEMPTS and self._backoff(attempt, exc):
                    continue
                return LLMOutcome(None, last_reason, last_detail, attempt)

            choices = getattr(response, "choices", None)
            if not choices:
                last_reason, last_detail = UnscannedReason.API_ERROR, "no choices returned"
                if attempt < MAX_ATTEMPTS and self._backoff(attempt, None):
                    continue
                return LLMOutcome(None, last_reason, last_detail, attempt)

            choice = choices[0]
            finish = getattr(choice, "finish_reason", None)

            # Truncated JSON must never be parsed — a half-object can validate
            # into a finding that misquotes the regulation.
            if finish in ("length", "max_tokens"):
                if max_tokens < MAX_TOKENS_RETRY and attempt < MAX_ATTEMPTS:
                    logger.warning(
                        "  %s %s — response truncated, retrying at %d tokens",
                        sop_id, section_id, MAX_TOKENS_RETRY,
                    )
                    max_tokens = MAX_TOKENS_RETRY
                    continue
                return LLMOutcome(None, UnscannedReason.TRUNCATED_RESPONSE,
                                  f"finish_reason={finish}", attempt)

            # Checked BEFORE .strip(): content is None on a refusal, and the
            # resulting AttributeError used to abort the whole SOP scan.
            content = getattr(getattr(choice, "message", None), "content", None)
            if content is None or not str(content).strip():
                if finish == "content_filter":
                    return LLMOutcome(None, UnscannedReason.MODEL_REFUSAL,
                                      "content filtered", attempt)
                last_reason, last_detail = UnscannedReason.MODEL_REFUSAL, "empty content"
                if attempt < MAX_ATTEMPTS and self._backoff(attempt, None):
                    continue
                return LLMOutcome(None, last_reason, last_detail, attempt)

            items, how = gr.extract_json_array(str(content))
            if items is not None:
                return LLMOutcome(items, None, how, attempt)

            logger.warning(
                "  %s %s — reply was not a JSON array (%s): %s",
                sop_id, section_id, how, str(content)[:200],
            )
            if attempt < MAX_ATTEMPTS:
                messages = messages + [
                    {"role": "assistant", "content": str(content)[:2000]},
                    {"role": "user", "content": REPAIR_NUDGE},
                ]
                continue
            return LLMOutcome(None, UnscannedReason.UNPARSEABLE_RESPONSE,
                              str(content)[:200], attempt)

        return LLMOutcome(None, last_reason, last_detail, attempt)

    @staticmethod
    def _classify_error(kind: str, exc: Exception) -> UnscannedReason:
        if "RateLimit" in kind:
            return UnscannedReason.RATE_LIMITED
        if "Timeout" in kind:
            return UnscannedReason.TIMEOUT
        status = getattr(exc, "status_code", None)
        if status == 429:
            return UnscannedReason.RATE_LIMITED
        if status == 408:
            return UnscannedReason.TIMEOUT
        return UnscannedReason.API_ERROR

    def _backoff(self, attempt: int, exc: Optional[Exception]) -> bool:
        """Sleep before the next attempt. False means the deadline forbids it."""
        delay = min(2 * (2 ** (attempt - 1)), 20) + random.uniform(0, 0.5)

        retry_after = None
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers:
            try:
                retry_after = float(headers.get("Retry-After") or headers.get("retry-after"))
            except (TypeError, ValueError):
                retry_after = None
        if retry_after:
            delay = max(delay, min(retry_after, 30.0))

        if delay >= self.budget.remaining_seconds():
            return False
        time.sleep(delay)
        return True

    # ------------------------------------------------------------------
    # RAG retrieval
    # ------------------------------------------------------------------

    def _retrieve_regulatory_context(self, heading: str, body: str) -> list[dict]:
        query = f"{heading}: {body[:RETRIEVAL_QUERY_CHARS]}"
        results = self.vectorstore.similarity_search_with_relevance_scores(
            query=query, k=self.top_k
        )
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
    # Aggregation
    # ------------------------------------------------------------------

    def _build_result(
        self,
        sop_id: str,
        sop_path: Path,
        clauses: list[SOPClause],
        outcomes: list[ClauseOutcome],
    ) -> SOPScanResult:
        findings = [f for o in outcomes for f in o.findings]
        unscanned = [
            UnscannedClause(
                section_id=o.section_id, heading=o.heading,
                reason=o.reason or UnscannedReason.API_ERROR,
                detail=o.detail, attempts=o.attempts,
            )
            for o in outcomes if o.is_unscanned
        ]
        counts = Counter(u.reason.value for u in unscanned)
        injection = [
            o.section_id for o in outcomes
            if any(VerificationFlag.INJECTION_SUSPECTED.value in f.verification_flags
                   for f in o.findings)
        ]

        return SOPScanResult(
            sop_id=sop_id,
            sop_file=sop_path.name,
            scan_timestamp=datetime.now(timezone.utc).isoformat(),
            total_clauses_scanned=len(clauses),
            gaps_found=len(findings),
            findings=findings,
            clauses_analysed=sum(1 for o in outcomes if o.status in ("ANALYSED", "NO_GAPS")),
            unscanned_clauses=unscanned,
            unscanned_count=len(unscanned),
            flagged_findings_count=sum(1 for f in findings if f.is_flagged),
            injection_suspected_clauses=injection,
            error_counts=dict(counts),
            llm_calls=self.budget.calls_made,
            model=MODEL,
            guardrails_version=GUARDRAILS_VERSION if self.enable_guardrails else None,
        )

    @staticmethod
    def _build_registry(results: list[SOPScanResult]) -> GapRegistry:
        findings = [f for r in results for f in r.findings]
        versions = {r.guardrails_version for r in results}
        return GapRegistry(
            registry_timestamp=datetime.now(timezone.utc).isoformat(),
            total_sops_scanned=len(results),
            total_gaps_found=sum(r.gaps_found for r in results),
            scans=results,
            total_unscanned_clauses=sum(r.unscanned_count for r in results),
            total_flagged_findings=sum(1 for f in findings if f.is_flagged),
            total_verified_findings=sum(1 for f in findings if not f.is_flagged),
            guardrails_version=(versions.pop() if len(versions) == 1 else GUARDRAILS_VERSION),
        )

    @staticmethod
    def _fmt_counts(counts: dict[str, int]) -> str:
        return ", ".join(f"{k}x{v}" for k, v in sorted(counts.items()))

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _write_result(self, result: SOPScanResult, out_dir: Path, sop_id: str) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        per_sop_path = out_dir / f"gap_registry_{sop_id}.json"
        # sop_id derives from an uploaded filename, so containment is checked
        # rather than assumed.
        if not per_sop_path.resolve().is_relative_to(out_dir.resolve()):
            raise ScanAbort(f"refusing to write outside {out_dir}: {per_sop_path}")
        per_sop_path.write_text(result.model_dump_json(indent=2))
        logger.info("  Written: %s (%d gaps)", per_sop_path, result.gaps_found)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _check_file_limits(self, sop_path: Path) -> None:
        if not sop_path.exists():
            raise ScanAbort(f"File not found: {sop_path}")
        size = sop_path.stat().st_size
        if size > self.budget.max_sop_bytes:
            raise ScanAbort(
                f"{sop_path.name} is {size / 1024 / 1024:.1f} MB, over the "
                f"{self.budget.max_sop_bytes / 1024 / 1024:.0f} MB limit."
            )
        if sop_path.suffix.lower() == ".pdf":
            try:
                import fitz
                with fitz.open(str(sop_path)) as doc:
                    if doc.page_count > self.budget.max_pdf_pages:
                        raise ScanAbort(
                            f"{sop_path.name} has {doc.page_count} pages, over the "
                            f"{self.budget.max_pdf_pages} page limit."
                        )
            except ScanAbort:
                raise
            except Exception as exc:
                raise ScanAbort(f"Could not open {sop_path.name}: {exc}") from exc

    @staticmethod
    def _extract_sop_id(path: Path) -> str:
        """Extract SOP ID from filename, e.g. BC-MFG-UC-047 from BC-MFG-UC-047_...

        The fallback is validated: this value is interpolated into a write path,
        and an unvalidated filename segment could escape the output directory.
        """
        stem = path.stem
        parts = stem.split("_")
        for part in parts:
            if re.match(r'^[A-Z]{2}-[A-Z]{2,3}-[A-Z]{2}-\d+$', part):
                return part

        candidate = parts[0] if parts else stem
        if SOP_ID_RE.match(candidate) and candidate not in (".", ".."):
            return candidate

        import hashlib
        digest = hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:12]
        logger.warning("Unsafe SOP id %r from %s; using SOP-%s", candidate, path.name, digest)
        return f"SOP-{digest}"


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
    parser.add_argument("--sops", type=Path, default=Path("sops"))
    parser.add_argument("--out-dir", type=Path, default=Path("output"),
                        help="Directory for gap_registry files (default: output)")
    parser.add_argument(
        "--chroma-path", type=Path,
        default=Path(os.environ.get("CHROMA_PATH", "./chroma_db")),
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-clauses", type=int, default=0,
                        help="Cap clauses analysed per SOP (0 = use the budget default)")
    parser.add_argument("--max-llm-calls", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=None,
                        help="Per-call timeout in seconds")
    parser.add_argument("--deadline", type=float, default=None,
                        help="Whole-scan deadline in seconds")
    parser.add_argument("--no-guardrails", action="store_true",
                        help="Revert to legacy behaviour (no grounding or flagging)")
    args = parser.parse_args()

    budget = ScanBudget()
    if args.max_llm_calls is not None:
        budget.max_llm_calls = args.max_llm_calls
    if args.timeout is not None:
        budget.per_call_timeout_s = args.timeout
    if args.deadline is not None:
        budget.scan_deadline_s = args.deadline

    try:
        detector = SOPGapDetector(
            chroma_path=args.chroma_path,
            budget=budget,
            enable_guardrails=not args.no_guardrails,
        )

        if args.all:
            registry = detector.scan_all(
                args.sops, workers=args.workers,
                out_dir=args.out_dir, max_clauses=args.max_clauses,
            )
            attempted = sum(s.total_clauses_scanned for s in registry.scans)
            unscanned = registry.total_unscanned_clauses
            print(
                f"\nScan complete — {registry.total_gaps_found} gaps found across "
                f"{registry.total_sops_scanned} SOPs."
            )
            print(
                f"Coverage: {attempted - unscanned}/{attempted} clauses analysed"
                f"  |  {registry.total_flagged_findings} finding(s) need human verification"
            )
        else:
            result = detector.scan_sop(
                args.sop, out_dir=args.out_dir, max_clauses=args.max_clauses,
            )
            attempted, unscanned = result.total_clauses_scanned, result.unscanned_count
            print(f"\nScan complete — {result.gaps_found} gaps found in {result.sop_id}.")
            print(
                f"Coverage: {result.analysed}/{attempted} clauses analysed"
                f"  |  {result.flagged_findings_count} finding(s) need human verification"
            )

        if attempted > 0 and unscanned >= attempted:
            print(
                "\nERROR: no clause could be analysed. Every clause is UNSCANNED — "
                "this is NOT a clean result.",
                file=sys.stderr,
            )
            sys.exit(2)

    except ScanAbort as exc:
        logger.error("Scan aborted: %s", exc)
        print(f"\nScan aborted: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
