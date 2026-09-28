"""
Biocon SOP Compliance Engine — Streamlit Demo UI

Run:
    streamlit run app.py
"""
from __future__ import annotations

import hashlib
import json
import os
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from src.gap_engine.parsing import count_clauses
from src.guardrails import (
    MAX_CLAUSES_SCANNED,
    audit_path_for_today,
    log_event,
    read_events,
    sha256_bytes,
    sha256_file,
    write_failure_count,
    MAX_CONCURRENT_SCANS,
    MAX_SCANS_PER_SESSION,
    MAX_UPLOAD_MB,
    PYTEST_TIMEOUT_S,
    REPORT_TIMEOUT_S,
    SCAN_TIMEOUT_S,
    SECRET_ENV_KEYS,
    SESSION_TTL_HOURS,
    TEST_COOLDOWN_S,
    assert_contained,
    redact,
    safe_html,
    st_code,
    validate_upload,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Streamlit Cloud: inject secrets into os.environ so child processes
# (detector, report generator) can read OPENROUTER_API_KEY at runtime.
# st.secrets is populated from the Streamlit Cloud dashboard; locally it
# falls back to .env via load_dotenv() above.
# ---------------------------------------------------------------------------
_SECRET_KEYS = ["OPENROUTER_API_KEY", "CHROMA_PATH"]
_SECRET_LOAD_ERROR: str | None = None
for _k in _SECRET_KEYS:
    try:
        if _k in st.secrets and not os.environ.get(_k):
            os.environ[_k] = st.secrets[_k]
    except FileNotFoundError:
        # No secrets.toml at all — normal for local runs using .env.
        pass
    except Exception as _exc:
        _SECRET_LOAD_ERROR = f"{type(_exc).__name__} while reading {_k}"

# A missing key used to surface 30 seconds later as a subprocess stack trace.
SCANNER_READY = bool(os.environ.get("OPENROUTER_API_KEY"))

st.set_page_config(
    page_title="Biocon | SOP Compliance Engine",
    page_icon="assets/favicon.png" if Path("assets/favicon.png").exists() else None,
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE          = Path(__file__).parent
OUTPUT_DIR    = BASE / "output"
SOPS_DIR      = BASE / "sops"
MANIFEST_PATH = OUTPUT_DIR / "corpus_manifest.json"

# Uploads never land in sops/ itself, so the four bundled demo SOPs are
# structurally unreachable rather than protected by a check someone can forget.
UPLOAD_ROOT   = SOPS_DIR / "uploads"
SESSIONS_ROOT = OUTPUT_DIR / "sessions"
BASELINE_DIR  = OUTPUT_DIR / "baseline"

OUTPUT_DIR.mkdir(exist_ok=True)
SOPS_DIR.mkdir(exist_ok=True)
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
SESSIONS_ROOT.mkdir(parents=True, exist_ok=True)


def session_id() -> str:
    """Stable per-browser-session id. Not the private Streamlit ctx API."""
    if "sid" not in st.session_state:
        st.session_state.sid = uuid.uuid4().hex[:12]
    return st.session_state.sid


def session_dir() -> Path:
    """This session's private output directory.

    Streamlit Cloud runs one process for every visitor and the filesystem is
    shared. Without this, the master registry merges every visitor's per-SOP
    file and one client can download another's findings.
    """
    d = SESSIONS_ROOT / session_id()
    d.mkdir(parents=True, exist_ok=True)
    return d


def session_upload_dir() -> Path:
    d = UPLOAD_ROOT / session_id()
    d.mkdir(parents=True, exist_ok=True)
    return d


def registry_path() -> Path:
    # A function, not a module constant: the session id is unknown at import.
    return session_dir() / "gap_registry.json"


@st.cache_resource
def sweep_stale_sessions() -> int:
    """Delete session directories older than the TTL. Runs once per process."""
    cutoff = time.time() - SESSION_TTL_HOURS * 3600
    removed = 0
    for root in (SESSIONS_ROOT, UPLOAD_ROOT):
        for child in root.glob("*"):
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    assert_contained(child, root)
                    shutil.rmtree(child)
                    removed += 1
            except Exception:
                continue
    return removed


sweep_stale_sessions()

# Process-wide concurrency brakes.
_SCAN_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_SCANS)
_TEST_SEMAPHORE = threading.BoundedSemaphore(1)

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Merriweather+Sans:wght@300;400;600;700;800&display=swap');

/* ── Reset ── */
html, body, [class*="css"], .stApp {
    font-family: 'Merriweather Sans', sans-serif !important;
}

/* ── Background ── */
.stApp { background: #f0f4f8; }

/* ── Hide default Streamlit chrome ── */
#MainMenu, footer, header { visibility: hidden; }
.block-container { padding-top: 0 !important; }

/* ── Sidebar ── */
[data-testid="stSidebar"] {
    background: #002F59 !important;
    border-right: none;
    box-shadow: 2px 0 12px rgba(0,0,0,0.15);
}
[data-testid="stSidebar"] > div { padding-top: 0 !important; }
[data-testid="stSidebar"] * { color: #c8d8e8 !important; }
[data-testid="stSidebar"] hr { border-color: rgba(112,169,220,0.2) !important; margin: 0 !important; }

/* ── Nav radio items ── */
[data-testid="stSidebar"] .stRadio > div { gap: 2px; }
[data-testid="stSidebar"] .stRadio label {
    font-size: 13px !important;
    font-weight: 500 !important;
    padding: 10px 16px !important;
    border-radius: 4px !important;
    color: #a8c4dc !important;
    letter-spacing: 0.3px;
    transition: all 0.15s ease;
    cursor: pointer;
}
[data-testid="stSidebar"] .stRadio label:hover { background: rgba(112,169,220,0.1) !important; color: #fff !important; }
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p { color: #a8c4dc !important; }

/* ── Header ── */
.app-header {
    background: linear-gradient(100deg, #002F59 60%, #0d3d6e 100%);
    padding: 22px 36px 18px;
    border-bottom: 2px solid #70A9DC;
    margin-bottom: 0;
}
.app-header-eyebrow {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 2.5px;
    text-transform: uppercase;
    color: #70A9DC;
    margin-bottom: 4px;
}
.app-header-title {
    font-size: 21px;
    font-weight: 700;
    color: #ffffff;
    margin: 0;
    letter-spacing: 0.2px;
}

/* ── Page title ── */
.page-title {
    font-size: 22px;
    font-weight: 700;
    color: #002F59;
    margin: 28px 0 4px;
    padding-bottom: 10px;
    border-bottom: 2px solid #e2eaf2;
}
.page-desc {
    font-size: 13px;
    color: #607080;
    margin: 0 0 28px;
    line-height: 1.6;
}

/* ── Section label ── */
.section-label {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: #002F59;
    margin: 24px 0 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid #dce8f0;
}

/* ── Metrics ── */
[data-testid="stMetric"] {
    background: #ffffff;
    border-radius: 6px;
    border: 1px solid #e2eaf2;
    border-top: 3px solid #002F59;
    padding: 18px 20px !important;
    box-shadow: 0 1px 4px rgba(0,47,89,0.06);
}
[data-testid="stMetricLabel"] {
    font-size: 10px !important;
    font-weight: 700 !important;
    text-transform: uppercase !important;
    letter-spacing: 1.2px !important;
    color: #708090 !important;
}
[data-testid="stMetricValue"] {
    font-size: 34px !important;
    font-weight: 800 !important;
    color: #002F59 !important;
}

/* ── Buttons ── */
.stButton > button {
    background: #002F59 !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 4px !important;
    font-family: 'Merriweather Sans', sans-serif !important;
    font-size: 12px !important;
    font-weight: 700 !important;
    letter-spacing: 1.2px !important;
    text-transform: uppercase !important;
    padding: 11px 28px !important;
    box-shadow: 0 2px 6px rgba(0,47,89,0.25) !important;
    transition: all 0.18s ease !important;
}
.stButton > button:hover {
    background: #003f78 !important;
    box-shadow: 0 4px 14px rgba(0,47,89,0.35) !important;
    transform: translateY(-1px);
}
.stButton > button:active { transform: translateY(0); }

/* ── Download button ── */
[data-testid="stDownloadButton"] > button {
    background: transparent !important;
    color: #002F59 !important;
    border: 1.5px solid #70A9DC !important;
    border-radius: 4px !important;
    font-size: 12px !important;
    font-weight: 700 !important;
    letter-spacing: 1.2px !important;
    text-transform: uppercase !important;
    padding: 11px 28px !important;
    box-shadow: none !important;
}
[data-testid="stDownloadButton"] > button:hover {
    background: #002F59 !important;
    color: #ffffff !important;
    border-color: #002F59 !important;
}

/* ── File uploader ── */
[data-testid="stFileUploader"] {
    background: #ffffff;
    border: 1.5px dashed #b0c8de;
    border-radius: 6px;
}
[data-testid="stFileUploader"]:focus-within { border-color: #002F59; }

/* ── Alerts ── */
[data-testid="stAlert"] { border-radius: 4px !important; font-size: 13px !important; }

/* ── Code blocks ── */
[data-testid="stCode"] > div {
    background: #f7f9fc !important;
    border: 1px solid #dce8f0 !important;
    border-radius: 4px !important;
    font-size: 12px !important;
}

/* ── Checkbox ── */
.stCheckbox label p { font-size: 13px !important; color: #334455 !important; }

/* ── Table ── */
[data-testid="stTable"] table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
[data-testid="stTable"] th {
    background: #002F59 !important;
    color: #ffffff !important;
    padding: 10px 14px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1px;
    text-transform: uppercase;
    border: none !important;
}
[data-testid="stTable"] td {
    padding: 10px 14px;
    border-bottom: 1px solid #eef2f6 !important;
    color: #334455;
}
[data-testid="stTable"] tr:hover td { background: #f4f8fc; }

/* ── Gap finding cards ── */
.gap-card {
    background: #ffffff;
    border-radius: 6px;
    border: 1px solid #e2eaf2;
    border-left: 4px solid #ccc;
    padding: 16px 20px;
    margin-bottom: 10px;
    box-shadow: 0 1px 3px rgba(0,47,89,0.05);
}
.gap-card.critical { border-left-color: #B00020; }
.gap-card.major    { border-left-color: #D46B08; }
.gap-card.minor    { border-left-color: #5a8a00; }
.gap-card.flagged  { border-left-color: #B06A00; background: #FFFBF4; }

.flag-note {
    font-size: 11px;
    color: #8a5a00;
    margin: 6px 0 0;
    line-height: 1.5;
}
.gap-trace { font-size: 11px; color: #5a8a00; margin: 6px 0 0; }
.gap-trace.flag { color: #B06A00; font-weight: 600; }

.reject-pill {
    background: #FFF4E5;
    border: 1px solid #F0C48A;
    border-left: 4px solid #B06A00;
    border-radius: 6px;
    padding: 12px 16px;
    font-size: 13px;
    color: #6a4500;
    margin-bottom: 10px;
}

.gap-header { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
.gap-badge {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.2px;
    text-transform: uppercase;
    padding: 3px 10px;
    border-radius: 3px;
    color: #fff;
}
.badge-CRITICAL { background: #B00020; }
.badge-MAJOR    { background: #D46B08; }
.badge-MINOR    { background: #5a8a00; }
.badge-FLAG     { background: #B06A00; }

.gap-clause { font-size: 13px; font-weight: 700; color: #002F59; }
.gap-reg {
    font-size: 11px;
    color: #708090;
    background: #f0f4f8;
    padding: 2px 8px;
    border-radius: 3px;
    font-family: 'Courier New', monospace;
}
.gap-desc {
    font-size: 13px;
    color: #223344;
    line-height: 1.6;
    margin: 0 0 10px;
}
.gap-rem {
    font-size: 12px;
    color: #445566;
    line-height: 1.5;
    padding: 10px 12px;
    background: #f7f9fc;
    border-radius: 4px;
    border-left: 3px solid #70A9DC;
    margin-bottom: 8px;
}
.gap-rem strong { color: #002F59; font-weight: 700; }
.gap-conf { font-size: 11px; color: #90a0b0; margin-top: 6px; }

/* ── Upload file card ── */
.file-pill {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    background: #ffffff;
    border: 1px solid #dce8f0;
    border-radius: 4px;
    padding: 10px 16px;
    font-size: 13px;
    color: #002F59;
    font-weight: 600;
    margin: 12px 0 20px;
    box-shadow: 0 1px 3px rgba(0,47,89,0.06);
}
.file-pill span { font-weight: 400; color: #708090; font-size: 12px; }

/* ── Corpus status table ── */
.corpus-row {
    display: flex;
    align-items: center;
    padding: 9px 0;
    border-bottom: 1px solid #eef2f6;
    font-size: 12.5px;
    gap: 12px;
}
.corpus-file { font-family: monospace; color: #334455; flex: 2; font-size: 12px; }
.corpus-reg  { color: #607080; flex: 4; font-size: 12px; }
.corpus-chunks { color: #002F59; font-weight: 600; flex: 1; text-align: center; }
.corpus-ok   { color: #5a8a00; font-weight: 700; font-size: 11px; letter-spacing: .5px; }
.corpus-miss { color: #B00020; font-weight: 700; font-size: 11px; letter-spacing: .5px; }

/* ── Sidebar status pills ── */
.status-pill {
    font-size: 11px;
    padding: 4px 10px;
    border-radius: 3px;
    font-weight: 600;
    letter-spacing: 0.3px;
}
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def scan_cache_path() -> Path:
    return session_dir() / "scan_cache.json"


if _SECRET_LOAD_ERROR:
    log_event("config.warning", "startup", rule="secrets_unavailable",
              detail=_SECRET_LOAD_ERROR)
if not SCANNER_READY:
    log_event("config.warning", "startup", rule="scanner_not_ready",
              detail="OPENROUTER_API_KEY is not set")


def _file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_cache() -> dict:
    """Session cache, falling back to the read-only committed baseline."""
    merged: dict = {}
    for path in (BASELINE_DIR / "scan_cache.json", scan_cache_path()):
        if path.exists():
            try:
                merged.update(json.loads(path.read_text()))
            except Exception:
                continue
    return merged


def _save_cache(cache: dict) -> None:
    # Write to a temp file then replace, so a concurrent reader never sees a
    # half-written file.
    path = scan_cache_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, indent=2))
    tmp.replace(path)


def _cached_scan(file_hash: str):
    """Return SOPScanResult for this hash if cached, else None."""
    cache = _load_cache()
    entry = cache.get(file_hash)
    if not entry:
        return None
    from src.schemas import SOPScanResult
    for base in (session_dir(), BASELINE_DIR):
        per_sop_path = base / f"gap_registry_{entry['sop_id']}.json"
        if not per_sop_path.exists():
            continue
        try:
            return SOPScanResult.model_validate(json.loads(per_sop_path.read_text()))
        except Exception:
            continue
    return None


def _write_cache(file_hash: str, sop_id: str, sop_file: str) -> None:
    cache = _load_cache()
    cache[file_hash] = {"sop_id": sop_id, "sop_file": sop_file}
    _save_cache(cache)


def _rebuild_master_registry() -> tuple[bool, str]:
    """Merge all per-SOP gap_registry_*.json files into gap_registry.json.

    scan_sop() (single-SOP CLI mode) only writes the per-SOP file, never the
    master registry.  This helper bridges that gap so the report generator
    always has a valid input file.

    Returns (ok, detail).  A per-SOP file that fails validation is skipped but
    named in `detail` — silently dropping it used to leave the master registry
    unwritten, which made the report step fail with no visible cause.
    """
    from datetime import datetime, timezone
    from src.schemas import GapRegistry, SOPScanResult

    # Session-scoped glob. Globbing all of output/ merged every visitor's
    # findings into one registry, so a client could download another's results.
    per_sop_files = sorted(session_dir().glob("gap_registry_*.json"))
    scans: list = []
    errors: list[str] = []
    for f in per_sop_files:
        try:
            scans.append(SOPScanResult.model_validate(json.loads(f.read_text())))
        except Exception as exc:
            errors.append(f"  {f.name}: {type(exc).__name__}: {exc}")

    if not scans:
        detail = "Could not build output/gap_registry.json — no valid per-SOP results.\n"
        if errors:
            detail += "\nPer-SOP files that failed to parse:\n" + "\n".join(errors)
        else:
            detail += (
                "\nNo gap_registry_<SOP_ID>.json files were found in output/. "
                "The detector did not write its results."
            )
        return False, detail

    findings = [f for s in scans for f in s.findings]
    versions = {s.guardrails_version for s in scans}
    registry = GapRegistry(
        registry_timestamp=datetime.now(timezone.utc).isoformat(),
        total_sops_scanned=len(scans),
        total_gaps_found=sum(s.gaps_found for s in scans),
        scans=scans,
        # Without these the master registry reports zero unscanned even when
        # the per-SOP files say otherwise, and the DOCX inherits that.
        total_unscanned_clauses=sum(s.unscanned_count for s in scans),
        total_flagged_findings=sum(1 for f in findings if f.is_flagged),
        total_verified_findings=sum(1 for f in findings if not f.is_flagged),
        guardrails_version=(versions.pop() if len(versions) == 1 else None),
    )
    registry_path().write_text(registry.model_dump_json(indent=2))

    log_event(
        "registry.built", session_id(),
        per_sop_files=len(per_sop_files), total_sops=len(scans),
        total_gaps=registry.total_gaps_found,
        total_unscanned=registry.total_unscanned_clauses,
        total_flagged=registry.total_flagged_findings,
        registry_sha256=sha256_file(registry_path()),
        skipped_invalid=len(errors),
    )

    detail = f"Merged {len(scans)} per-SOP registry file(s)."
    if errors:
        detail += "\n\nSkipped (invalid):\n" + "\n".join(errors)
    return True, detail


def _generate_report() -> tuple[bool, str, Path | None]:
    """Render the DOCX report and return (ok, log, path).

    Writes to an explicit --output path and verifies that exact file, rather
    than globbing for the newest gap_report_*.docx — a glob silently returns a
    stale report from an earlier run when the current render fails.
    """
    # Session id AND a timestamp in the name: a date-only filename meant every
    # visitor on a given day read and wrote the same file.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = session_dir() / f"gap_report_{session_id()}_{stamp}.docx"
    before = target.stat().st_mtime if target.exists() else None

    rc, out = run_cmd([
        "-m", "src.report.generator",
        "--registry", str(registry_path()),
        "--output", str(target),
    ], timeout=REPORT_TIMEOUT_S)
    if rc != 0:
        return False, out or "Report generator exited non-zero with no output.", None
    if not target.exists():
        return False, f"{out}\n\nGenerator exited 0 but {target.name} was not written.", None
    if before is not None and target.stat().st_mtime == before:
        return False, f"{out}\n\n{target.name} was not updated by this run (stale file).", None

    log_event(
        "report.generated", session_id(),
        report_filename=target.name,
        report_sha256=sha256_file(target),
        report_bytes=target.stat().st_size,
        registry_sha256=sha256_file(registry_path()),
    )
    return True, out, target


def child_env(secrets: bool = True, **extra: str) -> dict:
    """Environment for a child process.

    `secrets=False` drops the API keys entirely — used for the pytest child,
    which only reads output/ and has no reason to hold a credential.
    """
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"        # also makes the live scan log actually live
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if not secrets:
        for key in SECRET_ENV_KEYS:
            env.pop(key, None)
    env.update(extra)
    return env


def run_cmd(
    cmd: list[str],
    timeout: float = REPORT_TIMEOUT_S,
    secrets: bool = True,
    **env_extra: str,
) -> tuple[int, str]:
    try:
        r = subprocess.run(
            [sys.executable] + cmd,
            capture_output=True, text=True, cwd=str(BASE),
            timeout=timeout, env=child_env(secrets, **env_extra),
        )
    except subprocess.TimeoutExpired:
        # subprocess.run kills the child before raising.
        return 124, f"Timed out after {timeout:.0f}s and was terminated."
    return r.returncode, redact(r.stdout + r.stderr)


def run_cmd_stream(
    cmd: list[str],
    status_placeholder,
    log_placeholder,
    timeout: float = SCAN_TIMEOUT_S,
) -> tuple[int, str, list[tuple[str, str]]]:
    """Run a subprocess, streaming INFO/WARNING/ERROR lines to the UI.

    Returns (returncode, full_log, visible_lines).

    The previous implementation iterated proc.stdout directly, which blocks
    forever if the child hangs — there was no deadline and no way to kill it.
    A reader thread plus a queue keeps the deadline checkable even while the
    child is silent.
    """
    import re

    proc = subprocess.Popen(
        [sys.executable] + cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(BASE),
        env=child_env(True),
        # Own process group so a hung child's whole tree can be signalled.
        start_new_session=(os.name == "posix"),
    )

    q: queue.Queue = queue.Queue()
    SENTINEL = object()

    def _drain() -> None:
        try:
            for raw in proc.stdout:          # type: ignore[union-attr]
                q.put(raw.rstrip())
        except Exception:
            pass
        finally:
            q.put(SENTINEL)

    threading.Thread(target=_drain, daemon=True).start()

    lines: list[str] = []
    visible_lines: list[tuple[str, str]] = []
    STATUS_RE = re.compile(r"(INFO|WARNING|ERROR)\s+(.+)")
    deadline = time.monotonic() + timeout
    timed_out = False

    while True:
        if time.monotonic() > deadline:
            timed_out = True
            break
        try:
            item = q.get(timeout=1.0)
        except queue.Empty:
            continue
        if item is SENTINEL:
            break

        line = redact(item)
        lines.append(line)
        m = STATUS_RE.search(line)
        if not m:
            continue
        level, msg = m.group(1), m.group(2)
        if "HTTP Request" in msg:
            continue
        visible_lines.append((level, msg))
        colour = {"ERROR": "#B00020", "WARNING": "#B06A00"}.get(level, "#002F59")
        status_placeholder.markdown(
            f'<div style="font-size:13px;color:{colour};font-weight:600;">'
            f'{safe_html(msg)}</div>',
            unsafe_allow_html=True,
        )
        log_md = "\n".join(f"{'[' + lv + ']':12s} {ln}" for lv, ln in visible_lines[-20:])
        st_code(log_placeholder, log_md)

    if timed_out:
        _kill_process_tree(proc)
        lines.append(f"[guardrail] scan exceeded {timeout:.0f}s and was terminated")
        visible_lines.append(("ERROR", f"Scan exceeded {timeout:.0f}s and was terminated"))
        return 124, "\n".join(lines), visible_lines

    proc.wait()
    return proc.returncode, "\n".join(lines), visible_lines


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """SIGTERM the process group, then SIGKILL anything still alive."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
        return
    except Exception:
        pass
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        pass


def load_registry():
    path = registry_path()
    if not path.exists():
        return None
    try:
        from src.schemas import GapRegistry
        return GapRegistry.model_validate(json.loads(path.read_text()))
    except Exception:
        return None


# report_path() deliberately removed. It globbed output/ for the newest DOCX
# and handed any visitor the last report generated by anyone. Callers now read
# the report bytes from session state only.


def gap_card(i: int, f) -> str:
    """Render one finding.

    Every model-supplied value is escaped. This block is rendered with
    unsafe_allow_html=True, so a crafted SOP that steers the model into
    emitting markup would otherwise execute in the presenter's browser.
    """
    from src.gap_engine.guardrails import FLAG_LABELS

    sev = f.severity.value
    flagged = getattr(f, "is_flagged", False)
    flags = list(getattr(f, "verification_flags", []) or [])

    badge = ""
    note = ""
    if flagged:
        badge = '<span class="gap-badge badge-FLAG">REQUIRES VERIFICATION</span>'
        reasons = " ".join(FLAG_LABELS.get(x, x) for x in flags)
        note = f'<p class="flag-note">{safe_html(reasons)}</p>'

    source = getattr(f, "grounded_source_file", None)
    score = getattr(f, "grounding_score", None)
    if source and score is not None:
        trace = (f'<p class="gap-trace">Citation traced to '
                 f'{safe_html(source)} ({score:.0%} match)</p>')
    elif score is not None:
        trace = ('<p class="gap-trace flag">Citation NOT traced to the '
                 'retrieved corpus</p>')
    else:
        trace = ""

    return f"""
    <div class="gap-card {sev.lower()}{' flagged' if flagged else ''}">
        <div class="gap-header">
            <span class="gap-badge badge-{sev}">{sev}</span>
            {badge}
            <span class="gap-clause">{safe_html(f.sop_clause)}</span>
            <span class="gap-reg">{safe_html(f.regulation_ref)}</span>
        </div>
        <p class="gap-desc">{safe_html(f.gap_description)}</p>
        <div class="gap-rem"><strong>Remediation &mdash;</strong> {safe_html(f.remediation)}</div>
        <p class="gap-conf">Model confidence: {f.confidence:.0%}</p>
        {trace}
        {note}
    </div>"""


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("""
    <div style="background:#001e3c;padding:18px 20px;margin:-1rem -1rem 0;
                border-bottom:1px solid rgba(112,169,220,0.25);">
        <div style="font-size:9px;font-weight:600;letter-spacing:2.5px;
                    text-transform:uppercase;color:#70A9DC;">
            SOP Compliance Engine
        </div>
    </div>""", unsafe_allow_html=True)

    st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

    page = st.radio("nav", [
        "Scan SOP",
        "Gap Report",
        "Run Tests",
        "Audit Trail",
    ], label_visibility="collapsed")



# ---------------------------------------------------------------------------
# Top header
# ---------------------------------------------------------------------------

st.markdown("""
<div class="app-header">
    <div class="app-header-eyebrow">Regulatory Affairs &nbsp;&middot;&nbsp; Biologics</div>
    <div class="app-header-title">SOP Compliance Gap Analysis Engine</div>
</div>""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Page: Scan SOP
# ---------------------------------------------------------------------------

if page == "Scan SOP":
    # ── Persist scan result across Streamlit reruns ──────────────────────
    if "scan_result"    not in st.session_state:
        st.session_state.scan_result    = None
    if "scan_file_name" not in st.session_state:
        st.session_state.scan_file_name = None
    if "report_bytes"   not in st.session_state:
        st.session_state.report_bytes   = None
    if "report_name"    not in st.session_state:
        st.session_state.report_name    = None
    if "report_error"   not in st.session_state:
        st.session_state.report_error   = None
    if "scan_log"       not in st.session_state:
        st.session_state.scan_log       = []
    if "scans_run"      not in st.session_state:
        st.session_state.scans_run      = 0

    st.markdown('<p class="page-title">Scan SOP for Compliance Gaps</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="page-desc">Upload a Word or PDF SOP document. The engine parses it '
        'into clauses, retrieves the most relevant regulatory context via semantic search, '
        'and calls Claude to identify compliance gaps with severity and remediation guidance.</p>',
        unsafe_allow_html=True,
    )

    st.markdown('<p class="section-label">Upload SOP Document</p>', unsafe_allow_html=True)
    uploaded = st.file_uploader(
        "Accepts .docx or .pdf",
        type=["docx", "pdf"],
        label_visibility="collapsed",
    )

    if uploaded is not None:
        file_bytes = uploaded.getvalue()
        size_kb    = round(len(file_bytes) / 1024, 1)
        fhash      = _file_hash(file_bytes)

        # Clear previous results when a different file is uploaded
        if uploaded.name != st.session_state.scan_file_name:
            st.session_state.scan_result    = None
            st.session_state.report_bytes   = None
            st.session_state.report_name    = None
            st.session_state.report_error   = None
            st.session_state.scan_log       = []

        # ── Guardrail: validate BEFORE anything is written to disk ──────
        decision = validate_upload(uploaded.name, file_bytes)

        log_event(
            "upload.received", session_id(),
            filename_original=uploaded.name[:200],
            filename_sanitised=decision.safe_name,
            size_bytes=len(file_bytes), sha256=fhash,
            declared_ext=Path(uploaded.name).suffix.lower().lstrip("."),
            sniffed_type=decision.sniffed_type,
        )

        if not decision.ok:
            v = decision.blocking[0]
            log_event(
                "upload.rejected", session_id(),
                filename_sanitised=decision.safe_name, sha256=fhash,
                rule=v.rule, detail=v.detail,
            )
            log_event(
                "guardrail.violation", session_id(),
                rule=v.rule, detail=v.detail, action=v.action,
            )
            st.markdown(
                f'<div class="reject-pill"><strong>Upload rejected</strong><br>'
                f'{safe_html(v.message)}</div>',
                unsafe_allow_html=True,
            )
            with st.expander("Why was this rejected?"):
                st.write(f"**Rule:** `{v.rule}`")
                if v.detail:
                    st.write(f"**Detail:** {safe_html(v.detail)}")
                st.caption(
                    "Files are checked by their actual contents, not their "
                    "extension. Nothing was written to disk."
                )
        else:
            # Uploads land in a per-session directory, never in sops/ itself,
            # so the bundled demo SOPs cannot be overwritten.
            save_path = session_upload_dir() / decision.safe_name
            assert_contained(save_path, UPLOAD_ROOT)
            save_path.write_bytes(file_bytes)

            clause_count = count_clauses(save_path)
            capped = max(0, clause_count - MAX_CLAUSES_SCANNED)

            log_event(
                "upload.accepted", session_id(),
                filename_sanitised=decision.safe_name, sha256=fhash,
                sniffed_type=decision.sniffed_type,
                pages_or_paragraphs=decision.unit_count,
                clause_count=clause_count,
                clauses_truncated_to=MAX_CLAUSES_SCANNED if capped else None,
                stored_path=str(save_path.relative_to(BASE)),
            )
            if capped:
                log_event(
                    "guardrail.violation", session_id(),
                    rule="clause_cap", action="truncated",
                    detail=f"{clause_count} clauses, analysing {MAX_CLAUSES_SCANNED}",
                )

            cached_scan = _cached_scan(fhash)
            cache_hit   = cached_scan is not None

            pill_suffix = (
                "Cached &nbsp;&middot;&nbsp; results available instantly"
                if cache_hit else f"{clause_count} clause(s) &nbsp;&middot;&nbsp; ready to scan"
            )
            st.markdown(f"""
            <div class="file-pill">
                {safe_html(decision.safe_name)}
                <span>{size_kb} KB &nbsp;&middot;&nbsp; {pill_suffix}</span>
            </div>""", unsafe_allow_html=True)

            if decision.renamed:
                st.caption(
                    f"Saved as {decision.safe_name} — the uploaded name contained "
                    f"characters that were removed."
                )
            if clause_count == 0:
                st.warning(
                    "No numbered clauses were found in this document. The engine "
                    "needs sections such as \"7.3 Comparability\". Scanning would "
                    "return no findings."
                )
            if capped:
                st.warning(
                    f"This SOP has {clause_count} clauses. Scanning the first "
                    f"{MAX_CLAUSES_SCANNED}; the remaining {capped} will be "
                    f"recorded as UNSCANNED."
                )

            scans_run = st.session_state.get("scans_run", 0)
            over_quota = scans_run >= MAX_SCANS_PER_SESSION and not cache_hit

            if not SCANNER_READY:
                st.error(
                    "Scanner unavailable — OPENROUTER_API_KEY is not configured "
                    "for this deployment."
                )
            elif over_quota:
                st.warning(
                    f"You have run {scans_run} scans in this session (limit "
                    f"{MAX_SCANS_PER_SESSION}). Reload the page to start a new one."
                )
            elif clause_count == 0:
                pass
            elif st.button("Run Compliance Scan", type="primary"):
                scan, rc, out = None, 0, ""

                if cache_hit:
                    scan = cached_scan
                    st.session_state.scan_log = []
                    log_event(
                        "scan.started", session_id(),
                        sop_file=decision.safe_name, file_sha256=fhash,
                        cache_hit=True,
                    )
                elif not _SCAN_SEMAPHORE.acquire(blocking=False):
                    st.warning(
                        f"{MAX_CONCURRENT_SCANS} scans are already running. "
                        f"Try again in a moment."
                    )
                    rc = -1
                else:
                    from src.gap_engine.prompts import (
                        EMBEDDING_MODEL, MIN_RELEVANCE_SCORE, MODEL,
                        SYSTEM_PROMPT_SHA, TOP_K, USER_PROMPT_SHA,
                    )
                    log_event(
                        "scan.started", session_id(),
                        sop_file=decision.safe_name, file_sha256=fhash,
                        cache_hit=False, model=MODEL,
                        embedding_model=EMBEDDING_MODEL, temperature=0,
                        top_k=TOP_K, min_relevance_score=MIN_RELEVANCE_SCORE,
                        system_prompt_sha256=SYSTEM_PROMPT_SHA,
                        user_prompt_template_sha256=USER_PROMPT_SHA,
                        estimated_llm_calls=min(clause_count, MAX_CLAUSES_SCANNED),
                    )
                    _scan_started_at = time.monotonic()
                    try:
                        st.markdown('<p class="section-label">Scan Progress</p>',
                                    unsafe_allow_html=True)
                        status_box = st.empty()
                        status_box.markdown(
                            '<div style="font-size:13px;color:#002F59;font-weight:600;">'
                            'Starting scan…</div>',
                            unsafe_allow_html=True,
                        )
                        log_box = st.empty()

                        rc, out, visible = run_cmd_stream(
                            ["-m", "src.gap_engine.detector",
                             "--sop", str(save_path),
                             "--out-dir", str(session_dir()),
                             "--max-clauses", str(MAX_CLAUSES_SCANNED)],
                            status_box, log_box,
                        )
                        status_box.empty()
                        log_box.empty()
                        # The log used to be discarded here, taking every
                        # WARNING the detector emitted with it.
                        st.session_state.scan_log = visible
                        st.session_state.scans_run = scans_run + 1
                    finally:
                        _SCAN_SEMAPHORE.release()

                    if rc == 0:
                        from src.schemas import SOPScanResult as _SSR
                        for candidate in sorted(session_dir().glob("gap_registry_*.json")):
                            try:
                                cand = _SSR.model_validate(json.loads(candidate.read_text()))
                            except Exception:
                                continue
                            if cand.sop_file == decision.safe_name:
                                scan = cand
                                break
                        if scan:
                            _write_cache(fhash, scan.sop_id, scan.sop_file)

                    log_event(
                        "scan.finished", session_id(),
                        sop_file=decision.safe_name, file_sha256=fhash, rc=rc,
                        duration_s=round(time.monotonic() - _scan_started_at, 1),
                        timed_out=(rc == 124),
                        clauses_scanned=getattr(scan, "total_clauses_scanned", 0),
                        clauses_analysed=getattr(scan, "analysed", 0),
                        unscanned=getattr(scan, "unscanned_count", 0),
                        flagged=getattr(scan, "flagged_findings_count", 0),
                        gaps_found=getattr(scan, "gaps_found", 0),
                        llm_calls=getattr(scan, "llm_calls", 0),
                        severity_counts={
                            sev: sum(1 for f in getattr(scan, "findings", [])
                                     if f.severity.value == sev)
                            for sev in ("CRITICAL", "MAJOR", "MINOR")
                        },
                        error_counts=getattr(scan, "error_counts", {}),
                    )

                if rc == 0 and scan:
                    st.session_state.scan_result    = scan
                    st.session_state.scan_file_name = uploaded.name
                    st.session_state.report_bytes   = None
                    st.session_state.report_name    = None
                    st.session_state.report_error   = None

                    built, detail = _rebuild_master_registry()
                    if not built:
                        st.session_state.report_error = detail
                    else:
                        gen_ok, gen_log, rpt = _generate_report()
                        if gen_ok and rpt:
                            st.session_state.report_bytes = rpt.read_bytes()
                            st.session_state.report_name  = rpt.name
                        else:
                            st.session_state.report_error = gen_log
                elif rc == 124:
                    st.error(
                        f"Scan exceeded the {SCAN_TIMEOUT_S:.0f}s limit and was "
                        f"terminated. No results were produced."
                    )
                    st_code(st, out)
                elif rc not in (0, -1):
                    st.error("Scan failed.")
                    st_code(st, out)
                elif rc == 0:
                    # Exit 0 but no matching result file. This used to fall
                    # through silently: no results, no report, no error.
                    st.error(
                        f"Scan finished but produced no results for "
                        f"{decision.safe_name}. The detector did not write a "
                        f"matching gap_registry_<SOP_ID>.json."
                    )
                    st_code(st, out or "(no output captured)")

    # ── Results — rendered from session state, survive all reruns ────────
    scan = st.session_state.scan_result
    if scan:
        crit  = sum(1 for f in scan.findings if f.severity.value == "CRITICAL")
        maj   = sum(1 for f in scan.findings if f.severity.value == "MAJOR")
        minor = sum(1 for f in scan.findings if f.severity.value == "MINOR")

        st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)
        st.markdown('<p class="section-label">Scan Results</p>', unsafe_allow_html=True)

        unscanned = scan.unscanned_count
        flagged   = scan.flagged_findings_count
        attempted = scan.total_clauses_scanned

        # A scan where nothing could be analysed must never read as success.
        if attempted and unscanned >= attempted:
            st.error(
                f"Scan produced no analysable results — all {unscanned} clause(s) "
                f"are UNSCANNED. This is NOT a clean compliance result."
            )
        elif crit > 0:
            st.error(f"**{scan.gaps_found} gap(s) identified** — {crit} critical finding(s) "
                     f"require immediate attention.")
        else:
            st.success(f"Scan complete — {scan.gaps_found} gap(s) identified.")

        if 0 < unscanned < attempted:
            st.warning(
                f"Coverage is incomplete: {unscanned} clause(s) could not be "
                f"scanned. Their absence from the findings is not evidence of "
                f"compliance."
            )
        if flagged:
            st.info(
                f"{flagged} finding(s) could not be verified against the "
                f"regulatory corpus and are marked for human review."
            )

        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Gaps", scan.gaps_found)
        c2.metric("Critical",   crit)
        c3.metric("Major",      maj)
        c4.metric("Minor",      minor)

        d1, d2, d3 = st.columns(3)
        d1.metric("Clauses Analysed", f"{scan.analysed}/{attempted}")
        d2.metric("Unscanned",        unscanned)
        d3.metric("Need Verification", flagged)

        if scan.unscanned_clauses:
            with st.expander(f"Unscanned clauses ({unscanned}) — not assessed"):
                st.caption(
                    "These clauses were parsed but could not be analysed. Each "
                    "must be reviewed manually."
                )
                st.table([
                    {"Clause": u.section_id, "Heading": u.heading,
                     "Reason": u.reason.value, "Detail": u.detail[:80],
                     "Attempts": u.attempts}
                    for u in scan.unscanned_clauses
                ])

        log = st.session_state.get("scan_log") or []
        if log:
            warns = sum(1 for lv, _ in log if lv == "WARNING")
            errs  = sum(1 for lv, _ in log if lv == "ERROR")
            with st.expander(f"Scan log ({warns} warning(s), {errs} error(s))",
                             expanded=bool(errs)):
                st_code(st, "\n".join(f"[{lv}] {msg}" for lv, msg in log))

        # ── Download report — pinned here, no page navigation needed ──
        st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)
        st.markdown('<p class="section-label">Audit Report</p>', unsafe_allow_html=True)
        if st.session_state.report_bytes:
            st.download_button(
                label="Download Audit Report (.docx)",
                data=st.session_state.report_bytes,
                file_name=st.session_state.report_name,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        else:
            st.error("Report generation failed.")
            if st.session_state.report_error:
                st_code(st, st.session_state.report_error)

        # ── Gap finding cards ──────────────────────────────────────────
        st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)
        st.markdown('<p class="section-label">Gap Findings</p>', unsafe_allow_html=True)

        SEV_ORDER = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2}
        for i, f in enumerate(
            sorted(scan.findings,
                   key=lambda x: (SEV_ORDER[x.severity.value], x.sop_clause)), 1
        ):
            st.markdown(gap_card(i, f), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Page: Gap Report
# ---------------------------------------------------------------------------

elif page == "Gap Report":
    st.markdown('<p class="page-title">Gap Report</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="page-desc">Generate and download the audit-ready DOCX report '
        'compiled from the latest gap registry.</p>',
        unsafe_allow_html=True,
    )

    # Resolve report bytes: prefer session state (works on cloud / ephemeral FS),
    # fall back to the file on disk from a previous run.
    # Session state only. The previous disk fallback globbed output/ for the
    # newest DOCX, which served any visitor the last report anyone generated.
    _rpt_bytes = st.session_state.get("report_bytes")
    _rpt_name  = st.session_state.get("report_name")

    # Resolve registry: session state scan + disk registry
    _registry  = load_registry()
    _ss_scan   = st.session_state.get("scan_result")
    _has_data  = bool(_registry or _ss_scan)

    if not _has_data:
        st.warning("No gap registry found. Upload and scan an SOP first.")
    else:
        col_btn, col_dl = st.columns([1, 2])
        with col_btn:
            if st.button("Generate Report", type="primary"):
                with st.spinner("Rendering report…"):
                    # Rebuild first: the master registry may be missing when the
                    # only results came from a single-SOP scan this session.
                    built, detail = _rebuild_master_registry()
                    gen_ok, gen_log, rpt = (
                        _generate_report() if built else (False, detail, None)
                    )
                if gen_ok and rpt:
                    st.session_state.report_bytes = rpt.read_bytes()
                    st.session_state.report_name  = rpt.name
                    st.session_state.report_error = None
                    _rpt_bytes = st.session_state.report_bytes
                    _rpt_name  = st.session_state.report_name
                    st.success("Report generated.")
                else:
                    st.session_state.report_error = gen_log
                    st.error("Report generation failed.")
                    st_code(st, gen_log)

        if _rpt_bytes:
            with col_dl:
                st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
                st.download_button(
                    label="Download Audit Report (.docx)",
                    data=_rpt_bytes,
                    file_name=_rpt_name or "gap_report.docx",
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )

        # Summary table — merge disk registry with any in-session scan
        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
        st.markdown('<p class="section-label">Report Summary</p>', unsafe_allow_html=True)

        scans = list(_registry.scans) if _registry else []
        # Add the in-session scan if it isn't already in the disk registry
        if _ss_scan and not any(s.sop_id == _ss_scan.sop_id for s in scans):
            scans.append(_ss_scan)

        if scans:
            data = []
            for s in sorted(scans, key=lambda x: x.sop_id):
                crit  = sum(1 for f in s.findings if f.severity.value == "CRITICAL")
                maj   = sum(1 for f in s.findings if f.severity.value == "MAJOR")
                minor = sum(1 for f in s.findings if f.severity.value == "MINOR")
                data.append({
                    "SOP ID":          s.sop_id,
                    "File":            s.sop_file,
                    "Clauses Scanned": s.total_clauses_scanned,
                    "Analysed":        s.analysed,
                    "Total Gaps":      s.gaps_found,
                    "Critical":        crit,
                    "Major":           maj,
                    "Minor":           minor,
                    "Unscanned":       s.unscanned_count,
                    "Flagged":         s.flagged_findings_count,
                })
            st.table(data)


# ---------------------------------------------------------------------------
# Page: Run Tests
# ---------------------------------------------------------------------------

elif page == "Run Tests":
    st.markdown('<p class="page-title">Pre-Demo Verification</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="page-desc">Runs the automated smoke test suite to verify all '
        '4 pre-scripted critical gaps fire with model confidence at or above 0.80. '
        'Execute this the night before every demo.</p>',
        unsafe_allow_html=True,
    )

    # Fail-closed: the only endpoint on this app that spawns compute for an
    # unauthenticated visitor. Unset env var means the page is inert.
    TESTS_ENABLED = os.environ.get("ENABLE_TEST_PAGE", "").strip() in ("1", "true", "True")

    if not TESTS_ENABLED:
        st.info(
            "Test execution is disabled in this deployment. Set ENABLE_TEST_PAGE=1 "
            "to enable it."
        )
    else:
        last = st.session_state.get("last_test_run", 0.0)
        wait = TEST_COOLDOWN_S - (time.time() - last)

        if wait > 0:
            st.warning(f"Please wait {wait:.0f}s before running the suite again.")
        elif st.button("Run Test Suite", type="primary"):
            if not _TEST_SEMAPHORE.acquire(blocking=False):
                st.warning("A test run is already in progress.")
            else:
                try:
                    with st.spinner("Running pytest…"):
                        rc, out = run_cmd(
                            ["-m", "pytest", "tests/", "-v", "--tb=short", "--no-header"],
                            timeout=PYTEST_TIMEOUT_S,
                            # The suite only reads output/. It has no reason to
                            # hold an API key.
                            secrets=False,
                            # Turns a missing baseline registry into a failure
                            # instead of a skip that exits 0 and renders green.
                            REQUIRE_BASELINE="1",
                        )
                    st.session_state.last_test_run = time.time()
                finally:
                    _TEST_SEMAPHORE.release()

                passed = failed = skipped = 0
                m = re.search(r"(\d+) passed", out)
                if m: passed = int(m.group(1))
                m = re.search(r"(\d+) failed", out)
                if m: failed = int(m.group(1))
                m = re.search(r"(\d+) skipped", out)
                if m: skipped = int(m.group(1))

                log_event(
                    "tests.run", session_id(),
                    rc=rc, passed=passed, failed=failed, skipped=skipped,
                )

                t1, t2, t3 = st.columns(3)
                t1.metric("Passed", passed)
                t2.metric("Failed", failed)
                t3.metric("Skipped", skipped)

                if rc == 124:
                    st.error(f"Test run exceeded {PYTEST_TIMEOUT_S:.0f}s and was terminated.")
                elif failed or rc != 0:
                    st.error("One or more checks failed — review output below.")
                elif passed == 0:
                    # Exit code 0 with nothing executed used to render as
                    # "All pre-demo checks passed."
                    st.error(
                        "No checks actually ran. This is not a pass — the "
                        "baseline registry is probably missing."
                    )
                else:
                    st.success(f"All {passed} pre-demo checks passed.")
                st_code(st, out)


# ---------------------------------------------------------------------------
# Page: Audit Trail
# ---------------------------------------------------------------------------

elif page == "Audit Trail":
    st.markdown('<p class="page-title">Audit Trail</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="page-desc">Append-only record of every upload, scan, registry '
        'build and report generated. Each report is recorded with the SHA-256 of '
        'the DOCX, the registry it was built from, and the prompt version in '
        'force, so a specific report can be traced back to a specific analysis.</p>',
        unsafe_allow_html=True,
    )

    rows = read_events(session_id())
    failures = write_failure_count()

    m1, m2, m3 = st.columns(3)
    m1.metric("Records this session", len(rows))
    m2.metric("Session ID", session_id())
    m3.metric("Write failures", failures)

    if failures:
        st.warning(
            f"{failures} audit record(s) could not be written. The log is "
            f"incomplete for this session."
        )

    st.info(
        "Retention: audit records live only for the lifetime of this deployment "
        "container. Streamlit Cloud's filesystem is ephemeral, so these records "
        "are not durable GxP evidence on their own — durable retention requires "
        "shipping them to external storage."
    )

    if not rows:
        st.caption("No activity recorded in this session yet.")
    else:
        st.markdown('<p class="section-label">This Session</p>', unsafe_allow_html=True)
        st.dataframe(
            [
                {
                    "Time": r.get("ts", "")[11:23],
                    "Event": r.get("event", ""),
                    "Detail": redact(json.dumps(
                        {k: v for k, v in r.items()
                         if k not in ("ts", "event", "session_id",
                                      "schema_version", "app_version")},
                        default=str,
                    ))[:300],
                }
                for r in rows
            ],
            width="stretch",
            hide_index=True,
        )

        with st.expander("Raw records (JSON)"):
            st_code(st, "\n".join(json.dumps(r, default=str) for r in rows[:50]),
                    language="json")

    # Full-day export is cross-session by nature, so it sits behind the same
    # switch as test execution.
    if os.environ.get("ENABLE_TEST_PAGE", "").strip() in ("1", "true", "True"):
        path = audit_path_for_today()
        if path.exists():
            st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)
            st.download_button(
                label=f"Download full audit log ({path.name})",
                data=path.read_bytes(),
                file_name=path.name,
                mime="application/x-ndjson",
            )
