"""Unified research Anytime TD forecasting interface.

NFL primary: Player TD v1; optional equal-weight ensemble with serialized M6-GBM.
FBS primary: M5 (strict features — reject invalid rows).
Team scoring: team_td_v1. Correlated sim available but never auto-combined.
Does not promote. Does not enable Validated Play. Does not write live forecasts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.config import settings
from spe.db.base import as_utc
from spe.db.models import Game
from spe.models.player_td.score import score_upcoming_player_td
from spe.models.rolling_td import load_m5, predict_anytime_td_m5
from spe.research.atd_features import extract_feature_dict, m5_feature_candidates
from spe.research.m6_hist_gbm import VERSION as M6_GBM_VERSION
from spe.research.m6_hist_gbm import load_m6_hist_gbm, predict_proba_frame

PLAY_ENABLED = False
PROMOTED = False
RESEARCH_ONLY = True
VALIDATED_PLAY = False


def research_dir() -> Path:
    path = Path(settings.parquet_dir).parent / "research" / "atd_unified"
    path.mkdir(parents=True, exist_ok=True)
    return path


def forecast_path() -> Path:
    return research_dir() / "research_forecasts.json"


def prospective_dir() -> Path:
    path = research_dir() / "prospective"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _reject(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "rejected", "reason": reason, "p_atd": None, **extra}


def predict_m5_strict(
    artifact: dict[str, Any],
    *,
    player_id: str,
    team_id: str,
    position_group: str,
    row: pd.Series | dict[str, Any],
) -> dict[str, Any]:
    """M5 inference that never invents feature values.

    Finite observed features are used; missing features are omitted (not zero-filled).
    Rejects empty identity, non-finite probabilities, or zero usable signal when the
    artifact expects feature betas.
    """
    if not player_id or not position_group or position_group == "UNK":
        return _reject("invalid_player_identity", model="m5")
    required = list(artifact.get("features") or m5_feature_candidates(artifact.get("league_id") or "nfl"))
    feats, problems = extract_feature_dict(row, required, strict=False)
    assert feats is not None
    betas = artifact.get("feature_betas") or {}
    expected_beta_feats = [k for k, v in betas.items() if v]
    if expected_beta_feats and not any(k in feats for k in expected_beta_feats):
        # Artifact relies on feature multipliers but none are available — do not silently
        # return bare shrinkage as if it were the full M5 feature path.
        return _reject(
            "no_usable_m5_features",
            problems=problems,
            expected=expected_beta_feats,
            model="m5",
        )
    opp_raw = row.get("opp_def_z") if hasattr(row, "get") else None
    opp_defaulted = False
    try:
        opp_z = float(opp_raw) if opp_raw is not None and np.isfinite(float(opp_raw)) else None
    except (TypeError, ValueError):
        opp_z = None
    if opp_z is None:
        # Neutral opponent (z=0) is the documented league-average default when defense
        # priors are unavailable — not a fabricated player feature. Still flagged.
        opp_z = 0.0
        opp_defaulted = True
    pred = predict_anytime_td_m5(
        artifact,
        player_id=player_id,
        team_id=team_id,
        position_group=position_group,
        opp_def_z=opp_z,
        features=feats,
    )
    if not np.isfinite(float(pred.get("p") or np.nan)):
        return _reject("non_finite_probability", model="m5")
    return {
        "status": "ok",
        "model": "m5",
        "p_atd": float(pred["p"]),
        "p_atd_low": float(pred["p_low"]),
        "p_atd_high": float(pred["p_high"]),
        "features_used": sorted(feats.keys()),
        "features_skipped": problems,
        "opp_def_z_defaulted": opp_defaulted,
        "research_only": True,
        "play_enabled": False,
    }


def _ensemble_equal(*probs: float | None) -> float | None:
    vals = [float(p) for p in probs if p is not None and np.isfinite(float(p))]
    if not vals:
        return None
    return float(np.clip(np.mean(vals), 1e-6, 1 - 1e-6))


def forecast_upcoming(
    session: Session,
    *,
    horizon_hours: int = 24 * 14,
    now: datetime | None = None,
    include_nfl_ensemble: bool = True,
    game_ids: list[str] | None = None,
    max_games_per_league: int | None = 4,
) -> dict[str, Any]:
    """Research forecasts for upcoming games using the unified model routing."""
    moment = as_utc(now) or datetime.now(UTC)
    out: dict[str, Any] = {
        "status": "ok",
        "as_of": moment.isoformat(),
        "play_enabled": PLAY_ENABLED,
        "promoted": PROMOTED,
        "research_only": RESEARCH_ONLY,
        "validated_play": VALIDATED_PLAY,
        "routing": {
            "nfl_primary": "player_td_v1",
            "nfl_ensemble_candidate": "ens_m6_gbm_player_td" if include_nfl_ensemble else None,
            "fbs_primary": "m5",
            "team_scoring": "team_td_v1",
            "correlated_sim": "available_not_auto_combined",
        },
        "leagues": {},
        "examples": {"nfl": [], "fbs": []},
        "rejected": [],
        "bugs": [],
    }

    # --- NFL: Player TD v1 (+ optional M6-GBM ensemble) ---
    nfl_live = score_upcoming_player_td(
        session,
        game_ids=game_ids,
        horizon_hours=horizon_hours,
        now=moment,
        research_features=True,
    )
    nfl_games = [g for g in (nfl_live.get("games") or []) if g.get("league_id") == "nfl" or True]
    # score_upcoming returns mixed leagues; filter by querying game league when needed.
    nfl_filtered: list[dict[str, Any]] = []
    for g in nfl_live.get("games") or []:
        gid = g.get("game_id")
        game = session.get(Game, gid) if gid else None
        if game is None:
            continue
        if game.league_id != "nfl":
            continue
        nfl_filtered.append({**g, "league_id": "nfl"})
    if max_games_per_league is not None:
        nfl_filtered = nfl_filtered[:max_games_per_league]

    gbm_loaded = load_m6_hist_gbm("nfl") is not None
    if include_nfl_ensemble and not gbm_loaded:
        out["bugs"].append("nfl_m6_hist_gbm_not_serialized_ensemble_skipped")

    nfl_out_games: list[dict[str, Any]] = []
    for g in nfl_filtered:
        players = list(g.get("players") or [])
        # Build feature frame for optional GBM from player dicts.
        ens_applied = False
        if include_nfl_ensemble and gbm_loaded and players:
            frame = pd.DataFrame(players)
            # Map forecast field names back to feature names where needed.
            rename = {
                "exp_carries": "prior_carries",
                "exp_targets": "prior_targets",
                "exp_receptions": "prior_receptions",
                "exp_rz_targets": "prior_rz_targets",
                "exp_gl_carries": "prior_gl_carries",
            }
            for src, dest in rename.items():
                if src in frame.columns and dest not in frame.columns:
                    frame[dest] = frame[src]
            p_gbm, valid, info = predict_proba_frame("nfl", frame)
            for i, pl in enumerate(players):
                p_v1 = pl.get("p_atd")
                if i < len(p_gbm) and valid[i] and np.isfinite(p_gbm[i]):
                    ens = _ensemble_equal(p_v1, float(p_gbm[i]))
                    pl["p_m6_hist_gbm"] = float(p_gbm[i])
                    pl["p_ensemble_m6_gbm_player_td"] = ens
                    pl["primary_model"] = "player_td_v1"
                    pl["ensemble_status"] = "ok"
                    ens_applied = True
                else:
                    pl["p_m6_hist_gbm"] = None
                    pl["p_ensemble_m6_gbm_player_td"] = None
                    pl["ensemble_status"] = "rejected_invalid_gbm_features"
                    if info.get("status") != "ok":
                        out["rejected"].append(
                            {
                                "league_id": "nfl",
                                "game_id": g.get("game_id"),
                                "player_id": pl.get("player_id"),
                                "reason": "m6_gbm_invalid_features",
                            }
                        )
            g["m6_gbm_info"] = {k: info.get(k) for k in ("status", "n_valid", "n_rejected", "model_sha256", "version")}
        else:
            for pl in players:
                pl["primary_model"] = "player_td_v1"
                pl["p_m6_hist_gbm"] = None
                pl["p_ensemble_m6_gbm_player_td"] = None
        g["ensemble_applied"] = ens_applied
        g["team_scoring_source"] = "team_td_v1"
        nfl_out_games.append(g)
        for pl in players[:6]:
            if len(out["examples"]["nfl"]) < 12:
                out["examples"]["nfl"].append(
                    {
                        "game_id": g.get("game_id"),
                        "matchup": f"{g.get('away')} @ {g.get('home')}",
                        "player_name": pl.get("player_name"),
                        "position_group": pl.get("position_group"),
                        "p_player_td_v1": pl.get("p_atd"),
                        "p_m6_hist_gbm": pl.get("p_m6_hist_gbm"),
                        "p_ensemble": pl.get("p_ensemble_m6_gbm_player_td"),
                        "availability_status": pl.get("availability_status"),
                    }
                )

    out["leagues"]["nfl"] = {
        "primary": "player_td_v1",
        "n_games": len(nfl_out_games),
        "games": nfl_out_games,
        "m6_hist_gbm_version": M6_GBM_VERSION if gbm_loaded else None,
    }

    # --- FBS: M5 strict ---
    m5 = load_m5("fbs")
    fbs_games_q = (
        session.query(Game)
        .filter(
            Game.league_id == "fbs",
            Game.data_tier == "upcoming",
            # Pregame research only — do not freeze/serve in-progress as upcoming.
            Game.status.in_(("scheduled", "preview", "pregame")),
            Game.kickoff_utc >= moment,
            Game.kickoff_utc <= moment + timedelta(hours=horizon_hours),
        )
        .order_by(Game.kickoff_utc.asc())
        .all()
    )
    if game_ids:
        wanted = set(game_ids)
        fbs_games_q = [g for g in fbs_games_q if g.game_id in wanted]
    if max_games_per_league is not None:
        fbs_games_q = fbs_games_q[:max_games_per_league]

    # Reuse player_td scorer for roster/features, then replace probs with M5.
    fbs_live = score_upcoming_player_td(
        session,
        game_ids=[g.game_id for g in fbs_games_q] or None,
        horizon_hours=horizon_hours,
        now=moment,
        research_features=True,
    )
    fbs_out: list[dict[str, Any]] = []
    for g in fbs_live.get("games") or []:
        game = session.get(Game, g.get("game_id"))
        if game is None or game.league_id != "fbs":
            continue
        players_out: list[dict[str, Any]] = []
        for pl in g.get("players") or []:
            if m5 is None:
                players_out.append({**pl, "primary_model": "m5", "m5_status": "missing_artifact", "p_m5": None})
                out["rejected"].append(
                    {"league_id": "fbs", "game_id": g.get("game_id"), "player_id": pl.get("player_id"), "reason": "m5_missing"}
                )
                continue
            # Build row for strict M5 from scorer fields.
            row = {
                **pl,
                "prior_carries": pl.get("exp_carries"),
                "prior_targets": pl.get("exp_targets"),
                "prior_receptions": pl.get("exp_receptions"),
                "prior_rz_targets": pl.get("exp_rz_targets"),
                "prior_gl_carries": pl.get("exp_gl_carries"),
                "prior_td_rate": pl.get("prior_td_rate") if "prior_td_rate" in pl else None,
                "roll3_td_rate": pl.get("roll3_td_rate"),
                "workload_delta": pl.get("workload_delta"),
                "prior_carry_share": pl.get("prior_carry_share") if "prior_carry_share" in pl else None,
                "own_off_epa": pl.get("own_off_epa"),
                "opp_adj_def": pl.get("opp_adj_def"),
                "team_rz_td": pl.get("team_rz_td"),
                "team_points_per_opp": pl.get("team_points_per_opp"),
                "usage_x_defense": pl.get("usage_x_defense"),
                "is_home": pl.get("is_home"),
                "opp_def_z": pl.get("opp_def_z"),
            }
            # Player TD scorer may omit some M5 features — reject rather than invent.
            pred = predict_m5_strict(
                m5,
                player_id=str(pl.get("player_id")),
                team_id=str(pl.get("team_id") or ""),
                position_group=str(pl.get("position_group") or "UNK"),
                row=row,
            )
            entry = {
                **pl,
                "primary_model": "m5",
                "p_player_td_v1_ref": pl.get("p_atd"),
                "m5_status": pred.get("status"),
            }
            if pred.get("status") == "ok":
                entry["p_m5"] = pred["p_atd"]
                entry["p_atd"] = pred["p_atd"]  # primary for FBS research
                entry["p_atd_low"] = pred.get("p_atd_low")
                entry["p_atd_high"] = pred.get("p_atd_high")
                entry["features_used"] = pred.get("features_used")
                entry["opp_def_z_defaulted"] = bool(pred.get("opp_def_z_defaulted"))
                if pred.get("opp_def_z_defaulted"):
                    entry.setdefault("missing_information", [])
                    if isinstance(entry["missing_information"], list):
                        entry["missing_information"].append("opp_def_z_defaulted_to_league_average")
            else:
                entry["p_m5"] = None
                entry["p_atd"] = None
                entry["reject_reason"] = pred.get("reason")
                entry["reject_problems"] = pred.get("problems")
                out["rejected"].append(
                    {
                        "league_id": "fbs",
                        "game_id": g.get("game_id"),
                        "player_id": pl.get("player_id"),
                        "reason": pred.get("reason"),
                        "problems": pred.get("problems"),
                    }
                )
            players_out.append(entry)
        blob = {
            **g,
            "league_id": "fbs",
            "primary_model": "m5",
            "team_scoring_source": "team_td_v1",
            "players": players_out,
        }
        fbs_out.append(blob)
        for pl in players_out[:6]:
            if len(out["examples"]["fbs"]) < 12:
                out["examples"]["fbs"].append(
                    {
                        "game_id": g.get("game_id"),
                        "matchup": f"{g.get('away')} @ {g.get('home')}",
                        "player_name": pl.get("player_name"),
                        "position_group": pl.get("position_group"),
                        "p_m5": pl.get("p_m5"),
                        "m5_status": pl.get("m5_status"),
                        "reject_reason": pl.get("reject_reason"),
                        "availability_status": pl.get("availability_status"),
                    }
                )

    out["leagues"]["fbs"] = {
        "primary": "m5",
        "n_games": len(fbs_out),
        "games": fbs_out,
        "m5_artifact_present": m5 is not None,
        "strict_features": True,
    }

    path = forecast_path()
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    out["forecast_path"] = str(path)
    return out


def freeze_prospective(
    session: Session,
    *,
    horizon_hours: int = 24 * 72,
    now: datetime | None = None,
    include_nfl_ensemble: bool = True,
    examined_game_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Freeze unified research forecasts before kickoff for never-examined games."""
    moment = as_utc(now) or datetime.now(UTC)
    forecasts = forecast_upcoming(
        session,
        horizon_hours=horizon_hours,
        now=moment,
        include_nfl_ensemble=include_nfl_ensemble,
        max_games_per_league=20,
    )
    examined = examined_game_ids or set()
    # Also treat games already in prior shadow reports as examined when file exists.
    shadow_hint = Path(settings.parquet_dir).parent / "research" / "atd_integration_report.json"
    if shadow_hint.exists():
        try:
            prev = json.loads(shadow_hint.read_text(encoding="utf-8"))
            for lg in ("nfl", "fbs"):
                block = (prev.get("2_fair_comparison_results") or {}).get(lg) or {}
                for gid in block.get("game_ids") or []:
                    examined.add(str(gid))
        except Exception:
            pass

    frozen_games: list[dict[str, Any]] = []
    for lg, block in (forecasts.get("leagues") or {}).items():
        for g in block.get("games") or []:
            gid = str(g.get("game_id"))
            kick = g.get("kickoff_utc")
            if not kick:
                continue
            kick_dt = datetime.fromisoformat(str(kick).replace("Z", "+00:00"))
            if kick_dt.tzinfo is None:
                kick_dt = kick_dt.replace(tzinfo=UTC)
            if kick_dt <= moment:
                continue  # must be before kickoff
            tag = "previously_examined" if gid in examined else "prospective_unexamined"
            frozen_games.append(
                {
                    "league_id": lg,
                    "game_id": gid,
                    "kickoff_utc": kick_dt.isoformat(),
                    "frozen_at": moment.isoformat(),
                    "holdout_tag": tag,
                    "matchup": f"{g.get('away')} @ {g.get('home')}",
                    "primary_model": (block.get("primary") if isinstance(block, dict) else None),
                    "n_players": len(g.get("players") or []),
                    "players": g.get("players"),
                    "team_expectations": g.get("team_expectations"),
                }
            )

    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    out_path = prospective_dir() / f"freeze_{stamp}.json"
    payload = {
        "status": "ok",
        "frozen_at": moment.isoformat(),
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "validated_play": False,
        "n_games": len(frozen_games),
        "n_prospective_unexamined": sum(1 for g in frozen_games if g["holdout_tag"] == "prospective_unexamined"),
        "n_previously_examined": sum(1 for g in frozen_games if g["holdout_tag"] == "previously_examined"),
        "baselines_to_compare": ["m3", "m5", "player_td_v1", "m6_hist_gbm", "atd_sim_v1"],
        "metrics_plan": ["brier", "log_loss", "ece", "by_position", "vs_sportsbook"],
        "note": "Settle only after games complete. Do not treat previously examined games as untouched.",
        "games": frozen_games,
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    # Also write a stable "latest" pointer for tracking jobs.
    latest = prospective_dir() / "latest_freeze.json"
    latest.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return {
        "status": "ok",
        "freeze_path": str(out_path),
        "latest_path": str(latest),
        "n_games": len(frozen_games),
        "n_prospective_unexamined": payload["n_prospective_unexamined"],
        "readiness": {
            "freeze_before_kickoff": True,
            "examined_games_tagged": True,
            "baselines_listed": True,
            "settlement_pending": True,
            "sportsbook_eval_pending": True,
        },
    }
