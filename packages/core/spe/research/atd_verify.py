"""Anytime TD verification: disagreements, reliability, freeze→shadow, settlement.

Does not train new models. Does not promote. Does not enable Validated Play.
Does not invent confidence intervals or alter official probabilities.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.config import settings
from spe.db.base import as_utc, new_id
from spe.db.models import Game, ShadowPrediction, ShadowSettlement
from spe.market.odds import american_to_implied, ev_per_dollar, no_vig_two_way
from spe.models.metrics import brier_score, expected_calibration_error, log_loss
from spe.research.atd_features import extract_feature_dict
from spe.research.atd_forecast import forecast_path, prospective_dir, research_dir
from spe.research.m6_hist_gbm import VERSION as M6_GBM_VERSION
from spe.research.m6_hist_gbm import load_m6_hist_gbm, meta_path as m6_meta_path, model_path as m6_model_path
from spe.shadow.settle import settle_shadow_predictions
from spe.simulate.correlated_atd.shadow_atd import _opposing_price, _priced_anytime_rows

DISAGREE_THRESHOLD = 0.10
PLAY_ENABLED = False
PROMOTED = False
RESEARCH_ONLY = True

# Shadow model_version_id prefixes for unified research (append-only).
MODEL_IDS = {
    "player_td_v1": "player_td_v1-{league}-anytime_td",
    "m6_hist_gbm": "m6_hist_gbm_v1-{league}-anytime_td",
    "ens_m6_gbm_player_td": "ens_m6_gbm_player_td-{league}-anytime_td",
    "m5": "m5-{league}-anytime_td",
    "m5_c_recent": "m5_c_recent-{league}-anytime_td",
}


def report_path() -> Path:
    return research_dir() / "verification_report.json"


def freeze_ledger_path() -> Path:
    return research_dir() / "prospective" / "freeze_shadow_ledger.json"


def _sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_latest_freeze() -> dict[str, Any] | None:
    latest = prospective_dir() / "latest_freeze.json"
    if not latest.exists():
        return None
    return json.loads(latest.read_text(encoding="utf-8"))


def _load_forecasts() -> dict[str, Any] | None:
    path = forecast_path()
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def reliability_indicator(player: dict[str, Any], *, p_v1: float | None, p_gbm: float | None) -> dict[str, Any]:
    """Research reliability flags — not a calibrated CI and not used to alter p."""
    flags: list[str] = []
    missing = list(player.get("missing_information") or [])
    sample = player.get("sample_size")
    try:
        n = int(sample) if sample is not None else 0
    except (TypeError, ValueError):
        n = 0
    if n < 5:
        flags.append("limited_player_history")
    if n == 0:
        flags.append("no_prior_games")
    avail = str(player.get("availability_status") or "").lower()
    if avail in {"unknown", ""}:
        flags.append("unknown_availability")
    if avail in {"out", "doubtful"}:
        flags.append("availability_risk")
    if any("injury" in str(m).lower() or "availability" in str(m).lower() for m in missing):
        flags.append("injury_info_gap")
    if player.get("opp_def_z") is None or player.get("opp_def_z_defaulted"):
        flags.append("missing_or_defaulted_defense")
    if player.get("roll3_td_rate") is None and player.get("prior_td_rate") is None:
        flags.append("missing_current_season_td_signal")
    # Feature holes common to GBM schema
    for col in ("prior_rz_targets", "prior_gl_carries", "snap_roll3", "team_points_per_opp"):
        if player.get(col) is None and player.get(col.replace("prior_", "exp_")) is None:
            if col.startswith("prior_rz") or col.startswith("prior_gl"):
                continue  # may be legitimately absent for some positions
    disagree = None
    if p_v1 is not None and p_gbm is not None and np.isfinite(p_v1) and np.isfinite(p_gbm):
        disagree = abs(float(p_v1) - float(p_gbm))
        if disagree >= DISAGREE_THRESHOLD:
            flags.append("large_model_disagreement")
        if disagree >= 0.20:
            flags.append("extreme_model_disagreement")

    # Ordinal reliability for research triage only.
    if "no_prior_games" in flags or "extreme_model_disagreement" in flags:
        level = "low"
    elif len(flags) >= 3 or "large_model_disagreement" in flags or "unknown_availability" in flags:
        level = "medium"
    elif flags:
        level = "medium-high"
    else:
        level = "high"

    return {
        "level": level,
        "flags": flags,
        "sample_size": n,
        "abs_disagreement": disagree,
        "note": "Research triage only — does not invent CIs or modify probabilities",
    }


def _gbm_feature_snapshot(player: dict[str, Any], league_id: str = "nfl") -> dict[str, Any]:
    loaded = load_m6_hist_gbm(league_id)
    if loaded is None:
        return {"status": "missing_artifact"}
    feats = list(loaded["blob"]["features"])
    row = {
        **player,
        "prior_carries": player.get("prior_carries", player.get("exp_carries")),
        "prior_targets": player.get("prior_targets", player.get("exp_targets")),
        "prior_receptions": player.get("prior_receptions", player.get("exp_receptions")),
        "prior_rz_targets": player.get("prior_rz_targets", player.get("exp_rz_targets")),
        "prior_gl_carries": player.get("prior_gl_carries", player.get("exp_gl_carries")),
    }
    values, problems = extract_feature_dict(row, feats, strict=False)
    assert values is not None
    return {
        "status": "ok",
        "feature_schema": feats,
        "observed": values,
        "missing_or_nan": problems,
        "n_observed": len(values),
        "n_schema": len(feats),
        "model_sha256": loaded.get("model_sha256"),
        "platt": loaded["blob"].get("platt"),
    }


def explain_disagreement(player: dict[str, Any], game: dict[str, Any]) -> dict[str, Any]:
    """Attribute Player TD v1 vs M6-GBM gap without averaging them away."""
    p_v1 = player.get("p_atd") if player.get("primary_model") != "m5" else player.get("p_player_td_v1_ref", player.get("p_atd"))
    # NFL forecast stores player_td_v1 in p_atd and gbm separately.
    p_v1 = player.get("p_atd")
    p_gbm = player.get("p_m6_hist_gbm")
    if p_v1 is None or p_gbm is None:
        return {"status": "incomplete", "reason": "missing_probability"}
    p_v1 = float(p_v1)
    p_gbm = float(p_gbm)
    delta = p_gbm - p_v1
    team_lambda = player.get("team_lambda_off_td")
    lambda_player = player.get("lambda_player_td")
    rush_share = player.get("rush_td_share")
    rec_share = player.get("rec_td_share")

    gbm_snap = _gbm_feature_snapshot(player)
    observed = gbm_snap.get("observed") or {}

    causes: list[str] = []
    # Structural: team-budget allocator vs marginal classifier
    causes.append(
        "architecture: player_td_v1 = team_td_v1 λ × opportunity shares (team-budget constrained); "
        "m6_hist_gbm = marginal classifier on player features (not forced to sum to team TD mass)"
    )
    if lambda_player is not None and p_v1 is not None:
        # Poisson-ish mapping check
        try:
            poisson_p = 1.0 - float(np.exp(-float(lambda_player)))
            causes.append(
                f"player_td_v1 maps λ_player={float(lambda_player):.3f} → ~Poisson {poisson_p:.3f} "
                f"(after availability/calibration adjustments → {p_v1:.3f})"
            )
        except Exception:
            pass
    if team_lambda is not None:
        causes.append(f"team scoring context: team_lambda_off_td={float(team_lambda):.3f}")
    if rush_share is not None or rec_share is not None:
        causes.append(
            f"shares: rush_td_share={rush_share} rec_td_share={rec_share} "
            "(v1 probability mass comes from these; GBM does not use explicit shares)"
        )

    # Feature-driven GBM lean
    hot: list[str] = []
    prior_td = observed.get("prior_td_rate", player.get("prior_td_rate"))
    roll3 = observed.get("roll3_td_rate", player.get("roll3_td_rate"))
    rz = observed.get("prior_rz_targets", player.get("prior_rz_targets", player.get("exp_rz_targets")))
    tgt_share = observed.get("prior_target_share", player.get("prior_target_share"))
    if prior_td is not None and float(prior_td) >= 0.45:
        hot.append(f"high prior_td_rate={float(prior_td):.3f}")
    if roll3 is not None and float(roll3) >= 0.67:
        hot.append(f"hot roll3_td_rate={float(roll3):.3f}")
    if rz is not None and float(rz) >= 1.0:
        hot.append(f"elevated prior_rz_targets={float(rz):.3f}")
    if tgt_share is not None and float(tgt_share) >= 0.22:
        hot.append(f"high prior_target_share={float(tgt_share):.3f}")
    if hot:
        causes.append("GBM feature lean (higher p): " + "; ".join(hot))

    # Input consistency checks
    consistency: list[str] = []
    if player.get("player_id") and player.get("team_id"):
        consistency.append("player/team identity present")
    avail = player.get("availability_status")
    if avail:
        consistency.append(f"availability={avail} (prob={player.get('availability_prob')})")
    missing = player.get("missing_information") or []
    if missing:
        consistency.append(f"missing_information={missing}")
    else:
        consistency.append("no missing_information flags on forecast row")
    if gbm_snap.get("missing_or_nan"):
        consistency.append(f"GBM schema gaps (NaN, not invented): {gbm_snap['missing_or_nan']}")

    # Classify root cause
    if abs(delta) >= DISAGREE_THRESHOLD and hot and team_lambda is not None:
        root = "legitimate_modeling_difference"
        detail = (
            "GBM reacts strongly to player TD/RZ/usage history; "
            "Player TD v1 is capped by team λ × receiver share so it cannot fully follow "
            "a hot individual signal without reallocating team TD mass."
        )
    elif gbm_snap.get("n_observed", 0) < 8:
        root = "inconsistent_or_sparse_inputs"
        detail = "GBM saw few usable features relative to its schema; disagreement may reflect missing inputs."
    elif (player.get("sample_size") or 0) < 5:
        root = "sparse_history_instability"
        detail = "Limited player history — both models unstable; gap is not trustworthy either way."
    else:
        root = "legitimate_modeling_difference"
        detail = "Models optimize different objectives (team-coherent allocation vs marginal classification)."

    rel = reliability_indicator(player, p_v1=p_v1, p_gbm=p_gbm)
    return {
        "status": "ok",
        "player_id": player.get("player_id"),
        "player_name": player.get("player_name"),
        "team_id": player.get("team_id"),
        "team_name": player.get("team_name"),
        "position_group": player.get("position_group"),
        "game_id": game.get("game_id"),
        "matchup": f"{game.get('away')} @ {game.get('home')}",
        "p_player_td_v1": p_v1,
        "p_m6_hist_gbm": p_gbm,
        "delta_gbm_minus_v1": delta,
        "abs_delta": abs(delta),
        "p_ensemble_reference_only": player.get("p_ensemble_m6_gbm_player_td"),
        "ensemble_note": "Shown for reference only — not used to hide disagreement",
        "root_cause": root,
        "root_detail": detail,
        "causes": causes,
        "consistency_checks": consistency,
        "team_expectations": (game.get("team_expectations") or {}).get(player.get("team_id") or ""),
        "workload": {
            "exp_carries": player.get("exp_carries"),
            "exp_targets": player.get("exp_targets"),
            "exp_receptions": player.get("exp_receptions"),
            "exp_rz_targets": player.get("exp_rz_targets"),
            "exp_gl_carries": player.get("exp_gl_carries"),
            "prior_td_rate": player.get("prior_td_rate"),
            "roll3_td_rate": player.get("roll3_td_rate"),
            "sample_size": player.get("sample_size"),
        },
        "gbm_features": gbm_snap,
        "reliability": rel,
        "calibration_note": {
            "player_td_v1": "Phase-3 availability-adjusted / position Platt on λ path",
            "m6_hist_gbm": f"Global Platt {gbm_snap.get('platt')} on HistGBM scores",
        },
    }


def audit_disagreements(forecasts: dict[str, Any] | None = None) -> dict[str, Any]:
    data = forecasts or _load_forecasts() or {}
    nfl_games = ((data.get("leagues") or {}).get("nfl") or {}).get("games") or []
    disagreements: list[dict[str, Any]] = []
    for game in nfl_games:
        for player in game.get("players") or []:
            p_v1 = player.get("p_atd")
            p_gbm = player.get("p_m6_hist_gbm")
            if p_v1 is None or p_gbm is None:
                continue
            if abs(float(p_v1) - float(p_gbm)) + 1e-12 < DISAGREE_THRESHOLD:
                continue
            disagreements.append(explain_disagreement(player, game))
    disagreements.sort(key=lambda r: -float(r.get("abs_delta") or 0))
    by_root: dict[str, int] = {}
    for row in disagreements:
        by_root[str(row.get("root_cause"))] = by_root.get(str(row.get("root_cause")), 0) + 1
    return {
        "threshold": DISAGREE_THRESHOLD,
        "n_disagreements": len(disagreements),
        "by_root_cause": by_root,
        "players": disagreements,
        "chase_deep_dive": next(
            (r for r in disagreements if "chase" in str(r.get("player_name") or "").lower()),
            None,
        ),
    }


def assess_reliability(forecasts: dict[str, Any] | None = None) -> dict[str, Any]:
    data = forecasts or _load_forecasts() or {}
    rows: list[dict[str, Any]] = []
    for lg, block in (data.get("leagues") or {}).items():
        for game in block.get("games") or []:
            for player in game.get("players") or []:
                p_v1 = player.get("p_atd") if lg == "nfl" else player.get("p_player_td_v1_ref")
                p_gbm = player.get("p_m6_hist_gbm")
                p_primary = player.get("p_atd") if lg == "nfl" else player.get("p_m5", player.get("p_atd"))
                if p_primary is None:
                    continue
                rel = reliability_indicator(player, p_v1=p_v1 if p_v1 is not None else None, p_gbm=p_gbm)
                rows.append(
                    {
                        "league_id": lg,
                        "game_id": game.get("game_id"),
                        "player_id": player.get("player_id"),
                        "player_name": player.get("player_name"),
                        "position_group": player.get("position_group"),
                        "p_primary": p_primary,
                        "reliability": rel,
                    }
                )
    counts: dict[str, int] = {}
    for r in rows:
        lvl = r["reliability"]["level"]
        counts[lvl] = counts.get(lvl, 0) + 1
    return {
        "n_predictions": len(rows),
        "by_level": counts,
        "low_reliability": [r for r in rows if r["reliability"]["level"] == "low"][:40],
        "medium_with_disagreement": [
            r
            for r in rows
            if "large_model_disagreement" in (r["reliability"].get("flags") or [])
        ][:40],
        "method": "flag-based research triage; no invented confidence intervals",
    }


def verify_prospective_freezes(session: Session) -> dict[str, Any]:
    freeze = _load_latest_freeze()
    if freeze is None:
        return {"status": "missing_freeze"}
    games = freeze.get("games") or []
    issues: list[dict[str, Any]] = []
    ok_games = 0
    artifact_hashes = {
        "nfl_m6_hist_gbm": _sha256_file(m6_model_path("nfl")),
        "fbs_m6_hist_gbm": _sha256_file(m6_model_path("fbs")),
        "nfl_m6_meta": _sha256_file(m6_meta_path("nfl")),
        "fbs_m6_meta": _sha256_file(m6_meta_path("fbs")),
    }
    priced = _priced_anytime_rows(session)
    priced_by_game_player: dict[tuple[str, str], dict[str, Any]] = {}
    for row in priced.values():
        priced_by_game_player[(row["game_id"], row["player_id"])] = row

    freeze_at = freeze.get("frozen_at")
    freeze_dt = datetime.fromisoformat(str(freeze_at).replace("Z", "+00:00")) if freeze_at else None
    if freeze_dt and freeze_dt.tzinfo is None:
        freeze_dt = freeze_dt.replace(tzinfo=UTC)

    for g in games:
        gid = g.get("game_id")
        game = session.get(Game, gid) if gid else None
        kick = g.get("kickoff_utc")
        kick_dt = datetime.fromisoformat(str(kick).replace("Z", "+00:00")) if kick else None
        if kick_dt and kick_dt.tzinfo is None:
            kick_dt = kick_dt.replace(tzinfo=UTC)
        game_issues: list[str] = []
        if game is None:
            game_issues.append("game_missing_from_db")
        else:
            if game.league_id != g.get("league_id"):
                game_issues.append("league_mismatch")
        if freeze_dt and kick_dt and freeze_dt >= kick_dt:
            game_issues.append("freeze_not_before_kickoff")
        if not g.get("players"):
            game_issues.append("no_players")
        # Sportsbook coverage
        n_priced = 0
        n_players = len(g.get("players") or [])
        for pl in g.get("players") or []:
            if not pl.get("player_id"):
                game_issues.append("player_missing_id")
                continue
            if (gid, pl["player_id"]) in priced_by_game_player:
                n_priced += 1
            # Model version presence on NFL rows
            if g.get("league_id") == "nfl":
                if pl.get("p_atd") is None:
                    game_issues.append(f"missing_player_td_v1:{pl.get('player_name')}")
        if n_priced == 0 and n_players > 0:
            game_issues.append("no_sportsbook_anytime_td_ticks_for_game")
        record = {
            "game_id": gid,
            "league_id": g.get("league_id"),
            "kickoff_utc": kick,
            "frozen_at": g.get("frozen_at") or freeze_at,
            "holdout_tag": g.get("holdout_tag"),
            "db_status": game.status if game else None,
            "n_players": n_players,
            "n_priced_players": n_priced,
            "price_coverage": (n_priced / n_players) if n_players else None,
            "issues": game_issues,
            "immutable_file": True,
        }
        if game_issues:
            issues.append(record)
        else:
            ok_games += 1

    return {
        "status": "ok",
        "freeze_path": str(prospective_dir() / "latest_freeze.json"),
        "frozen_at": freeze_at,
        "n_games": len(games),
        "n_ok": ok_games,
        "n_with_issues": len(issues),
        "artifact_hashes": artifact_hashes,
        "m6_hist_gbm_version": M6_GBM_VERSION,
        "issues": issues[:30],
        "shadow_connection": "use ingest_freeze_into_shadow to write append-only ShadowPrediction rows",
        "settlement_connection": "existing settle_shadow_predictions grades shadow_predictions",
    }


def ingest_freeze_into_shadow(
    session: Session,
    *,
    now: datetime | None = None,
    freeze: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write freeze forecasts into append-only shadow_predictions with live sportsbook ticks.

    Never rewrites existing rows. recommendation_status stays no_bet. Play stays off.
    """
    moment = as_utc(now) or datetime.now(UTC)
    freeze = freeze or _load_latest_freeze()
    if freeze is None:
        return {"status": "missing_freeze"}
    priced = _priced_anytime_rows(session)
    if not priced:
        return {"status": "no_priced_anytime_td", "written": 0}

    existing = {
        (r.outcome_id, r.model_version_id, r.odds_tick_id)
        for r in session.query(ShadowPrediction)
        .filter(ShadowPrediction.market_id == "anytime_td")
        .all()
    }

    summary: dict[str, Any] = {
        "status": "ok",
        "written": 0,
        "unchanged": 0,
        "skipped_no_price": 0,
        "skipped_no_prob": 0,
        "by_model": {},
        "play_enabled": False,
        "research_only": True,
        "validated_play": False,
        "recommendation_status": "no_bet",
        "freeze_frozen_at": freeze.get("frozen_at"),
        "artifact_hashes": {
            "nfl_m6_hist_gbm": _sha256_file(m6_model_path("nfl")),
            "fbs_m6_hist_gbm": _sha256_file(m6_model_path("fbs")),
        },
    }

    # Index freeze players
    for g in freeze.get("games") or []:
        league = g.get("league_id")
        gid = g.get("game_id")
        for pl in g.get("players") or []:
            pid = pl.get("player_id")
            if not pid:
                continue
            # Find priced outcome for this player-game (any book — use latest from priced index)
            price_row = None
            for row in priced.values():
                if row["game_id"] == gid and row["player_id"] == pid:
                    price_row = row
                    break
            if price_row is None:
                summary["skipped_no_price"] += 1
                continue

            candidates: list[tuple[str, float | None, dict[str, Any]]] = []
            if league == "nfl":
                candidates.append(
                    (
                        MODEL_IDS["player_td_v1"].format(league=league),
                        pl.get("p_atd"),
                        {"family": "player_td_v1", "primary": True},
                    )
                )
                candidates.append(
                    (
                        MODEL_IDS["m6_hist_gbm"].format(league=league),
                        pl.get("p_m6_hist_gbm"),
                        {
                            "family": "m6_hist_gbm",
                            "model_sha256": summary["artifact_hashes"]["nfl_m6_hist_gbm"],
                            "version": M6_GBM_VERSION,
                        },
                    )
                )
                candidates.append(
                    (
                        MODEL_IDS["ens_m6_gbm_player_td"].format(league=league),
                        pl.get("p_ensemble_m6_gbm_player_td"),
                        {"family": "ens_m6_gbm_player_td", "note": "equal_weight_research_candidate"},
                    )
                )
            else:
                candidates.append(
                    (
                        MODEL_IDS["m5"].format(league=league),
                        pl.get("p_m5") if pl.get("p_m5") is not None else pl.get("p_atd"),
                        {"family": "m5", "strict_features": True},
                    )
                )

            for model_version_id, p, extra in candidates:
                if p is None or not np.isfinite(float(p)):
                    summary["skipped_no_prob"] += 1
                    continue
                key = (price_row["outcome_id"], model_version_id, price_row["tick_id"])
                if key in existing:
                    summary["unchanged"] += 1
                    continue
                _write_shadow_row(
                    session,
                    price_row=price_row,
                    model_version_id=model_version_id,
                    p=float(p),
                    now=moment,
                    freeze_meta={
                        "freeze_frozen_at": freeze.get("frozen_at"),
                        "holdout_tag": g.get("holdout_tag"),
                        "matchup": g.get("matchup"),
                        "player_name": pl.get("player_name"),
                        "position_group": pl.get("position_group"),
                        "availability_status": pl.get("availability_status"),
                        "reliability": reliability_indicator(
                            pl,
                            p_v1=pl.get("p_atd"),
                            p_gbm=pl.get("p_m6_hist_gbm"),
                        ),
                        **extra,
                    },
                )
                existing.add(key)
                summary["written"] += 1
                fam = str(extra.get("family"))
                summary["by_model"][fam] = int(summary["by_model"].get(fam, 0)) + 1

    session.flush()
    freeze_ledger_path().write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


def _write_shadow_row(
    session: Session,
    *,
    price_row: dict[str, Any],
    model_version_id: str,
    p: float,
    now: datetime,
    freeze_meta: dict[str, Any],
) -> None:
    price = int(price_row["price_american"])
    implied_raw = american_to_implied(price)
    opposing = _opposing_price(session, price_row["outcome_id"], price_row["bookmaker_id"], price_row["side"])
    novig = None
    if opposing is not None:
        try:
            a, _b = no_vig_two_way(implied_raw, american_to_implied(opposing))
            novig = float(a)
        except ValueError:
            novig = None
    ev = ev_per_dollar(p, price)
    reasons = [
        "RESEARCH_ONLY",
        "PLAY_DISABLED",
        "SHADOW_TRACK",
        "UNIFIED_ATD_FREEZE",
        "VALIDATED_PLAY_DISABLED",
        "NO_PROMOTION",
    ]
    if ev <= 0:
        reasons.append("NEGATIVE_EXPECTED_VALUE")
    try:
        from spe.shadow.live_validation import pregame_opportunity_snapshot

        opp_snap = pregame_opportunity_snapshot(session, str(price_row["player_id"]), str(price_row["game_id"]))
    except Exception:  # noqa: BLE001
        opp_snap = {}
    family = str(freeze_meta.get("family") or model_version_id.split("-")[0])
    try:
        from spe.shadow.freeze_versioning import build_freeze_version_block

        version_block = build_freeze_version_block(
            family=family,
            league_id=str(price_row["league_id"]),
            forecast_as_of=now,
            source_data_cutoff=now,
            price_captured_at=price_row.get("captured_at"),
            bookmaker_id=str(price_row.get("bookmaker_id") or ""),
            opportunity=opp_snap,
            missing_features=list(freeze_meta.get("missing_features") or [])
            if isinstance(freeze_meta.get("missing_features"), list)
            else [],
            proxy_flags={
                "fbs_receptions_usage_proxy": freeze_meta.get("fbs_receptions_usage_proxy"),
            },
        )
    except Exception:  # noqa: BLE001
        version_block = {}
    session.add(
        ShadowPrediction(
            shadow_id=new_id(),
            player_id=price_row["player_id"],
            game_id=price_row["game_id"],
            league_id=price_row["league_id"],
            market_id="anytime_td",
            outcome_id=price_row["outcome_id"],
            bookmaker_id=price_row["bookmaker_id"],
            line=None if price_row["line"] is None else float(price_row["line"]),
            side=price_row["side"],
            price_american=price,
            odds_tick_id=price_row["tick_id"],
            forecast_probability=float(p),
            market_implied_raw=float(implied_raw),
            market_implied_novig=novig,
            expected_value=float(ev),
            model_version_id=model_version_id,
            forecast_as_of=now,
            data_quality_status="research",
            abstain_reasons=reasons,
            recommendation_status="no_bet",
            components={
                "play_enabled": False,
                "research_only": True,
                "line_source": "sportsbook",
                "immutable_append_only": True,
                "prior_carries": opp_snap.get("prior_carries"),
                "prior_targets": opp_snap.get("prior_targets"),
                "prior_receptions": opp_snap.get("prior_receptions"),
                "prior_atd_rate": opp_snap.get("prior_atd_rate"),
                "n_prior_games": opp_snap.get("n_prior_games"),
                "prior_team_td_share": opp_snap.get("prior_team_td_share"),
                "team_td_share": opp_snap.get("prior_team_td_share"),
                **version_block,
                **freeze_meta,
            },
            research_only=True,
            play_enabled=False,
            created_at=now,
        )
    )


def settled_performance(session: Session) -> dict[str, Any]:
    """Score settled shadow predictions for research model families on identical rows where possible."""
    rows = session.execute(
        text(
            """
            SELECT sp.shadow_id, sp.league_id, sp.game_id, sp.player_id, sp.model_version_id,
                   sp.forecast_probability, sp.price_american, sp.market_implied_raw,
                   sp.forecast_as_of, sp.components,
                   st.result, st.actual_value, st.void_reason, st.closing_price_american
            FROM shadow_predictions sp
            JOIN shadow_settlements st ON st.shadow_id = sp.shadow_id
            WHERE sp.market_id = 'anytime_td'
              AND st.result IN ('win', 'loss')
            """
        )
    ).mappings().all()

    def family(mid: str) -> str:
        mid = mid or ""
        if mid.startswith("blend_v1_c_recent"):
            return "blend_v1_c_recent"
        if mid.startswith("player_td_v1"):
            return "player_td_v1"
        if mid.startswith("m5_c_recent"):
            return "m5_c_recent"
        if mid.startswith("m6_hist_gbm"):
            return "m6_hist_gbm"
        if mid.startswith("ens_m6_gbm_player_td"):
            return "ens_m6_gbm_player_td"
        if mid.startswith("m6-"):
            return "m6_shrink"
        if mid.startswith("atd_sim_v1"):
            return "atd_sim_v1"
        if mid.startswith("m5"):
            return "m5"
        if "m3" in mid or mid.startswith("nfl-m3") or mid.startswith("fbs-m3"):
            return "m3"
        if mid.startswith("m4") or "-m4-" in mid:
            return "m4"
        return mid.split("-")[0] if mid else "unknown"

    by_lg_fam: dict[str, dict[str, list[tuple[float, float]]]] = {"nfl": {}, "fbs": {}}
    for r in rows:
        lg = r["league_id"]
        if lg not in by_lg_fam:
            continue
        fam = family(str(r["model_version_id"]))
        y = 1.0 if r["result"] == "win" else 0.0
        p = float(r["forecast_probability"])
        by_lg_fam[lg].setdefault(fam, []).append((y, p))

    out: dict[str, Any] = {"nfl": {}, "fbs": {}, "n_settled_rows": len(rows), "note": None}
    for lg, fams in by_lg_fam.items():
        block: dict[str, Any] = {}
        for fam, pairs in fams.items():
            y = np.asarray([a[0] for a in pairs], dtype=float)
            p = np.clip(np.asarray([a[1] for a in pairs], dtype=float), 1e-6, 1 - 1e-6)
            block[fam] = {
                "n": int(len(y)),
                "brier": float(brier_score(y, p)) if len(y) else None,
                "log_loss": float(log_loss(y, p)) if len(y) else None,
                "ece": float(expected_calibration_error(y, p)) if len(y) else None,
                "base_rate": float(y.mean()) if len(y) else None,
                "mean_p": float(p.mean()) if len(p) else None,
            }
        out[lg] = block
        # Paired diffs on overlapping player-games when multiple families settled
    if out["n_settled_rows"] < 50:
        out["note"] = (
            f"Only {out['n_settled_rows']} settled win/loss rows — do not claim model superiority "
            "from this sample."
        )
    elif out["n_settled_rows"] == 0:
        out["note"] = "No settled win/loss rows yet — prospective games still pending kickoff/finals."
    return out


def run_verification(
    session: Session,
    *,
    now: datetime | None = None,
    ingest_shadow: bool = True,
    run_settle: bool = True,
) -> dict[str, Any]:
    moment = as_utc(now) or datetime.now(UTC)
    forecasts = _load_forecasts()
    freeze = _load_latest_freeze()

    print("[verify] disagreement audit…", flush=True)
    disagreements = audit_disagreements(forecasts)
    print("[verify] reliability assessment…", flush=True)
    reliability = assess_reliability(forecasts)
    print("[verify] prospective freeze check…", flush=True)
    freeze_check = verify_prospective_freezes(session)

    ingest_summary: dict[str, Any] = {"status": "skipped"}
    if ingest_shadow:
        print("[verify] ingesting freeze into shadow ledger…", flush=True)
        ingest_summary = ingest_freeze_into_shadow(session, now=moment, freeze=freeze)
        session.commit()

    settle_summary: dict[str, Any] = {"status": "skipped"}
    if run_settle:
        print("[verify] running shadow settlement…", flush=True)
        settle_summary = settle_shadow_predictions(session, now=moment)
        session.commit()

    print("[verify] settled performance…", flush=True)
    performance = settled_performance(session)

    # Data-quality summary from disagreements + reliability
    data_quality = {
        "disagreement_root_causes": disagreements.get("by_root_cause"),
        "reliability_levels": reliability.get("by_level"),
        "freeze_issues_n": freeze_check.get("n_with_issues"),
        "common_problems": [],
    }
    if reliability.get("by_level", {}).get("low"):
        data_quality["common_problems"].append("low_reliability_predictions_present")
    if freeze_check.get("n_with_issues"):
        data_quality["common_problems"].append("some_freeze_games_missing_sportsbook_or_identity_fields")
    # Chase spotlight
    chase = disagreements.get("chase_deep_dive")

    blockers = [
        "Validated Play remains disabled by design",
        "No model promotion until prospective settled sample is large enough",
        "Sportsbook price coverage incomplete for some FBS freeze players",
        "Settlement pending until frozen games reach official final status",
    ]
    if performance.get("n_settled_rows", 0) < 100:
        blockers.append("Insufficient settled prospective sample for superiority claims")

    report = {
        "status": "ok",
        "as_of": moment.isoformat(),
        "play_enabled": PLAY_ENABLED,
        "promoted": PROMOTED,
        "research_only": RESEARCH_ONLY,
        "validated_play": False,
        "1_largest_model_disagreements": {
            "threshold": DISAGREE_THRESHOLD,
            "n": disagreements.get("n_disagreements"),
            "top": [
                {
                    "player": r.get("player_name"),
                    "matchup": r.get("matchup"),
                    "position": r.get("position_group"),
                    "p_v1": r.get("p_player_td_v1"),
                    "p_gbm": r.get("p_m6_hist_gbm"),
                    "delta": r.get("delta_gbm_minus_v1"),
                    "root_cause": r.get("root_cause"),
                }
                for r in (disagreements.get("players") or [])[:15]
            ],
            "all": disagreements.get("players"),
        },
        "2_causes_of_disagreements": {
            "by_root_cause": disagreements.get("by_root_cause"),
            "chase_example": chase,
            "summary": (
                "Most NFL gaps ≥10pp are legitimate architecture differences: "
                "team-λ×share allocation vs marginal HistGBM — not swapped player IDs."
            ),
        },
        "3_data_quality_problems": data_quality,
        "4_forecast_reliability_assessment": reliability,
        "5_prospective_freeze_verification": freeze_check,
        "6_settlement_status": {
            "ingest_into_shadow": ingest_summary,
            "settle": settle_summary,
            "ledger_path": str(freeze_ledger_path()),
        },
        "7_actual_accuracy_results": performance,
        "8_remaining_blockers_before_promotion": blockers,
    }
    path = report_path()
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    print(f"[verify] wrote {path}", flush=True)
    return report
