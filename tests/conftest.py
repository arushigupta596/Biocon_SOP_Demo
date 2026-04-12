"""
Shared pytest fixtures for the Biocon SOP Compliance Engine test suite.

The gap_registry fixture loads output/gap_registry.json. This file is produced
by running the detector:
    python -m src.gap_engine.detector --all --sops sops/
"""
import json
from pathlib import Path

import pytest

from src.schemas import GapRegistry, GapResult


@pytest.fixture(scope="session")
def gap_registry() -> GapRegistry:
    registry_path = Path("output/gap_registry.json")
    if not registry_path.exists():
        pytest.skip(
            "output/gap_registry.json not found. "
            "Run: python -m src.gap_engine.detector --all --sops sops/"
        )
    data = json.loads(registry_path.read_text())
    return GapRegistry.model_validate(data)


@pytest.fixture(scope="session")
def all_findings(gap_registry: GapRegistry) -> list[GapResult]:
    findings: list[GapResult] = []
    for scan in gap_registry.scans:
        findings.extend(scan.findings)
    return findings
