"""Phase 5 Anytime TD validation isolation. Does not modify Phase 4 models or Play."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from spe.simulate.correlated_atd.isolation import (
    PLAY_ENABLED,
    PROMOTED,
    RESEARCH_ONLY,
    VERSION,
    artifact_dir,
    assert_protected_unchanged,
    development_database_url,
    safety_banner,
    snapshot_protected,
)

PHASE = 5
PHASE_VERSION = "atd_sim_v1_phase5"


def phase5_dir() -> Path:
    path = Path("data") / "atd_sim_dev" / "phase5"
    path.mkdir(parents=True, exist_ok=True)
    return path


def report_path() -> Path:
    return phase5_dir() / "phase5_atd_validation_report.json"


def examined_games_path() -> Path:
    """Games used in Phase 4 backtests — not treated as fresh validation."""
    return phase5_dir() / "phase4_examined_games.json"


def load_phase4_examined_games() -> dict[str, list[str]]:
    """Recover Phase 4 examined game ids from prior backtest artifacts when present."""
    out: dict[str, list[str]] = {"nfl": [], "fbs": []}
    cached = examined_games_path()
    if cached.exists():
        raw = json.loads(cached.read_text(encoding="utf-8"))
        for lg in ("nfl", "fbs"):
            out[lg] = list(raw.get(lg) or [])
        return out
    # Infer from Phase 4 report examples + backtest artifacts if they stored game lists.
    p4 = Path("data") / "atd_sim_dev" / "phase4_atd_sim_report.json"
    if p4.exists():
        rep = json.loads(p4.read_text(encoding="utf-8"))
        for lg, body in (rep.get("backtests") or {}).items():
            # Phase 4 did not always persist game_id lists; mark count only.
            n = int(body.get("n_games") or 0)
            out.setdefault(lg, [])
            out[f"{lg}_phase4_n_games"] = n  # type: ignore[assignment]
        for ex in (rep.get("examples") or {}).get("examples") or []:
            gid = ex.get("game_id")
            lg = ex.get("league_id")
            if gid and lg in out:
                out[lg].append(str(gid))
    return out


def save_examined_games(by_league: dict[str, list[str]]) -> None:
    examined_games_path().write_text(json.dumps(by_league, indent=2), encoding="utf-8")


def phase5_banner() -> dict[str, Any]:
    base = safety_banner()
    base.update(
        {
            "phase": PHASE,
            "phase_version": PHASE_VERSION,
            "parent_sim_version": VERSION,
            "research_only": RESEARCH_ONLY,
            "play_enabled": PLAY_ENABLED,
            "promoted": PROMOTED,
            "validated_play": False,
            "phase5_dir": str(phase5_dir()),
            "goal": "expand validation + shadow + FBS diagnosis; do not promote",
        }
    )
    return base


__all__ = [
    "PHASE",
    "PHASE_VERSION",
    "PLAY_ENABLED",
    "PROMOTED",
    "RESEARCH_ONLY",
    "artifact_dir",
    "assert_protected_unchanged",
    "development_database_url",
    "examined_games_path",
    "load_phase4_examined_games",
    "phase5_banner",
    "phase5_dir",
    "report_path",
    "save_examined_games",
    "snapshot_protected",
]
