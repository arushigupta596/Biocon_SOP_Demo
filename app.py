"""
Biocon SOP Compliance Engine — Streamlit Demo UI

Run:
    streamlit run app.py
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Streamlit Cloud: inject secrets into os.environ so child processes
# (detector, report generator) can read OPENROUTER_API_KEY at runtime.
# st.secrets is populated from the Streamlit Cloud dashboard; locally it
# falls back to .env via load_dotenv() above.
# ---------------------------------------------------------------------------
_SECRET_KEYS = ["OPENROUTER_API_KEY", "CHROMA_PATH"]
for _k in _SECRET_KEYS:
    try:
        if _k in st.secrets and not os.environ.get(_k):
            os.environ[_k] = st.secrets[_k]
    except Exception:
        pass

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
REGISTRY_PATH = OUTPUT_DIR / "gap_registry.json"
MANIFEST_PATH = OUTPUT_DIR / "corpus_manifest.json"

OUTPUT_DIR.mkdir(exist_ok=True)
SOPS_DIR.mkdir(exist_ok=True)

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

SCAN_CACHE_PATH = OUTPUT_DIR / "scan_cache.json"


def _file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_cache() -> dict:
    if SCAN_CACHE_PATH.exists():
        try:
            return json.loads(SCAN_CACHE_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_cache(cache: dict) -> None:
    SCAN_CACHE_PATH.write_text(json.dumps(cache, indent=2))


def _cached_scan(file_hash: str):
    """Return SOPScanResult for this hash if cached, else None."""
    cache = _load_cache()
    entry = cache.get(file_hash)
    if not entry:
        return None
    per_sop_path = OUTPUT_DIR / f"gap_registry_{entry['sop_id']}.json"
    if not per_sop_path.exists():
        return None
    try:
        from src.schemas import SOPScanResult
        return SOPScanResult.model_validate(json.loads(per_sop_path.read_text()))
    except Exception:
        return None


def _write_cache(file_hash: str, sop_id: str, sop_file: str) -> None:
    cache = _load_cache()
    cache[file_hash] = {"sop_id": sop_id, "sop_file": sop_file}
    _save_cache(cache)


def _rebuild_master_registry() -> None:
    """Merge all per-SOP gap_registry_*.json files into gap_registry.json.

    scan_sop() (single-SOP CLI mode) only writes the per-SOP file, never the
    master registry.  This helper bridges that gap so the report generator
    always has a valid input file.
    """
    from datetime import datetime, timezone
    from src.schemas import GapRegistry, SOPScanResult

    per_sop_files = sorted(OUTPUT_DIR.glob("gap_registry_*.json"))
    scans: list = []
    for f in per_sop_files:
        try:
            scans.append(SOPScanResult.model_validate(json.loads(f.read_text())))
        except Exception:
            pass

    if not scans:
        return

    registry = GapRegistry(
        registry_timestamp=datetime.now(timezone.utc).isoformat(),
        total_sops_scanned=len(scans),
        total_gaps_found=sum(s.gaps_found for s in scans),
        scans=scans,
    )
    REGISTRY_PATH.write_text(registry.model_dump_json(indent=2))


def run_cmd(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run([sys.executable] + cmd, capture_output=True, text=True, cwd=str(BASE))
    return r.returncode, r.stdout + r.stderr


def run_cmd_stream(cmd: list[str], status_placeholder, log_placeholder) -> tuple[int, str]:
    """Run a subprocess and stream INFO log lines as live status updates."""
    import re
    proc = subprocess.Popen(
        [sys.executable] + cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(BASE),
    )
    lines = []
    visible_lines = []  # lines shown to user (filtered)

    STATUS_RE = re.compile(r"(INFO|WARNING|ERROR)\s+(.+)")

    for raw in proc.stdout:
        line = raw.rstrip()
        lines.append(line)
        m = STATUS_RE.search(line)
        if m:
            level, msg = m.group(1), m.group(2)
            # Skip low-signal HTTP lines
            if "HTTP Request" in msg:
                continue
            visible_lines.append((level, msg))
            # Update live status with the latest meaningful message
            status_placeholder.markdown(
                f'<div style="font-size:13px;color:#002F59;font-weight:600;">'
                f'{msg}</div>',
                unsafe_allow_html=True,
            )
            # Show scrollable log
            log_md = "\n".join(
                f"{'[' + lv + ']':12s} {ln}" for lv, ln in visible_lines[-20:]
            )
            log_placeholder.code(log_md, language="text")

    proc.wait()
    return proc.returncode, "\n".join(lines)


def load_registry():
    if not REGISTRY_PATH.exists():
        return None
    try:
        from src.schemas import GapRegistry
        return GapRegistry.model_validate(json.loads(REGISTRY_PATH.read_text()))
    except Exception:
        return None


def report_path() -> Path | None:
    reports = sorted(OUTPUT_DIR.glob("gap_report_*.docx"), reverse=True)
    return reports[0] if reports else None


def gap_card(i: int, f) -> str:
    sev = f.severity.value
    return f"""
    <div class="gap-card {sev.lower()}">
        <div class="gap-header">
            <span class="gap-badge badge-{sev}">{sev}</span>
            <span class="gap-clause">{f.sop_clause}</span>
            <span class="gap-reg">{f.regulation_ref}</span>
        </div>
        <p class="gap-desc">{f.gap_description}</p>
        <div class="gap-rem"><strong>Remediation &mdash;</strong> {f.remediation}</div>
        <p class="gap-conf">Model confidence: {f.confidence:.0%}</p>
    </div>"""


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("""
    <div style="background:#001e3c;padding:24px 20px 20px;margin:-1rem -1rem 0;
                border-bottom:1px solid rgba(112,169,220,0.25);">
        <div style="font-size:17px;font-weight:800;color:#fff;letter-spacing:0.3px;">BIOCON</div>
        <div style="font-size:9px;font-weight:600;letter-spacing:2.5px;
                    text-transform:uppercase;color:#70A9DC;margin-top:3px;">
            SOP Compliance Engine
        </div>
    </div>""", unsafe_allow_html=True)

    st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

    page = st.radio("nav", [
        "Scan SOP",
        "Gap Report",
        "Run Tests",
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
        save_path  = SOPS_DIR / uploaded.name

        # Clear previous results when a different file is uploaded
        if uploaded.name != st.session_state.scan_file_name:
            st.session_state.scan_result    = None
            st.session_state.report_bytes   = None
            st.session_state.report_name    = None

        save_path.write_bytes(file_bytes)

        cached_scan = _cached_scan(fhash)
        cache_hit   = cached_scan is not None

        pill_suffix = "Cached &nbsp;&middot;&nbsp; results available instantly" if cache_hit else "Ready to scan"
        st.markdown(f"""
        <div class="file-pill">
            {uploaded.name}
            <span>{size_kb} KB &nbsp;&middot;&nbsp; {pill_suffix}</span>
        </div>""", unsafe_allow_html=True)

        if st.button("Run Compliance Scan", type="primary"):
            if cache_hit:
                scan = cached_scan
                rc   = 0
            else:
                st.markdown(
                    '<p class="section-label">Scan Progress</p>',
                    unsafe_allow_html=True,
                )
                status_box = st.empty()
                status_box.markdown(
                    '<div style="font-size:13px;color:#002F59;font-weight:600;">'
                    'Starting scan…</div>',
                    unsafe_allow_html=True,
                )
                log_box = st.empty()

                rc, out = run_cmd_stream(
                    ["-m", "src.gap_engine.detector", "--sop", str(save_path)],
                    status_box,
                    log_box,
                )
                status_box.empty()
                log_box.empty()

                # Resolve scan from the per-SOP registry file the detector wrote
                scan = None
                if rc == 0:
                    per_sop = OUTPUT_DIR / f"gap_registry_{save_path.stem.split('_')[0]}.json"
                    # More robust: find by filename in master, or load per-SOP directly
                    from src.schemas import SOPScanResult as _SSR
                    for candidate in sorted(OUTPUT_DIR.glob("gap_registry_*.json")):
                        try:
                            s = _SSR.model_validate(json.loads(candidate.read_text()))
                            if s.sop_file == uploaded.name:
                                scan = s
                                break
                        except Exception:
                            pass
                    if scan:
                        _write_cache(fhash, scan.sop_id, scan.sop_file)

            if rc == 0 and scan:
                # Rebuild master registry (scan_sop CLI never writes it) then generate report
                _rebuild_master_registry()
                run_cmd(["-m", "src.report.generator",
                         "--registry", "output/gap_registry.json"])
                rpt = report_path()
                st.session_state.scan_result    = scan
                st.session_state.scan_file_name = uploaded.name
                st.session_state.report_bytes   = rpt.read_bytes() if rpt else None
                st.session_state.report_name    = rpt.name if rpt else None
            elif rc != 0:
                st.error("Scan failed.")
                st.code(out, language="text")

    # ── Results — rendered from session state, survive all reruns ────────
    scan = st.session_state.scan_result
    if scan:
        crit  = sum(1 for f in scan.findings if f.severity.value == "CRITICAL")
        maj   = sum(1 for f in scan.findings if f.severity.value == "MAJOR")
        minor = sum(1 for f in scan.findings if f.severity.value == "MINOR")

        st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)
        st.markdown('<p class="section-label">Scan Results</p>', unsafe_allow_html=True)

        if crit > 0:
            st.error(f"**{scan.gaps_found} gap(s) identified** — {crit} critical finding(s) "
                     f"require immediate attention.")
        else:
            st.success(f"Scan complete — {scan.gaps_found} gap(s) identified.")

        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Gaps", scan.gaps_found)
        c2.metric("Critical",   crit)
        c3.metric("Major",      maj)
        c4.metric("Minor",      minor)

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
            st.caption("Report generation failed — run from the Gap Report page.")

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
    _rpt_bytes = st.session_state.get("report_bytes")
    _rpt_name  = st.session_state.get("report_name")
    _rpt_disk  = report_path()
    if not _rpt_bytes and _rpt_disk:
        _rpt_bytes = _rpt_disk.read_bytes()
        _rpt_name  = _rpt_disk.name

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
                    rc, out = run_cmd(["-m", "src.report.generator",
                                       "--registry", "output/gap_registry.json"])
                if rc == 0:
                    rpt = report_path()
                    if rpt:
                        st.session_state.report_bytes = rpt.read_bytes()
                        st.session_state.report_name  = rpt.name
                        _rpt_bytes = st.session_state.report_bytes
                        _rpt_name  = st.session_state.report_name
                    st.success("Report generated.")
                else:
                    st.error("Report generation failed.")
                    st.code(out, language="text")

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
                    "Total Gaps":      s.gaps_found,
                    "Critical":        crit,
                    "Major":           maj,
                    "Minor":           minor,
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

    if st.button("Run Test Suite", type="primary"):
        with st.spinner("Running pytest…"):
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "tests/test_demo_gaps.py",
                 "-v", "--tb=short", "--no-header"],
                capture_output=True, text=True, cwd=str(BASE),
            )
        if result.returncode == 0:
            st.success("All pre-demo checks passed.")
        else:
            st.error("One or more checks failed — review output below.")
        st.code(result.stdout + result.stderr, language="text")
