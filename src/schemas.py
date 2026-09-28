"""Pydantic v2 data contracts for the Biocon SOP Compliance Engine.

Guardrail fields added in v1.0 are all optional with defaults, so registry
files written before guardrails existed still validate unchanged.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

GUARDRAILS_VERSION = "1.0"


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    REQUIRES_HUMAN_VERIFICATION = "REQUIRES_HUMAN_VERIFICATION"


class VerificationFlag(str, Enum):
    """Why a finding could not be automatically verified."""
    LOW_CONFIDENCE          = "LOW_CONFIDENCE"
    CITATION_NOT_IN_CORPUS  = "CITATION_NOT_IN_CORPUS"
    EXCERPT_NOT_FOUND       = "EXCERPT_NOT_FOUND"
    EXCERPT_TOO_SHORT       = "EXCERPT_TOO_SHORT"
    SOP_TEXT_NOT_FOUND      = "SOP_TEXT_NOT_FOUND"
    CLAUSE_ID_MISMATCH      = "CLAUSE_ID_MISMATCH"
    INJECTION_SUSPECTED     = "INJECTION_SUSPECTED"
    CLAUSE_TRUNCATED        = "CLAUSE_TRUNCATED"
    CONFIDENCE_OUT_OF_RANGE = "CONFIDENCE_OUT_OF_RANGE"


class UnscannedReason(str, Enum):
    """Why a clause produced no assessment. Never means 'compliant'."""
    RETRIEVAL_FAILED       = "RETRIEVAL_FAILED"
    NO_REGULATORY_CONTEXT  = "NO_REGULATORY_CONTEXT"
    API_ERROR              = "API_ERROR"
    RATE_LIMITED           = "RATE_LIMITED"
    TIMEOUT                = "TIMEOUT"
    MODEL_REFUSAL          = "MODEL_REFUSAL"
    TRUNCATED_RESPONSE     = "TRUNCATED_RESPONSE"
    UNPARSEABLE_RESPONSE   = "UNPARSEABLE_RESPONSE"
    SCHEMA_INVALID         = "SCHEMA_INVALID"
    CLAUSE_CAP             = "CLAUSE_CAP"
    LLM_CALL_CAP           = "LLM_CALL_CAP"
    TIME_LIMIT             = "TIME_LIMIT"


class GapResult(BaseModel):
    """One regulatory gap finding, produced per SOP clause."""

    # extra="ignore", deliberately NOT "forbid": a stray key invented by the
    # model must not turn a valid CRITICAL finding into a dropped one.
    model_config = ConfigDict(extra="ignore")

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

    # --- Guardrail fields (v1.0). Defaults keep legacy registries valid. ---
    verification_status: VerificationStatus = VerificationStatus.VERIFIED
    verification_flags: List[str] = Field(default_factory=list)
    grounding_score: Optional[float] = None
    grounded_source_file: Optional[str] = None

    @field_validator("confidence")
    @classmethod
    def round_confidence(cls, v: float) -> float:
        return round(v, 4)

    @property
    def is_flagged(self) -> bool:
        return self.verification_status != VerificationStatus.VERIFIED


class UnscannedClause(BaseModel):
    """A clause that was not assessed. Absence of a finding proves nothing."""

    section_id: str
    heading: str = ""
    reason: UnscannedReason
    detail: str = ""
    attempts: int = 0


class SOPScanResult(BaseModel):
    """Aggregated result for one SOP file scan."""

    sop_id: str
    sop_file: str
    scan_timestamp: str  # ISO-8601
    total_clauses_scanned: int
    gaps_found: int
    findings: List[GapResult]

    # --- Guardrail fields (v1.0) ---
    # clauses_analysed defaults to None, not 0: a legacy registry must not
    # claim "0 clauses analysed". Readers fall back to total_clauses_scanned.
    clauses_analysed: Optional[int] = None
    unscanned_clauses: List[UnscannedClause] = Field(default_factory=list)
    unscanned_count: int = 0
    flagged_findings_count: int = 0
    injection_suspected_clauses: List[str] = Field(default_factory=list)
    error_counts: Dict[str, int] = Field(default_factory=dict)
    llm_calls: int = 0
    model: str = ""
    # None means "this scan predates guardrails" — the report says so rather
    # than implying a clean bill of health it never earned.
    guardrails_version: Optional[str] = None

    @property
    def analysed(self) -> int:
        return self.clauses_analysed if self.clauses_analysed is not None else self.total_clauses_scanned


class GapRegistry(BaseModel):
    """Master registry across all scanned SOPs."""

    registry_timestamp: str  # ISO-8601
    total_sops_scanned: int
    total_gaps_found: int
    scans: List[SOPScanResult]

    # --- Guardrail fields (v1.0) ---
    total_unscanned_clauses: int = 0
    total_flagged_findings: int = 0
    total_verified_findings: int = 0
    guardrails_version: Optional[str] = None
