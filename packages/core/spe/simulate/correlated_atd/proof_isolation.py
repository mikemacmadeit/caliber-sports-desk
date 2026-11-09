"""Proof-of-accuracy milestone isolation. No new models. No promotion."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from spe.simulate.correlated_atd.isolation import (
    assert_protected_unchanged,
    development_database_url,
    snapshot_protected,
)

PHASE = "prove_atd_accuracy"
PLAY_ENABLED = False
PROMOTED = False
RESEARCH_ONLY = True


def proof_dir() -> Path:
    path = Path("data") / "atd_sim_dev" / "proof"
    path.mkdir(parents=True, exist_ok=True)
    return path


def report_path() -> Path:
    return proof_dir() / "prove_atd_accuracy_report.json"


def banner() -> dict[str, Any]:
    return {
        "phase": PHASE,
        "research_only": RESEARCH_ONLY,
        "play_enabled": PLAY_ENABLED,
        "promoted": PROMOTED,
        "validated_play": False,
        "develop_new_models": False,
        "database_url": development_database_url(),
        "goal": "Prove calibration/accuracy with correctly labeled evidence; do not promote",
        "preserves": ["M3", "M4", "M5", "M6", "team_td_v1", "atd_sim_v1"],
    }


__all__ = [
    "PHASE",
    "PLAY_ENABLED",
    "PROMOTED",
    "RESEARCH_ONLY",
    "assert_protected_unchanged",
    "banner",
    "development_database_url",
    "proof_dir",
    "report_path",
    "snapshot_protected",
]
