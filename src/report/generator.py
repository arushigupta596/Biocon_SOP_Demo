"""
Gap Report Generator — reads output/gap_registry.json and renders an
audit-ready DOCX report. Never calls any external API.

Usage:
    python -m src.report.generator --registry output/gap_registry.json
    python -m src.report.generator --registry output/gap_registry.json --output output/my_report.docx
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Inches, Pt, RGBColor

from src.gap_engine.guardrails import (
    CONFIDENCE_THRESHOLD,
    EXCERPT_CONTAINMENT_THRESHOLD,
    FLAG_LABELS,
)
from src.schemas import (
    GapRegistry,
    GapResult,
    Severity,
    SOPScanResult,
    VerificationFlag,
    VerificationStatus,
)


# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------

COLOUR_CRITICAL = RGBColor(0xC0, 0x00, 0x00)   # dark red
COLOUR_MAJOR = RGBColor(0xED, 0x7D, 0x31)       # orange
COLOUR_MINOR = RGBColor(0x70, 0xAD, 0x47)       # green
COLOUR_HEADING = RGBColor(0x1F, 0x49, 0x7D)     # dark blue

COLOUR_FLAG = RGBColor(0xB0, 0x6A, 0x00)        # amber — needs human verification

SEVERITY_COLOURS = {
    Severity.CRITICAL: COLOUR_CRITICAL,
    Severity.MAJOR: COLOUR_MAJOR,
    Severity.MINOR: COLOUR_MINOR,
}

FLAG_BG = "FFF4E5"
UNSCANNED_BG = "FFE8E8"

# Printed wherever unscanned clauses are reported. The single most important
# sentence in the document from a GxP standpoint.
UNSCANNED_DISCLAIMER = (
    "Unscanned clauses were not assessed. The absence of a finding for an "
    "unscanned clause must not be interpreted as evidence of compliance."
)


def _agg(registry: GapRegistry) -> dict:
    """Totals, recomputed from the scans so a stale header cannot mislead."""
    scans = registry.scans
    findings = [f for s in scans for f in s.findings]
    attempted = sum(s.total_clauses_scanned for s in scans)
    unscanned = sum(s.unscanned_count for s in scans)
    return {
        "attempted": attempted,
        "unscanned": unscanned,
        "analysed": sum(s.analysed for s in scans),
        "flagged": sum(1 for f in findings if f.is_flagged),
        "verified": sum(1 for f in findings if not f.is_flagged),
        "has_unscanned": unscanned > 0,
        "legacy": registry.guardrails_version is None,
    }


# ---------------------------------------------------------------------------
# XML sanitisation
# ---------------------------------------------------------------------------

# Control characters that are illegal in XML 1.0 and therefore rejected by
# python-docx. Tab (\x09), LF (\x0A) and CR (\x0D) are legal and preserved.
_ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")


def _scrub(value: Any) -> Any:
    """Recursively strip XML-illegal control characters from registry data.

    LLM output occasionally contains stray control bytes. Without this, the
    whole report render dies with a ValueError from python-docx.
    """
    if isinstance(value, str):
        return _ILLEGAL_XML_RE.sub("", value)
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _add_horizontal_line(doc: Document) -> None:
    para = doc.add_paragraph()
    pPr = para._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "CCCCCC")
    pBdr.append(bottom)
    pPr.append(pBdr)


def _set_cell_bg(cell: Any, hex_colour: str) -> None:
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_colour)
    tcPr.append(shd)


def _bold_para(doc: Document, text: str, size: int = 10) -> None:
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = True
    run.font.size = Pt(size)


def _label_value(doc: Document, label: str, value: str) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(2)
    run_label = p.add_run(f"{label}: ")
    run_label.bold = True
    run_label.font.size = Pt(9)
    run_value = p.add_run(value)
    run_value.font.size = Pt(9)


def _blockquote(doc: Document, text: str) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.3)
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(4)
    run = p.add_run(text[:500] + ("…" if len(text) > 500 else ""))
    run.font.size = Pt(8)
    run.font.italic = True
    run.font.color.rgb = RGBColor(0x40, 0x40, 0x40)


# ---------------------------------------------------------------------------
# Report generator
# ---------------------------------------------------------------------------

class ReportGenerator:
    def __init__(self, registry_path: Path) -> None:
        self.registry_path = registry_path
        data = _scrub(json.loads(registry_path.read_text()))
        self.registry = GapRegistry.model_validate(data)

    def render(self, output_path: Path) -> Path:
        doc = Document()
        self._set_margins(doc)
        self._cover_page(doc)
        doc.add_page_break()
        self._executive_summary(doc)
        doc.add_page_break()
        self._checklist_summary(doc)
        doc.add_page_break()
        self._unscanned_section(doc)
        self._gap_registry_section(doc)
        self._appendix_methodology(doc)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(output_path))
        return output_path

    # ------------------------------------------------------------------
    # Cover page
    # ------------------------------------------------------------------

    def _cover_page(self, doc: Document) -> None:
        doc.add_paragraph()
        doc.add_paragraph()

        title = doc.add_heading("SOP Compliance Gap Analysis Report", level=0)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER

        subtitle = doc.add_paragraph()
        subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = subtitle.add_run("Pharmaceutical Regulatory Compliance Assessment")
        run.font.size = Pt(14)
        run.font.color.rgb = COLOUR_HEADING

        doc.add_paragraph()
        _add_horizontal_line(doc)
        doc.add_paragraph()

        agg = _agg(self.registry)
        meta_table = doc.add_table(rows=7, cols=2)
        meta_table.style = "Table Grid"
        meta_data = [
            ("Prepared for", "Biocon Biologics Limited", None),
            ("Prepared by", "EMB Global", None),
            ("Report date", date.today().strftime("%d-%b-%Y"), None),
            ("SOPs assessed", str(self.registry.total_sops_scanned), None),
            ("Total gaps identified", str(self.registry.total_gaps_found), None),
            ("Clauses not assessed", str(agg["unscanned"]),
             COLOUR_CRITICAL if agg["unscanned"] else None),
            ("Findings requiring verification", str(agg["flagged"]),
             COLOUR_FLAG if agg["flagged"] else None),
        ]
        for row, (label, value, colour) in zip(meta_table.rows, meta_data):
            row.cells[0].text = label
            row.cells[0].paragraphs[0].runs[0].bold = True
            row.cells[1].text = value
            if colour is not None:
                run = row.cells[1].paragraphs[0].runs[0]
                run.bold = True
                run.font.color.rgb = colour
            _set_cell_bg(row.cells[0], "EBF3FB")

        doc.add_paragraph()
        _add_horizontal_line(doc)
        doc.add_paragraph()

        classif = doc.add_paragraph()
        classif.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = classif.add_run("CONFIDENTIAL — FOR INTERNAL USE ONLY")
        run.bold = True
        run.font.size = Pt(9)
        run.font.color.rgb = COLOUR_CRITICAL

    # ------------------------------------------------------------------
    # Executive summary
    # ------------------------------------------------------------------

    def _executive_summary(self, doc: Document) -> None:
        doc.add_heading("Executive Summary", level=1)

        # Count severities across all SOPs
        total_critical = total_major = total_minor = 0
        for scan in self.registry.scans:
            for f in scan.findings:
                if f.severity == Severity.CRITICAL:
                    total_critical += 1
                elif f.severity == Severity.MAJOR:
                    total_major += 1
                else:
                    total_minor += 1

        p = doc.add_paragraph()
        p.add_run(
            f"This report presents the findings of an automated regulatory compliance gap analysis "
            f"conducted against {self.registry.total_sops_scanned} Biocon Biologics Standard Operating "
            f"Procedures. The analysis identified a total of {self.registry.total_gaps_found} compliance "
            f"gap(s): "
        )
        r_crit = p.add_run(f"{total_critical} CRITICAL")
        r_crit.bold = True
        r_crit.font.color.rgb = COLOUR_CRITICAL
        p.add_run(", ")
        r_maj = p.add_run(f"{total_major} MAJOR")
        r_maj.bold = True
        r_maj.font.color.rgb = COLOUR_MAJOR
        p.add_run(", and ")
        r_min = p.add_run(f"{total_minor} MINOR")
        r_min.bold = True
        r_min.font.color.rgb = COLOUR_MINOR
        p.add_run(".")

        # --- Coverage and data integrity -------------------------------
        agg = _agg(self.registry)
        doc.add_heading("Analysis Coverage and Data Integrity", level=2)

        if agg["legacy"]:
            warn = doc.add_paragraph()
            run = warn.add_run(
                "This registry was produced before automated guardrails were "
                "introduced. Citation grounding and coverage accounting were not "
                "applied to these findings."
            )
            run.bold = True
            run.font.color.rgb = COLOUR_CRITICAL
        else:
            cov = doc.add_paragraph()
            cov.add_run(
                f"{agg['analysed']} of {agg['attempted']} SOP clause(s) were "
                f"successfully analysed. "
            )
            if agg["unscanned"]:
                r = cov.add_run(f"{agg['unscanned']} clause(s) could not be assessed. ")
                r.bold = True
                r.font.color.rgb = COLOUR_CRITICAL
            if agg["flagged"]:
                r = cov.add_run(
                    f"{agg['flagged']} finding(s) could not be automatically "
                    f"verified against the regulatory corpus and are marked "
                    f"REQUIRES HUMAN VERIFICATION. "
                )
                r.bold = True
                r.font.color.rgb = COLOUR_FLAG

            disc = doc.add_paragraph()
            r = disc.add_run(UNSCANNED_DISCLAIMER)
            r.bold = True
            r.font.size = Pt(9)
            r.font.color.rgb = COLOUR_CRITICAL

        doc.add_paragraph()

        # Summary table
        table = doc.add_table(rows=1, cols=8)
        table.style = "Table Grid"
        headers = ["SOP ID", "SOP File", "Clauses Scanned", "CRITICAL", "MAJOR",
                   "MINOR", "Unscanned", "Flagged"]
        header_row = table.rows[0]
        for i, hdr in enumerate(headers):
            header_row.cells[i].text = hdr
            header_row.cells[i].paragraphs[0].runs[0].bold = True
            _set_cell_bg(header_row.cells[i], "1F497D")
            header_row.cells[i].paragraphs[0].runs[0].font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

        for scan in sorted(self.registry.scans, key=lambda s: s.sop_id):
            crit = sum(1 for f in scan.findings if f.severity == Severity.CRITICAL)
            maj = sum(1 for f in scan.findings if f.severity == Severity.MAJOR)
            minor = sum(1 for f in scan.findings if f.severity == Severity.MINOR)
            row = table.add_row()
            row.cells[0].text = scan.sop_id
            row.cells[1].text = scan.sop_file
            row.cells[2].text = str(scan.total_clauses_scanned)
            row.cells[3].text = str(crit)
            row.cells[4].text = str(maj)
            row.cells[5].text = str(minor)
            row.cells[6].text = str(scan.unscanned_count)
            row.cells[7].text = str(scan.flagged_findings_count)
            if crit > 0:
                _set_cell_bg(row.cells[3], "FFCCCC")
            if maj > 0:
                _set_cell_bg(row.cells[4], "FFE4CC")
            if scan.unscanned_count > 0:
                _set_cell_bg(row.cells[6], UNSCANNED_BG)
            if scan.flagged_findings_count > 0:
                _set_cell_bg(row.cells[7], FLAG_BG)

    # ------------------------------------------------------------------
    # Compliance checklist summary
    # ------------------------------------------------------------------

    def _checklist_summary(self, doc: Document) -> None:
        doc.add_heading("Compliance Checklist Summary", level=1)
        intro = doc.add_paragraph(
            "Each row represents one SOP clause reviewed. "
            "A clause is marked PASS only when it was successfully analysed and no gap "
            "was identified against the regulatory corpus. "
            "Any clause with one or more findings is marked with its highest severity. "
            "A clause that could not be analysed is marked UNSCANNED. " +
            UNSCANNED_DISCLAIMER
        )
        intro.runs[0].font.size = Pt(9)
        doc.add_paragraph()

        PASS_BG   = "EBF5EB"
        CRIT_BG   = "FFDDDD"
        MAJOR_BG  = "FFEEDD"
        MINOR_BG  = "FAFFF0"
        HDR_BG    = "002F59"

        for scan in sorted(self.registry.scans, key=lambda s: s.sop_id):
            doc.add_heading(f"{scan.sop_id}", level=2)
            p = doc.add_paragraph()
            p.add_run(f"File: {scan.sop_file}   |   "
                      f"Clauses reviewed: {scan.total_clauses_scanned}   |   "
                      f"Gaps found: {scan.gaps_found}").font.size = Pt(8)

            # Build clause → worst finding map
            clause_map: dict[str, GapResult] = {}
            for f in scan.findings:
                existing = clause_map.get(f.sop_clause)
                order = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2}
                if existing is None or order[f.severity.value] < order[existing.severity.value]:
                    clause_map[f.sop_clause] = f

            # Collect all clause ids that appeared (from findings; gaps-only list)
            # Supplemented with PASS rows for clauses without findings
            all_gap_clauses = sorted(clause_map.keys())

            table = doc.add_table(rows=1, cols=4)
            table.style = "Table Grid"
            hdr = table.rows[0]
            for i, txt in enumerate(["Clause", "Description / Gap", "Regulation", "Status"]):
                hdr.cells[i].text = txt
                run = hdr.cells[i].paragraphs[0].runs[0]
                run.bold = True
                run.font.size = Pt(9)
                run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                _set_cell_bg(hdr.cells[i], HDR_BG)

            # Rows with gaps
            for clause_id in all_gap_clauses:
                finding = clause_map[clause_id]
                sev = finding.severity.value
                row = table.add_row()

                row.cells[0].text = clause_id
                row.cells[1].text = finding.gap_description[:180] + (
                    "…" if len(finding.gap_description) > 180 else ""
                )
                row.cells[2].text = finding.regulation_ref
                row.cells[3].text = sev

                bg = {"CRITICAL": CRIT_BG, "MAJOR": MAJOR_BG, "MINOR": MINOR_BG}.get(sev, "FFFFFF")
                colour = SEVERITY_COLOURS[finding.severity]
                for cell in row.cells:
                    _set_cell_bg(cell, bg)
                    cell.paragraphs[0].runs[0].font.size = Pt(9)
                # Bold + colour the status cell
                status_run = row.cells[3].paragraphs[0].runs[0]
                status_run.bold = True
                status_run.font.color.rgb = colour

            # UNSCANNED rows — listed explicitly so the checklist is complete.
            for uc in scan.unscanned_clauses:
                row = table.add_row()
                row.cells[0].text = uc.section_id or "—"
                row.cells[1].text = (
                    f"NOT ASSESSED — {uc.reason.value}"
                    + (f": {uc.detail}" if uc.detail else "")
                )[:180]
                row.cells[2].text = "—"
                row.cells[3].text = "UNSCANNED"
                for cell in row.cells:
                    _set_cell_bg(cell, UNSCANNED_BG)
                    cell.paragraphs[0].runs[0].font.size = Pt(9)
                unscanned_run = row.cells[3].paragraphs[0].runs[0]
                unscanned_run.bold = True
                unscanned_run.font.color.rgb = COLOUR_CRITICAL

            # PASS row summary.
            # Counts against clauses ANALYSED, not clauses parsed: the previous
            # formula silently reported every unscanned clause as PASS.
            pass_count = scan.analysed - len(all_gap_clauses)
            if pass_count > 0:
                row = table.add_row()
                row.cells[0].text = f"({pass_count} clause(s))"
                row.cells[1].text = "No regulatory gaps identified"
                row.cells[2].text = "—"
                row.cells[3].text = "PASS"
                for cell in row.cells:
                    _set_cell_bg(cell, PASS_BG)
                    cell.paragraphs[0].runs[0].font.size = Pt(9)
                pass_run = row.cells[3].paragraphs[0].runs[0]
                pass_run.bold = True
                pass_run.font.color.rgb = RGBColor(0x2E, 0x7D, 0x32)

            doc.add_paragraph()

    # ------------------------------------------------------------------
    # Per-SOP gap sections
    # ------------------------------------------------------------------

    def _unscanned_section(self, doc: Document) -> None:
        """Clauses that were not assessed. Rendered only when there are any."""
        agg = _agg(self.registry)
        if not agg["has_unscanned"]:
            return

        doc.add_heading("Unscanned Clauses", level=1)
        warn = doc.add_paragraph()
        run = warn.add_run(UNSCANNED_DISCLAIMER)
        run.bold = True
        run.font.size = Pt(10)
        run.font.color.rgb = COLOUR_CRITICAL

        intro = doc.add_paragraph(
            "The clauses below were parsed from the source SOP but could not be "
            "analysed against the regulatory corpus. Each must be reviewed manually."
        )
        intro.runs[0].font.size = Pt(9)

        for scan in sorted(self.registry.scans, key=lambda s: s.sop_id):
            if not scan.unscanned_clauses:
                continue
            doc.add_heading(f"{scan.sop_id} — {scan.unscanned_count} clause(s)", level=2)

            table = doc.add_table(rows=1, cols=5)
            table.style = "Table Grid"
            hdr = table.rows[0]
            for i, txt in enumerate(["Clause", "Heading", "Reason", "Detail", "Attempts"]):
                hdr.cells[i].text = txt
                run = hdr.cells[i].paragraphs[0].runs[0]
                run.bold = True
                run.font.size = Pt(9)
                run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                _set_cell_bg(hdr.cells[i], "8B0000")

            for uc in scan.unscanned_clauses:
                row = table.add_row()
                row.cells[0].text = uc.section_id or "—"
                row.cells[1].text = (uc.heading or "—")[:80]
                row.cells[2].text = uc.reason.value
                row.cells[3].text = (uc.detail or "—")[:160]
                row.cells[4].text = str(uc.attempts)
                for cell in row.cells:
                    _set_cell_bg(cell, UNSCANNED_BG)
                    cell.paragraphs[0].runs[0].font.size = Pt(8)

            doc.add_paragraph()

        doc.add_page_break()

    def _gap_registry_section(self, doc: Document) -> None:
        doc.add_heading("Gap Registry", level=1)

        for scan in sorted(self.registry.scans, key=lambda s: s.sop_id):
            doc.add_heading(f"{scan.sop_id} — {scan.sop_file}", level=2)
            p = doc.add_paragraph()
            p.add_run(
                f"Scan timestamp: {scan.scan_timestamp}  |  "
                f"Clauses scanned: {scan.total_clauses_scanned}  |  "
                f"Gaps found: {scan.gaps_found}"
            ).font.size = Pt(8)

            if not scan.findings:
                doc.add_paragraph("No compliance gaps detected for this SOP.")
                continue

            for i, finding in enumerate(
                sorted(scan.findings, key=lambda f: (f.severity.value, f.sop_clause))
            ):
                self._render_finding(doc, i + 1, finding)
                _add_horizontal_line(doc)

    def _render_finding(self, doc: Document, n: int, finding: GapResult) -> None:
        colour = SEVERITY_COLOURS[finding.severity]

        flags = list(getattr(finding, "verification_flags", []) or [])
        flagged = getattr(finding, "verification_status", VerificationStatus.VERIFIED) \
            != VerificationStatus.VERIFIED

        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(6)
        run_num = p.add_run(f"Finding #{n}  ")
        run_num.bold = True
        run_sev = p.add_run(f"[{finding.severity.value}]")
        run_sev.bold = True
        run_sev.font.color.rgb = colour
        if flagged:
            run_flag = p.add_run("  [REQUIRES HUMAN VERIFICATION]")
            run_flag.bold = True
            run_flag.font.color.rgb = COLOUR_FLAG
        p.add_run(f"  {finding.sop_clause} — {finding.regulation_ref}")

        _label_value(doc, "Clause", finding.sop_clause)
        _label_value(doc, "Regulation", finding.regulation_ref)
        _label_value(doc, "Confidence", f"{finding.confidence:.0%}")

        if flagged:
            reasons = "; ".join(FLAG_LABELS.get(f, f) for f in flags)
            pv = doc.add_paragraph()
            pv.paragraph_format.space_before = Pt(2)
            rl = pv.add_run("Verification: ")
            rl.bold = True
            rl.font.size = Pt(9)
            rl.font.color.rgb = COLOUR_FLAG
            rv = pv.add_run(reasons)
            rv.font.size = Pt(9)
            rv.font.color.rgb = COLOUR_FLAG

        source = getattr(finding, "grounded_source_file", None)
        score = getattr(finding, "grounding_score", None)
        if source and score is not None:
            _label_value(doc, "Citation traced to", f"{source} (match {score:.0%})")

        _label_value(doc, "Gap Description", finding.gap_description)
        _label_value(doc, "Remediation", finding.remediation)

        p_sop = doc.add_paragraph()
        p_sop.paragraph_format.space_before = Pt(4)
        run = p_sop.add_run("SOP Clause Text (verbatim):")
        run.bold = True
        run.font.size = Pt(9)
        _blockquote(doc, finding.sop_clause_text)

        p_reg = doc.add_paragraph()
        excerpt_unverified = VerificationFlag.EXCERPT_NOT_FOUND.value in flags
        if excerpt_unverified:
            # Never present an unlocatable quote as verbatim regulatory text.
            run = p_reg.add_run(
                "Regulatory Requirement (as quoted by the model, NOT located in "
                "the retrieved corpus):"
            )
            run.font.color.rgb = COLOUR_FLAG
        else:
            run = p_reg.add_run("Regulatory Requirement (verbatim):")
        run.bold = True
        run.font.size = Pt(9)
        _blockquote(doc, finding.regulation_excerpt)

    # ------------------------------------------------------------------
    # Appendix — methodology
    # ------------------------------------------------------------------

    def _appendix_methodology(self, doc: Document) -> None:
        doc.add_page_break()
        doc.add_heading("Appendix — Methodology", level=1)

        doc.add_heading("Pipeline Overview", level=2)
        doc.add_paragraph(
            "This report was generated by the Biocon SOP Compliance Engine, a RAG-based "
            "automated regulatory compliance analysis system. The pipeline operates as follows:"
        )
        for step in [
            "1. Regulatory PDFs are loaded, chunked (512 tokens / 64-token overlap), and embedded "
            "   using OpenAI text-embedding-3-large into a local ChromaDB vector store.",
            "2. Each SOP is parsed into individual clauses by section number.",
            "3. For each clause, the top-8 most relevant regulatory chunks are retrieved by "
            "   cosine similarity search.",
            "4. Claude (via OpenRouter) analyses each clause against the retrieved regulatory "
            "   context at temperature=0 and returns structured gap findings.",
            "5. Findings are validated against the GapResult schema, checked against "
            "   the retrieved regulatory context, and aggregated into this report. "
            "   Clauses that could not be analysed are recorded as UNSCANNED.",
        ]:
            doc.add_paragraph(step, style="List Bullet")

        doc.add_heading("Model and Parameters", level=2)
        models = {s.model for s in self.registry.scans if s.model}
        calls = sum(s.llm_calls for s in self.registry.scans)
        doc.add_paragraph(
            f"LLM: {', '.join(sorted(models)) or 'anthropic/claude-sonnet-4-5'} via OpenRouter\n"
            f"Temperature: 0 (deterministic output)\n"
            f"Embedding model: text-embedding-3-large\n"
            f"Chunk size: 512 tokens  |  Overlap: 64 tokens\n"
            f"Retrieval top-k: 8  |  Minimum relevance score: 0.30\n"
            f"Model calls made: {calls}"
        )

        # --- Automated guardrails -------------------------------------
        doc.add_heading("Automated Guardrails", level=2)
        version = self.registry.guardrails_version
        if version is None:
            warn = doc.add_paragraph()
            run = warn.add_run(
                "Legacy scan: automated guardrails were not applied to these "
                "findings. Citations in this report have not been verified "
                "against the retrieved regulatory corpus."
            )
            run.bold = True
            run.font.color.rgb = COLOUR_CRITICAL
        else:
            doc.add_paragraph(f"Guardrails version: {version}")
            doc.add_paragraph(
                "Every finding is checked automatically before it reaches this "
                "report. A finding that fails any check is retained but marked "
                "REQUIRES HUMAN VERIFICATION — it is never silently removed."
            )
            for item in [
                f"Citation grounding: the cited regulation must correspond to a "
                f"document actually retrieved for that clause.",
                f"Excerpt fidelity: at least "
                f"{EXCERPT_CONTAINMENT_THRESHOLD:.0%} of the quoted regulatory "
                f"text must be present in a single retrieved chunk.",
                f"Confidence: findings below {CONFIDENCE_THRESHOLD:.0%} model "
                f"confidence are flagged.",
                "SOP self-consistency: the quoted SOP text and clause number must "
                "match the clause that was analysed.",
                "Injection defence: document text is sanitised before it enters "
                "the prompt, and clauses containing instruction-shaped content "
                "are flagged.",
                "Coverage accounting: a clause that could not be analysed after "
                "retries is recorded as UNSCANNED, never as compliant.",
            ]:
                doc.add_paragraph(item, style="List Bullet")

            doc.add_heading("Verification Flags", level=3)
            for flag, label in FLAG_LABELS.items():
                para = doc.add_paragraph()
                r = para.add_run(f"{flag}: ")
                r.bold = True
                r.font.size = Pt(9)
                r.font.color.rgb = COLOUR_FLAG
                rv = para.add_run(label)
                rv.font.size = Pt(9)

        doc.add_heading("Severity Definitions", level=2)
        for sev, defn in [
            ("CRITICAL", "Missing required element; will cause regulatory non-conformance at inspection."),
            ("MAJOR", "Deficient element; likely to be cited at FDA/EMA inspection."),
            ("MINOR", "Improvement recommended; low regulatory risk; not typically cited."),
        ]:
            p = doc.add_paragraph()
            run = p.add_run(f"{sev}: ")
            run.bold = True
            run.font.color.rgb = SEVERITY_COLOURS[Severity(sev)]
            p.add_run(defn)

    # ------------------------------------------------------------------
    # Page margins
    # ------------------------------------------------------------------

    def _set_margins(self, doc: Document) -> None:
        for section in doc.sections:
            section.top_margin = Inches(1)
            section.bottom_margin = Inches(1)
            section.left_margin = Inches(1.2)
            section.right_margin = Inches(1.2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate audit-ready DOCX gap report from gap_registry.json"
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("output/gap_registry.json"),
        help="Path to gap_registry.json (default: output/gap_registry.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output .docx path (default: output/gap_report_YYYY-MM-DD.docx)",
    )
    args = parser.parse_args()

    if not args.registry.exists():
        raise FileNotFoundError(
            f"Registry not found: {args.registry}\n"
            "Run the detector first: python -m src.gap_engine.detector --all --sops sops/"
        )

    output_path = args.output or (
        Path("output") / f"gap_report_{date.today().isoformat()}.docx"
    )

    gen = ReportGenerator(args.registry)
    out = gen.render(output_path)
    print(f"Report written to: {out}")


if __name__ == "__main__":
    main()
