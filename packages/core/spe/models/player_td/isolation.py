"""Isolation guards for the player Anytime TD research lane.

Agent 1 owns ``spe.models.team_td`` and shared ``*_team_td_v1.json`` artifacts.
This lane never overwrites M3/M4/M5 or team-TD artifacts, never enables Play,
and never writes live forecasts.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from spe.config import settings

VERSION = "m6_player_td"
RESEARCH_ONLY = True
PLAY_ENABLED = False
PROMOTED = False

PROTECTED_ARTIFACT_GLOBS = (
    "*_m3.json",
    "*_m4.json",
    "*_m5.json",
    "*_team_td_v1.json",
    "*_player_td_v1.json",
    "m4_comparison.json",
    "m5_comparison.json",
    "phase2_team_td_report.json",
    "phase3_player_td_report.json",
)


def artifact_dir() -> Path:
    path = Path(settings.parquet_dir) / "models" / "player_td"
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifact_path(league_id: str) -> Path:
    return artifact_dir() / f"{league_id}_{VERSION}.json"


def report_path() -> Path:
    return Path(settings.parquet_dir).parent / "player_td_dev" / "m6_player_td_report.json"


def comparison_path() -> Path:
    return artifact_dir() / "m6_comparison.json"


def protected_model_dir() -> Path:
    return Path(settings.parquet_dir) / "models"


def snapshot_protected_artifacts() -> dict[str, int | None]:
    root = protected_model_dir()
    out: dict[str, int | None] = {}
    for pattern in PROTECTED_ARTIFACT_GLOBS:
        for path in root.glob(pattern):
            # Skip our subdirectory.
            if "player_td" in path.parts:
                continue
            out[str(path)] = path.stat().st_mtime_ns if path.exists() else None
    return out


def assert_protected_unchanged(before: dict[str, int | None]) -> None:
    after = snapshot_protected_artifacts()
    for path, stamp in before.items():
        now = after.get(path)
        if now != stamp:
            raise RuntimeError(f"Protected shared artifact changed: {path}")
    # New protected files outside player_td/ are also forbidden if they match globs
    # and were not present before — ignore extras that Agent 1 may write concurrently
    # under team_td names only if stamps differ for keys we tracked.


def development_database_url() -> str:
    """Prefer the isolated player-TD copy; never invent a second write to live forecasts."""
    explicit = os.environ.get("CALIBER_PLAYER_TD_DATABASE_URL")
    if explicit:
        return explicit
    dev = Path("data/caliber_player_td_dev.db")
    if dev.exists():
        return f"sqlite:///{dev.resolve().as_posix()}"
    # Fall back to settings but research code must still avoid live forecast writes.
    return settings.database_url


def safety_banner() -> dict[str, Any]:
    return {
        "version": VERSION,
        "research_only": RESEARCH_ONLY,
        "play_enabled": PLAY_ENABLED,
        "promoted": PROMOTED,
        "artifact_dir": str(artifact_dir()),
        "database_url": development_database_url(),
        "does_not_modify": [
            "spe.models.team_td",
            "M3/M4/M5 artifacts",
            "team_td_v1 artifacts",
            "live forecasts",
            "Validated Play",
        ],
    }
