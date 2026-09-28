"""Numeric caps and timeouts for the compliance engine.

Every value can be overridden at runtime with the matching GR_* environment
variable, so a presenter can loosen a cap on Streamlit Cloud without a redeploy.
"""
from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# --- Upload -----------------------------------------------------------------
# Keep MAX_UPLOAD_MB in step with maxUploadSize in .streamlit/config.toml.
MAX_UPLOAD_MB       = _int("GR_MAX_UPLOAD_MB", 10)
MAX_UPLOAD_BYTES    = MAX_UPLOAD_MB * 1024 * 1024
MAX_PDF_PAGES       = _int("GR_MAX_PDF_PAGES", 60)
MAX_DOCX_PARAGRAPHS = _int("GR_MAX_DOCX_PARAGRAPHS", 3000)

# Zip-bomb guards applied while sniffing a .docx container.
MAX_ZIP_ENTRIES     = _int("GR_MAX_ZIP_ENTRIES", 2000)
MAX_ZIP_UNCOMPRESSED_BYTES = _int("GR_MAX_ZIP_UNCOMPRESSED_MB", 200) * 1024 * 1024

# --- Scan shape -------------------------------------------------------------
MAX_CLAUSES_SCANNED = _int("GR_MAX_CLAUSES", 120)
MAX_SCANS_PER_SESSION = _int("GR_MAX_SCANS_PER_SESSION", 5)
MAX_CONCURRENT_SCANS  = _int("GR_MAX_CONCURRENT_SCANS", 2)

# --- Timeouts (seconds) -----------------------------------------------------
SCAN_TIMEOUT_S   = _float("GR_SCAN_TIMEOUT_S", 600.0)
REPORT_TIMEOUT_S = _float("GR_REPORT_TIMEOUT_S", 120.0)
PYTEST_TIMEOUT_S = _float("GR_PYTEST_TIMEOUT_S", 300.0)
TEST_COOLDOWN_S  = _float("GR_TEST_COOLDOWN_S", 60.0)

# --- Session hygiene --------------------------------------------------------
SESSION_TTL_HOURS = _int("GR_SESSION_TTL_HOURS", 12)

# --- Names of environment variables that must never reach the UI ------------
SECRET_ENV_KEYS = (
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)
