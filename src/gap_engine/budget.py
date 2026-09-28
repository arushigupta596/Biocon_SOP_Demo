"""Cost and runtime caps for a scan.

Shared across the ThreadPoolExecutor in scan_all, so every mutation is locked.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

from src.schemas import UnscannedReason


class ScanAbort(Exception):
    """Fatal: the whole run must stop loudly (bad key, unreadable file)."""


class BudgetExceeded(Exception):
    """A cap was hit. Remaining clauses are marked UNSCANNED; output still ships."""

    def __init__(self, reason: UnscannedReason, detail: str = "") -> None:
        super().__init__(detail or reason.value)
        self.reason = reason
        self.detail = detail


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


@dataclass
class ScanBudget:
    # Demo SOPs parse to 11-15 clauses, so 60 leaves 4x headroom while still
    # capping a pathological upload.
    max_clauses_per_sop: int = field(default_factory=lambda: _int("GUARDRAIL_MAX_CLAUSES", 60))
    max_llm_calls: int = field(default_factory=lambda: _int("GUARDRAIL_MAX_LLM_CALLS", 250))
    per_call_timeout_s: float = field(default_factory=lambda: _float("GUARDRAIL_TIMEOUT_S", 90.0))
    scan_deadline_s: float = field(default_factory=lambda: _float("GUARDRAIL_DEADLINE_S", 900.0))
    max_sop_bytes: int = field(default_factory=lambda: _int("GUARDRAIL_MAX_SOP_BYTES", 15 * 1024 * 1024))
    max_pdf_pages: int = field(default_factory=lambda: _int("GUARDRAIL_MAX_PDF_PAGES", 300))

    def __post_init__(self) -> None:
        self._started_at = time.monotonic()
        self._calls = 0
        self._lock = threading.Lock()

    # -- runtime ----------------------------------------------------------
    def elapsed(self) -> float:
        return time.monotonic() - self._started_at

    def remaining_seconds(self) -> float:
        return max(0.0, self.scan_deadline_s - self.elapsed())

    def check_deadline(self) -> None:
        if self.elapsed() >= self.scan_deadline_s:
            raise BudgetExceeded(
                UnscannedReason.TIME_LIMIT,
                f"scan deadline of {self.scan_deadline_s:.0f}s exceeded",
            )

    def reserve_call(self) -> int:
        with self._lock:
            if self._calls >= self.max_llm_calls:
                raise BudgetExceeded(
                    UnscannedReason.LLM_CALL_CAP,
                    f"LLM call cap of {self.max_llm_calls} reached",
                )
            self._calls += 1
            return self._calls

    @property
    def calls_made(self) -> int:
        with self._lock:
            return self._calls

    def snapshot(self) -> dict:
        return {
            "llm_calls": self.calls_made,
            "max_llm_calls": self.max_llm_calls,
            "elapsed_s": round(self.elapsed(), 1),
            "deadline_s": self.scan_deadline_s,
            "max_clauses_per_sop": self.max_clauses_per_sop,
            "per_call_timeout_s": self.per_call_timeout_s,
        }
