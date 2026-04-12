"""Pydantic v2 data contracts for the Biocon SOP Compliance Engine."""
from __future__ import annotations

from enum import Enum
from typing import List

from pydantic import BaseModel, Field, field_validator


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"


class GapResult(BaseModel):
    """One regulatory gap finding, produced per SOP clause."""

    sop_id: str = Field(description="Machine-readable SOP identifier, e.g. BC-MFG-UC-047")
    sop_clause: str = Field(description="Section reference within SOP, e.g. §7.3")
    sop_clause_text: str = Field(
        description="Verbatim excerpt of the SOP clause text that is deficient"
    )
    regulation_ref: str = Field(
        description="Regulatory citation, e.g. EMA CHMP/437/04 Rev1 §5.2.3"
    )
    regulation_excerpt: str = Field(
        description="Verbatim excerpt from the retrieved regulatory chunk"
    )
    gap_description: str = Field(
        description="Plain-English description of the compliance gap"
    )
    severity: Severity = Field(description="CRITICAL | MAJOR | MINOR")
    remediation: str = Field(
        description="Actionable steps to close the gap"
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Model confidence, 0.0–1.0")

    @field_validator("confidence")
    @classmethod
    def round_confidence(cls, v: float) -> float:
        return round(v, 4)


class SOPScanResult(BaseModel):
    """Aggregated result for one SOP file scan."""

    sop_id: str
    sop_file: str
    scan_timestamp: str  # ISO-8601
    total_clauses_scanned: int
    gaps_found: int
    findings: List[GapResult]


class GapRegistry(BaseModel):
    """Master registry across all scanned SOPs."""

    registry_timestamp: str  # ISO-8601
    total_sops_scanned: int
    total_gaps_found: int
    scans: List[SOPScanResult]
