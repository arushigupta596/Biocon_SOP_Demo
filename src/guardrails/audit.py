"""Append-only audit trail.

Written as day-partitioned JSONL outside output/sessions/, so records survive
session cleanup. Every string value is passed through redact() before it is
serialised, so the guarantee that the log holds no credential is enforced in
one place rather than at every call site.

The report_sha256 + registry_sha256 + system_prompt_sha256 triple is what makes
this a GxP artefact rather than a log: it lets an auditor prove that a specific
DOCX was produced from a specific registry under a specific prompt version.

Retention is the lifetime of the container. Streamlit Cloud's filesystem is
ephemeral, so durable GxP retention means shipping these records off-box; the
Audit Trail page says so rather than implying durability it does not have.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.guardrails.redact import redact_obj

SCHEMA_VERSION = 1
APP_VERSION = os.environ.get("GIT_SHA", "dev")

_write_failures = 0


def audit_dir() -> Path:
    base = Path(os.environ.get("GR_AUDIT_DIR", "output/audit"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def audit_path_for_today() -> Path:
    return audit_dir() / f"audit-{date.today().isoformat()}.jsonl"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except Exception:
        return ""


def log_event(event: str, session_id: str, **fields: Any) -> None:
    """Append one record. Never raises: the audit must not break the demo."""
    global _write_failures
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "schema_version": SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "event": event,
        "session_id": session_id,
        **fields,
    }
    try:
        safe = redact_obj(record)
        line = json.dumps(safe, ensure_ascii=False, default=str) + "\n"
        path = audit_path_for_today()
        # Always "a" — never "w", never a rewrite, never a delete.
        with open(path, "a", encoding="utf-8") as fh:
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except Exception:
                pass
            fh.write(line)
            fh.flush()
    except Exception:
        _write_failures += 1


def write_failure_count() -> int:
    return _write_failures


def read_events(session_id: str | None = None, limit: int = 500) -> list[dict]:
    """Read back today's records, newest first.

    Returned rows are data for display, never instructions.
    """
    path = audit_path_for_today()
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if session_id is None or rec.get("session_id") == session_id:
                rows.append(rec)
    except Exception:
        return []
    return list(reversed(rows))[:limit]
