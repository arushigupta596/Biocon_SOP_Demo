"""
Shared pytest fixtures for the Biocon SOP Compliance Engine test suite.

The gap_registry fixture reads output/baseline/gap_registry.json — a committed
baseline produced by:

    python -m src.gap_engine.detector --all --sops sops/ --out-dir output/baseline

It deliberately does NOT read output/sessions/, which is where the Streamlit app
writes. Client uploads therefore cannot influence the pre-demo test result.

Set REQUIRE_BASELINE=1 to turn a missing baseline into a failure rather than a
skip. The app sets it, because a skipped suite exits 0 and would otherwise
render as "all pre-demo checks passed" having verified nothing.
"""
import json
import os
from pathlib import Path

import pytest

from src.schemas import GapRegistry, GapResult

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE = REPO_ROOT / "output" / "baseline" / "gap_registry.json"

DEMO_SOP_IDS = {
    "BC-MFG-UC-047",
    "BC-QC-BR-012",
    "BC-RA-IM-008",
    "BC-AN-MV-031",
}


@pytest.fixture(scope="session")
def registry_path() -> Path:
    return Path(os.environ.get("GAP_REGISTRY_PATH", BASELINE))


@pytest.fixture(scope="session")
def gap_registry(registry_path: Path) -> GapRegistry:
    if not registry_path.exists():
        message = (
            f"{registry_path} not found. Build the baseline with:\n"
            "  python -m src.gap_engine.detector --all --sops sops/ "
            "--out-dir output/baseline"
        )
        if os.environ.get("REQUIRE_BASELINE") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return GapRegistry.model_validate(json.loads(registry_path.read_text()))


@pytest.fixture(scope="session")
def all_findings(gap_registry: GapRegistry) -> list[GapResult]:
    findings: list[GapResult] = []
    for scan in gap_registry.scans:
        findings.extend(scan.findings)
    return findings
