"""
Pre-demo smoke test — verifies all 4 pre-scripted critical gaps fire
with confidence >= 0.80.

Run:
    pytest tests/test_demo_gaps.py -v
"""
from __future__ import annotations

from typing import Optional

import pytest

from src.schemas import GapResult, Severity

CONFIDENCE_THRESHOLD = 0.80


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def find_gap(
    findings: list[GapResult],
    sop_id: str,
    clause_fragment: str,
    reg_fragment: str,
) -> Optional[GapResult]:
    """
    Locate a gap finding by SOP ID, clause substring, and regulation substring.
    Case-insensitive matching to be robust against minor text variation.
    """
    clause_fragment = clause_fragment.lower()
    reg_fragment = reg_fragment.lower()
    for f in findings:
        if (
            f.sop_id == sop_id
            and clause_fragment in f.sop_clause.lower()
            and reg_fragment in f.regulation_ref.lower()
        ):
            return f
    return None


def _describe_findings(findings: list[GapResult]) -> str:
    if not findings:
        return "  (no findings)"
    return "\n".join(
        f"  {f.sop_id}  {f.sop_clause}  {f.regulation_ref}"
        for f in findings
    )


# ---------------------------------------------------------------------------
# GAP 1 — BC-MFG-UC-047 §7.3 — No comparability protocol
# Ref: EMA CHMP/437/04 Rev1 §5.2.3
# ---------------------------------------------------------------------------

class TestGap1ComparabilityProtocol:
    SOP_ID = "BC-MFG-UC-047"
    CLAUSE = "7.3"
    REG = "CHMP/437"

    def _gap(self, all_findings: list[GapResult]) -> GapResult:
        gap = find_gap(all_findings, self.SOP_ID, self.CLAUSE, self.REG)
        assert gap is not None, (
            f"GAP 1 not found: {self.SOP_ID} clause containing '§{self.CLAUSE}' "
            f"against regulation containing '{self.REG}'.\n"
            f"All findings:\n{_describe_findings(all_findings)}"
        )
        return gap

    def test_gap_fires(self, all_findings: list[GapResult]) -> None:
        self._gap(all_findings)

    def test_severity_is_critical(self, all_findings: list[GapResult]) -> None:
        gap = self._gap(all_findings)
        assert gap.severity == Severity.CRITICAL, (
            f"Expected CRITICAL, got {gap.severity}"
        )

    def test_confidence_threshold(self, all_findings: list[GapResult]) -> None:
        gap = self._gap(all_findings)
        assert gap.confidence >= CONFIDENCE_THRESHOLD, (
            f"Confidence {gap.confidence:.2f} is below threshold {CONFIDENCE_THRESHOLD}"
        )

    def test_remediation_not_empty(self, all_findings: list[GapResult]) -> None:
        gap = self._gap(all_findings)
        assert len(gap.remediation.strip()) >= 20, (
            "Remediation is too short or empty"
        )

    def test_gap_description_mentions_comparability(self, all_findings: list[GapResult]) -> None:
        gap = self._gap(all_findings)
        assert "comparab" in gap.gap_description.lower(), (
            f"Gap description does not mention comparability: {gap.gap_description}"
        )


# ---------------------------------------------------------------------------
# GAP 2 — BC-MFG-UC-047 §6.2 — Glycosylation not listed as CQA
# Ref: EMA BWP/247713 §5.3.2
# ---------------------------------------------------------------------------

class TestGap2GlycosylationCQA:
    SOP_ID = "BC-MFG-UC-047"
    CLAUSE = "6.2"
    REG = "BWP/247"

    def _gap(self, all_findings: list[GapResult]) -> GapResult:
        gap = find_gap(all_findings, self.SOP_ID, self.CLAUSE, self.REG)
        assert gap is not None, (
            f"GAP 2 not found: {self.SOP_ID} clause containing '§{self.CLAUSE}' "
            f"against regulation containing '{self.REG}'.\n"
            f"All findings:\n{_describe_findings(all_findings)}"
        )
        return gap

    def test_gap_fires(self, all_findings: list[GapResult]) -> None:
        self._gap(all_findings)

    def test_severity_is_critical(self, all_findings: list[GapResult]) -> None:
        assert self._gap(all_findings).severity == Severity.CRITICAL

    def test_confidence_threshold(self, all_findings: list[GapResult]) -> None:
        gap = self._gap(all_findings)
        assert gap.confidence >= CONFIDENCE_THRESHOLD

    def test_gap_description_mentions_glycosylation(self, all_findings: list[GapResult]) -> None:
        gap = self._gap(all_findings)
        assert "glycosylat" in gap.gap_description.lower(), (
            f"Gap description does not mention glycosylation: {gap.gap_description}"
        )


# ---------------------------------------------------------------------------
# GAP 3 — BC-QC-BR-012 §5.2 — Review timeline not specified
# Ref: 21 CFR 211.192
# ---------------------------------------------------------------------------

class TestGap3BatchRecordTimeline:
    SOP_ID = "BC-QC-BR-012"
    CLAUSE = "5.2"
    REG = "211.192"

    def _gap(self, all_findings: list[GapResult]) -> GapResult:
        gap = find_gap(all_findings, self.SOP_ID, self.CLAUSE, self.REG)
        assert gap is not None, (
            f"GAP 3 not found: {self.SOP_ID} clause containing '§{self.CLAUSE}' "
            f"against regulation containing '{self.REG}'.\n"
            f"All findings:\n{_describe_findings(all_findings)}"
        )
        return gap

    def test_gap_fires(self, all_findings: list[GapResult]) -> None:
        self._gap(all_findings)

    def test_severity_is_critical(self, all_findings: list[GapResult]) -> None:
        assert self._gap(all_findings).severity == Severity.CRITICAL

    def test_confidence_threshold(self, all_findings: list[GapResult]) -> None:
        gap = self._gap(all_findings)
        assert gap.confidence >= CONFIDENCE_THRESHOLD

    def test_gap_description_mentions_timeline(self, all_findings: list[GapResult]) -> None:
        gap = self._gap(all_findings)
        keywords = ["timeline", "deadline", "frequency", "timeframe", "completion", "period"]
        assert any(kw in gap.gap_description.lower() for kw in keywords), (
            f"Gap description does not mention timeline/deadline: {gap.gap_description}"
        )


# ---------------------------------------------------------------------------
# GAP 4 — BC-AN-MV-031 §6.3 — System suitability criteria not defined
# Ref: ICH Q2(R1) §2 OR 21 CFR 211.194(a)
# ---------------------------------------------------------------------------

class TestGap4SystemSuitability:
    SOP_ID = "BC-AN-MV-031"
    CLAUSE = "6.3"
    REG_ICH = "q2"         # ICH Q2(R1)
    REG_CFR = "211.194"    # 21 CFR 211.194

    def _gap(self, all_findings: list[GapResult]) -> GapResult:
        gap = (
            find_gap(all_findings, self.SOP_ID, self.CLAUSE, self.REG_ICH)
            or find_gap(all_findings, self.SOP_ID, self.CLAUSE, self.REG_CFR)
        )
        assert gap is not None, (
            f"GAP 4 not found: {self.SOP_ID} clause containing '§{self.CLAUSE}' "
            f"against '{self.REG_ICH}' or '{self.REG_CFR}'.\n"
            f"All findings:\n{_describe_findings(all_findings)}"
        )
        return gap

    def test_gap_fires(self, all_findings: list[GapResult]) -> None:
        self._gap(all_findings)

    def test_severity_is_critical(self, all_findings: list[GapResult]) -> None:
        assert self._gap(all_findings).severity == Severity.CRITICAL

    def test_confidence_threshold(self, all_findings: list[GapResult]) -> None:
        gap = self._gap(all_findings)
        assert gap.confidence >= CONFIDENCE_THRESHOLD

    def test_gap_description_mentions_system_suitability(self, all_findings: list[GapResult]) -> None:
        gap = self._gap(all_findings)
        assert "system suitability" in gap.gap_description.lower(), (
            f"Gap description does not mention system suitability: {gap.gap_description}"
        )


# ---------------------------------------------------------------------------
# Registry integrity checks
# ---------------------------------------------------------------------------

class TestRegistryIntegrity:
    def test_four_sops_scanned(self, gap_registry) -> None:
        assert gap_registry.total_sops_scanned == 4, (
            f"Expected 4 SOPs scanned, got {gap_registry.total_sops_scanned}"
        )

    def test_minimum_gap_count(self, gap_registry) -> None:
        assert gap_registry.total_gaps_found >= 4, (
            f"Expected at least 4 gaps total, got {gap_registry.total_gaps_found}"
        )

    def test_all_findings_have_valid_confidence(self, all_findings: list[GapResult]) -> None:
        for f in all_findings:
            assert 0.0 <= f.confidence <= 1.0, (
                f"Invalid confidence {f.confidence} in {f.sop_id} {f.sop_clause}"
            )

    def test_all_findings_have_valid_severity(self, all_findings: list[GapResult]) -> None:
        valid = {Severity.CRITICAL, Severity.MAJOR, Severity.MINOR}
        for f in all_findings:
            assert f.severity in valid, (
                f"Invalid severity '{f.severity}' in {f.sop_id} {f.sop_clause}"
            )

    def test_no_empty_remediations(self, all_findings: list[GapResult]) -> None:
        for f in all_findings:
            assert f.remediation.strip(), (
                f"Empty remediation in {f.sop_id} {f.sop_clause}"
            )

    def test_no_empty_gap_descriptions(self, all_findings: list[GapResult]) -> None:
        for f in all_findings:
            assert f.gap_description.strip(), (
                f"Empty gap_description in {f.sop_id} {f.sop_clause}"
            )
