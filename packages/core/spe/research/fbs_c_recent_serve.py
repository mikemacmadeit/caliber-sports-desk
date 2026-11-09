"""Serve locked C_recent as the primary FBS research Anytime TD estimate.

Research-serving only. Does not promote, retune, or enable Validated Play.
Does not overwrite the locked 821-pair trial freeze.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.research.atd_forecast import forecast_upcoming, research_dir
from spe.research.m5_fbs_lock_handoff import (
    CANDIDATE_FAMILY,
    LOCKED_VERSION,
    _c_artifact_path,
    _lock_dir,
    _score_c_recent,
    out_dir,
)
from spe.research.m5_fbs_refit import PIPELINE_VERSION, RECENT_MODEL_ID

PRIMARY_LABEL = "C_recent — experimental research"
# Bust live score cache when current-team recent8 scoping / PGS team_id repair ships
# (frozen pairs untouched).
FEATURE_SCOPE_VERSION = "current_team_recent8_pgs_team_v2"
LEGACY_M5_LABEL = "Legacy M5 comparison"
UNAVAILABLE = "Research estimate unavailable"


def locked_artifact_path() -> Path:
    snap = _lock_dir() / f"{RECENT_MODEL_ID}.locked.json"
    if snap.exists():
        return snap
    return _c_artifact_path()


def load_locked_c_recent() -> dict[str, Any] | None:
    path = locked_artifact_path()
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def paired_freeze_index() -> dict[tuple[str, str], dict[str, Any]]:
    """Immutable locked-cohort probs — never overwrite; use original freeze timestamp."""
    path = out_dir() / "prospective" / "latest_paired_freeze.json"
    if not path.exists():
        return {}
    freeze = json.loads(path.read_text(encoding="utf-8"))
    frozen_at = freeze.get("frozen_at")
    freeze_id = freeze.get("freeze_id")
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for g in freeze.get("games") or []:
        gid = str(g.get("game_id"))
        for pl in g.get("players") or []:
            if not pl.get("paired"):
                continue
            pid = str(pl.get("player_id"))
            out[(gid, pid)] = {
                "p_c_recent": pl.get("p_c_recent"),
                "p_m5": pl.get("p_m5"),
                "prediction_timestamp": frozen_at,
                "freeze_id": freeze_id,
                "source": "locked_paired_freeze",
                "kickoff_utc": g.get("kickoff_utc"),
                "prior_td_rate": pl.get("prior_td_rate"),
                "recent8_td_rate": pl.get("recent8_td_rate"),
                "p_c_recent_prior_source": pl.get("p_c_recent_prior_source"),
            }
    return out


def research_forecast_dir() -> Path:
    path = research_dir() / "fbs_c_recent_serve"
    path.mkdir(parents=True, exist_ok=True)
    return path


def research_forecast_log_path() -> Path:
    return research_forecast_dir() / "live_research_forecasts.jsonl"


def live_score_cache_path() -> Path:
    return research_forecast_dir() / "live_score_cache.json"


def load_live_score_cache() -> dict[str, Any]:
    path = live_score_cache_path()
    if not path.exists():
        return {"rows": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"rows": {}}


def save_live_score_cache(cache: dict[str, Any]) -> None:
    live_score_cache_path().write_text(json.dumps(cache, indent=2, default=str), encoding="utf-8")


def append_live_research_forecast(row: dict[str, Any]) -> None:
    """Separately timestamped live research scores — never mutates the 821-pair freeze."""
    path = research_forecast_log_path()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def apply_c_recent_to_player(
    pl: dict[str, Any],
    *,
    game_id: str,
    kickoff_utc: str | None,
    artifact: dict[str, Any],
    freeze_idx: dict[tuple[str, str], dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """Attach C_recent primary fields onto a forecast player dict (mutates copy)."""
    out = dict(pl)
    pid = str(pl.get("player_id") or "")
    key = (str(game_id), pid)
    kick = None
    if kickoff_utc:
        try:
            kick = datetime.fromisoformat(str(kickoff_utc).replace("Z", "+00:00"))
            if kick.tzinfo is None:
                kick = kick.replace(tzinfo=UTC)
        except ValueError:
            kick = None

    # Never invent post-kickoff "pregame" forecasts
    if kick is not None and kick <= now:
        out["p_c_recent"] = None
        out["p_c_recent_status"] = "rejected_post_kickoff"
        out["c_recent_unavailable_reason"] = "game_already_kicked_off"
        out["primary_model"] = CANDIDATE_FAMILY
        out["primary_model_label"] = PRIMARY_LABEL
        out["prediction_timestamp"] = None
        out["prediction_source"] = "unavailable"
        return out

    frozen = freeze_idx.get(key)
    if frozen and frozen.get("p_c_recent") is not None and np.isfinite(float(frozen["p_c_recent"])):
        out["p_c_recent"] = float(frozen["p_c_recent"])
        out["p_c_recent_status"] = "ok"
        out["prediction_timestamp"] = frozen.get("prediction_timestamp")
        out["prediction_source"] = "locked_paired_freeze"
        out["freeze_id"] = frozen.get("freeze_id")
        out["p_c_recent_prior_source"] = frozen.get("p_c_recent_prior_source")
        # Keep legacy M5 comparison from freeze when present
        if frozen.get("p_m5") is not None:
            out["p_m5_legacy"] = float(frozen["p_m5"])
        elif out.get("p_m5") is not None:
            out["p_m5_legacy"] = out.get("p_m5")
        out["primary_model"] = CANDIDATE_FAMILY
        out["primary_model_label"] = PRIMARY_LABEL
        out["locked_version"] = LOCKED_VERSION
        out["pipeline_version"] = PIPELINE_VERSION + "+recent8_prior_td_rate"
        return out

    # Reuse cached live research score for this player-game (stable prediction timestamp).
    cache_key = f"{game_id}|{pid}"
    live_cache = out.get("_live_cache_ref")
    cached = (live_cache or {}).get("rows", {}).get(cache_key) if isinstance(live_cache, dict) else None
    if (
        isinstance(cached, dict)
        and cached.get("p_c_recent") is not None
        and cached.get("locked_version") == LOCKED_VERSION
        and cached.get("feature_scope_version") == FEATURE_SCOPE_VERSION
    ):
        out["p_c_recent"] = float(cached["p_c_recent"])
        out["p_c_recent_status"] = "ok"
        out["prediction_timestamp"] = cached.get("prediction_timestamp")
        out["prediction_source"] = "locked_artifact_live_score_cached"
        out["p_c_recent_prior_source"] = cached.get("p_c_recent_prior_source")
        out["p_m5_legacy"] = out.get("p_m5") if out.get("p_m5") is not None else cached.get("p_m5_legacy")
        out["primary_model"] = CANDIDATE_FAMILY
        out["primary_model_label"] = PRIMARY_LABEL
        out["locked_version"] = LOCKED_VERSION
        out["pipeline_version"] = PIPELINE_VERSION + "+recent8_prior_td_rate+" + FEATURE_SCOPE_VERSION
        return out

    # Live research score with locked artifact (separate timestamp from freeze trial)
    pred = _score_c_recent(artifact, out)
    scored_at = now.isoformat()
    if pred.get("status") == "ok" and pred.get("p_atd") is not None and np.isfinite(float(pred["p_atd"])):
        out["p_c_recent"] = float(pred["p_atd"])
        out["p_c_recent_status"] = "ok"
        out["prediction_timestamp"] = scored_at
        out["prediction_source"] = "locked_artifact_live_score"
        prior_src = (pred.get("row") or {}).get("prior_td_rate_source") if isinstance(pred.get("row"), dict) else None
        if not prior_src:
            if out.get("recent8_td_rate_current_team") is not None and np.isfinite(
                float(out.get("recent8_td_rate_current_team"))
            ):
                prior_src = "recent8_td_rate_current_team"
            elif out.get("recent8_td_rate") is not None and np.isfinite(float(out.get("recent8_td_rate"))):
                prior_src = "recent8_td_rate"
            else:
                prior_src = "expanding_fallback"
        out["p_c_recent_prior_source"] = prior_src
        out["p_m5_legacy"] = out.get("p_m5")
        out["primary_model"] = CANDIDATE_FAMILY
        out["primary_model_label"] = PRIMARY_LABEL
        out["locked_version"] = LOCKED_VERSION
        out["pipeline_version"] = PIPELINE_VERSION + "+recent8_prior_td_rate+" + FEATURE_SCOPE_VERSION
        row = {
            "scored_at": scored_at,
            "prediction_timestamp": scored_at,
            "game_id": game_id,
            "player_id": pid,
            "p_c_recent": out["p_c_recent"],
            "p_m5_legacy": out.get("p_m5_legacy"),
            "p_c_recent_prior_source": out.get("p_c_recent_prior_source"),
            "locked_version": LOCKED_VERSION,
            "feature_scope_version": FEATURE_SCOPE_VERSION,
            "kickoff_utc": kickoff_utc,
            "note": "live_research_not_part_of_821_pair_trial",
        }
        append_live_research_forecast(row)
        if isinstance(live_cache, dict):
            live_cache.setdefault("rows", {})[cache_key] = row
            out["_cache_dirty"] = True
    else:
        out["p_c_recent"] = None
        out["p_c_recent_status"] = pred.get("status") or "rejected"
        out["c_recent_unavailable_reason"] = pred.get("reason") or "c_recent_scoring_failed"
        out["prediction_timestamp"] = None
        out["prediction_source"] = "unavailable"
        out["p_m5_legacy"] = out.get("p_m5")
        out["primary_model"] = CANDIDATE_FAMILY
        out["primary_model_label"] = PRIMARY_LABEL
        # Do NOT copy M5 into primary
    return out


def score_fbs_forecasts_with_c_recent(
    session: Session,
    *,
    now: datetime | None = None,
    horizon_hours: int = 72,
    max_games: int = 40,
) -> dict[str, Any]:
    """Run FBS upcoming forecasts and attach locked C_recent as primary."""
    moment = as_utc(now) or datetime.now(UTC)
    artifact = load_locked_c_recent()
    freeze_idx = paired_freeze_index()
    live_cache = load_live_score_cache()
    fc = forecast_upcoming(
        session,
        now=moment,
        horizon_hours=horizon_hours,
        max_games_per_league=max_games,
        include_nfl_ensemble=False,
    )
    fbs_block = ((fc.get("leagues") or {}).get("fbs") or {})
    games_out: list[dict[str, Any]] = []
    n_ok = 0
    n_unavail = 0
    n_frozen = 0
    n_live = 0
    cache_dirty = False
    if artifact is None:
        return {
            "status": "missing_locked_artifact",
            "leagues": {"fbs": {"games": []}},
            "as_of": moment.isoformat(),
        }

    for g in fbs_block.get("games") or []:
        gid = str(g.get("game_id"))
        kick = g.get("kickoff_utc")
        players = []
        for pl in g.get("players") or []:
            pl = {**pl, "_live_cache_ref": live_cache}
            scored = apply_c_recent_to_player(
                pl,
                game_id=gid,
                kickoff_utc=kick,
                artifact=artifact,
                freeze_idx=freeze_idx,
                now=moment,
            )
            if scored.pop("_cache_dirty", False):
                cache_dirty = True
            scored.pop("_live_cache_ref", None)
            if scored.get("p_c_recent") is not None:
                n_ok += 1
                if scored.get("prediction_source") == "locked_paired_freeze":
                    n_frozen += 1
                else:
                    n_live += 1
            else:
                n_unavail += 1
            players.append(scored)
        games_out.append({**g, "players": players, "league_id": "fbs"})

    if cache_dirty:
        save_live_score_cache(live_cache)

    return {
        "status": "ok",
        "as_of": moment.isoformat(),
        "serving": {
            "primary": CANDIDATE_FAMILY,
            "primary_label": PRIMARY_LABEL,
            "legacy_comparison": "m5",
            "legacy_label": LEGACY_M5_LABEL,
            "locked_version": LOCKED_VERSION,
            "artifact_path": str(locked_artifact_path()),
            "play_enabled": False,
            "promoted": False,
            "validated_play": False,
            "research_only": True,
            "accuracy_unproven": True,
        },
        "counts": {
            "games": len(games_out),
            "players_ok": n_ok,
            "players_unavailable": n_unavail,
            "from_locked_freeze": n_frozen,
            "live_research_scores": n_live,
        },
        "leagues": {"fbs": {"games": games_out, "primary": CANDIDATE_FAMILY}},
    }
