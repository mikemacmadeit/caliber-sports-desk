"""Isolation for Phase 4 correlated Anytime TD simulation research."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from spe.config import settings

VERSION = "atd_sim_v1"
PLAY_ENABLED = False
PROMOTED = False
RESEARCH_ONLY = True

PROTECTED_GLOBS = (
    "*_m3.json",
    "*_m4.json",
    "*_m5.json",
    "*_m6_player_td.json",
    "*_team_td_v1.json",
    "*_player_td_v1.json",
    "m4_comparison.json",
    "m5_comparison.json",
)


def artifact_dir() -> Path:
    path = Path(settings.parquet_dir) / "models" / "atd_sim"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def report_path() -> Path:
    path = Path(settings.parquet_dir).parent / "atd_sim_dev" / "phase4_atd_sim_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def examples_path() -> Path:
    return report_path().parent / "phase4_atd_sim_examples.json"


def development_database_url() -> str:
    explicit = os.environ.get("CALIBER_ATD_SIM_DATABASE_URL") or os.environ.get("CALIBER_PLAYER_TD_DATABASE_URL")
    if explicit:
        return explicit
    for candidate in (Path("data/caliber_player_td_dev.db"), Path("data/caliber_atd_sim_dev.db")):
        if candidate.exists():
            return f"sqlite:///{candidate.resolve().as_posix()}"
    return settings.database_url


def snapshot_protected() -> dict[str, int | None]:
    root = Path(settings.parquet_dir) / "models"
    out: dict[str, int | None] = {}
    for pattern in PROTECTED_GLOBS:
        for path in root.rglob(pattern):
            if "atd_sim" in path.parts:
                continue
            out[str(path)] = path.stat().st_mtime_ns if path.exists() else None
    return out


def assert_protected_unchanged(before: dict[str, int | None]) -> None:
    after = snapshot_protected()
    for path, stamp in before.items():
        if after.get(path) != stamp:
            raise RuntimeError(f"Protected artifact changed during Phase 4: {path}")


def safety_banner() -> dict[str, Any]:
    return {
        "version": VERSION,
        "research_only": RESEARCH_ONLY,
        "play_enabled": PLAY_ENABLED,
        "promoted": PROMOTED,
        "validated_play": False,
        "database_url": development_database_url(),
        "artifact_dir": str(artifact_dir()),
        "preserves": ["M3", "M4", "M5", "M6", "team_td_v1", "player_td_v1"],
        "does_not_modify": [
            "spe.models.team_td",
            "spe.models.player_td.train/score/dataset/model/shares",
            "live Anytime TD Finder",
            "historical forecasts/odds",
        ],
    }
