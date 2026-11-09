"""Anytime TD reliability guardrails — research transparency only.

Flags disagreements and sparse-history risk. Tests hierarchical fallbacks
without changing official probabilities. Never invents CIs. Never promotes.
Validated Play stays off.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.models.rolling_td import load_m5
from spe.research.atd_features import extract_feature_dict
from spe.research.atd_forecast import forecast_path, research_dir
from spe.research.atd_verify import (
    DISAGREE_THRESHOLD,
    audit_disagreements,
    explain_disagreement,
    reliability_indicator,
    settled_performance,
    verify_prospective_freezes,
)
from spe.research.m6_hist_gbm import load_m6_hist_gbm
from spe.shadow.settle import settle_shadow_predictions
from spe.simulate.correlated_atd.player_weights import load_m6, _m6_predict_simple

SPARSE_N = 5
STALE_PRICE_HOURS = 12
FOCUS_PLAYERS = ("Jonah Coleman", "Haynes King", "Dallen Bentley")
WARNING_CATALOG = {
    "large_model_disagreement": "Player TD v1 and M6 HistGBM differ by more than 10 percentage points.",
    "extreme_model_disagreement": "Model gap exceeds 20 percentage points — inspect both estimates before trusting either.",
    "limited_player_history": "Fewer than 5 prior games in the research sample.",
    "no_prior_games": "No prior box-score games for this player in the research frame.",
    "unknown_availability": "Injury/availability status is unknown — not assumed healthy.",
    "availability_risk": "Player is doubtful or out on the latest injury designation.",
    "missing_or_defaulted_defense": "Opponent defensive feature missing or defaulted to league average.",
    "missing_current_season_td_signal": "No prior or roll3 TD rate available.",
    "stale_sportsbook_price": "Latest sportsbook tick is older than the research freshness window.",
    "sparse_history_extrapolation": "Extreme probability may reflect extrapolation from thin history.",
    "hierarchical_fallback_suggested": "Research-only: hierarchical prior is far from the model estimate (not applied).",
    "m5_artifact_rate_vs_current_form": (
        "M5 primary probability remains high/low relative to current-season TD rate — "
        "artifact player rates may outweigh live form until M5 is explicitly retrained."
    ),
    "research_only_not_a_bet": "Research forecast only. Not a validated betting recommendation. Play disabled.",
    "involved_conditional_not_game_level": (
        "Locked C_recent is involved-conditional and has no current-role participation term. "
        "Raw score is retained for inspection only — not ranked as a game-level projection."
    ),
}


def report_path() -> Path:
    return research_dir() / "reliability_guardrails_report.json"


def finder_payload_path() -> Path:
    return research_dir() / "anytime_td_finder.json"


def _load_forecasts() -> dict[str, Any]:
    path = forecast_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def build_warnings(
    player: dict[str, Any],
    *,
    league_id: str,
    p_v1: float | None,
    p_gbm: float | None,
    price_captured_at: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """Research warnings — not confidence intervals and not probability edits."""
    moment = as_utc(now) or datetime.now(UTC)
    rel = reliability_indicator(player, p_v1=p_v1, p_gbm=p_gbm)
    warnings: list[dict[str, str]] = [
        {"code": "research_only_not_a_bet", "message": WARNING_CATALOG["research_only_not_a_bet"]}
    ]
    for code in rel.get("flags") or []:
        if code in WARNING_CATALOG:
            warnings.append({"code": code, "message": WARNING_CATALOG[code]})
    if price_captured_at:
        try:
            captured = datetime.fromisoformat(str(price_captured_at).replace("Z", "+00:00"))
            if captured.tzinfo is None:
                captured = captured.replace(tzinfo=UTC)
            if moment - captured > timedelta(hours=STALE_PRICE_HOURS):
                warnings.append(
                    {"code": "stale_sportsbook_price", "message": WARNING_CATALOG["stale_sportsbook_price"]}
                )
        except Exception:
            pass
    # Deduplicate by code
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for w in warnings:
        if w["code"] in seen:
            continue
        seen.add(w["code"])
        out.append(w)
    return out


def hierarchical_fallback_test(
    player: dict[str, Any],
    *,
    league_id: str,
    p_model: float | None,
    model_name: str,
) -> dict[str, Any]:
    """Compute hierarchical shrink prior and compare — does NOT replace official p."""
    art = load_m5(league_id) or load_m6(league_id)
    if art is None:
        return {"status": "no_hierarchical_artifact", "applied": False}
    pid = str(player.get("player_id") or "")
    pos = str(player.get("position_group") or "UNK")
    team = str(player.get("team_id") or "")
    k = float(art.get("k_player") or 12.0)
    league = float(art.get("league_rate") or 0.15)
    pos_rate = float((art.get("pos_rates") or {}).get(pos, league))
    team_rate = float((art.get("team_rates") or {}).get(team, pos_rate))
    n = int((art.get("player_n") or {}).get(pid, player.get("sample_size") or 0) or 0)
    raw = (art.get("player_rates") or {}).get(pid)
    if raw is None:
        prior_mean = team_rate if n == 0 else pos_rate
    else:
        shrink = n / (n + k) if n > 0 else 0.0
        prior_mean = shrink * float(raw) + (1.0 - shrink) * team_rate
    # Convert mean TD rate → anytime probability via 1-exp(-λ) when mean looks like a rate.
    p_hier = float(1.0 - np.exp(-max(1e-6, prior_mean)))
    # Also try M6 simple if this is the m6 artifact family.
    if art.get("version") == "m6_player_td" or "m6" in str(art.get("version") or ""):
        p6, mean6 = _m6_predict_simple(art, pid, pos)
        if p6 is not None:
            p_hier = float(p6)
            prior_mean = float(mean6 or prior_mean)

    delta = None if p_model is None else float(p_model) - p_hier
    suggest = False
    reason = "ok"
    sample = int(player.get("sample_size") or n or 0)
    if sample < SPARSE_N and p_model is not None and abs(delta or 0) >= 0.10:
        suggest = True
        reason = "sparse_history_and_far_from_hierarchical_prior"
    elif sample < SPARSE_N and p_model is not None and (p_model < 0.02 or p_model > 0.45):
        suggest = True
        reason = "sparse_history_extreme_probability"

    return {
        "status": "ok",
        "applied": False,
        "note": "Research test only — official probabilities unchanged",
        "model_name": model_name,
        "p_model": p_model,
        "p_hierarchical": p_hier,
        "prior_mean_td_rate": prior_mean,
        "player_n": n,
        "k_player": k,
        "pos_rate": pos_rate,
        "team_rate": team_rate,
        "delta_model_minus_hier": delta,
        "fallback_suggested": suggest,
        "suggestion_reason": reason if suggest else None,
    }


def investigate_sparse_player(player: dict[str, Any], game: dict[str, Any], league_id: str) -> dict[str, Any]:
    p_v1 = player.get("p_atd")
    p_gbm = player.get("p_m6_hist_gbm")
    sample = int(player.get("sample_size") or 0)
    gbm_feats = {}
    loaded = load_m6_hist_gbm(league_id) if league_id == "nfl" else None
    if loaded and league_id == "nfl":
        feats = list(loaded["blob"]["features"])
        row = {
            **player,
            "prior_carries": player.get("prior_carries", player.get("exp_carries")),
            "prior_targets": player.get("prior_targets", player.get("exp_targets")),
            "prior_rz_targets": player.get("prior_rz_targets", player.get("exp_rz_targets")),
            "prior_gl_carries": player.get("prior_gl_carries", player.get("exp_gl_carries")),
        }
        observed, problems = extract_feature_dict(row, feats, strict=False)
        gbm_feats = {"observed": observed, "missing_or_nan": problems, "n_schema": len(feats)}

    fb_v1 = hierarchical_fallback_test(player, league_id=league_id, p_model=p_v1, model_name="player_td_v1")
    fb_gbm = hierarchical_fallback_test(player, league_id=league_id, p_model=p_gbm, model_name="m6_hist_gbm")

    diagnosis = "insufficient_data"
    if sample == 0:
        diagnosis = "no_history_model_extrapolation"
    elif sample < SPARSE_N and gbm_feats.get("missing_or_nan"):
        diagnosis = "insufficient_data_and_missing_features"
    elif sample < SPARSE_N and p_gbm is not None and (float(p_gbm) < 0.02 or float(p_gbm) > 0.4):
        diagnosis = "insufficient_data_extreme_gbm"
    elif sample < SPARSE_N:
        diagnosis = "insufficient_data"

    return {
        "player_id": player.get("player_id"),
        "player_name": player.get("player_name"),
        "team_id": player.get("team_id"),
        "position_group": player.get("position_group"),
        "game_id": game.get("game_id"),
        "matchup": f"{game.get('away')} @ {game.get('home')}",
        "sample_size": sample,
        "availability_status": player.get("availability_status"),
        "p_player_td_v1": p_v1,
        "p_m6_hist_gbm": p_gbm,
        "abs_disagreement": (
            abs(float(p_v1) - float(p_gbm))
            if p_v1 is not None and p_gbm is not None
            else None
        ),
        "workload": {
            "exp_carries": player.get("exp_carries"),
            "exp_targets": player.get("exp_targets"),
            "exp_rz_targets": player.get("exp_rz_targets"),
            "exp_gl_carries": player.get("exp_gl_carries"),
            "prior_td_rate": player.get("prior_td_rate"),
            "roll3_td_rate": player.get("roll3_td_rate"),
        },
        "missing_information": player.get("missing_information"),
        "gbm_features": gbm_feats,
        "diagnosis": diagnosis,
        "hierarchical_fallback_tests": {"player_td_v1": fb_v1, "m6_hist_gbm": fb_gbm},
        "official_probabilities_changed": False,
        "recommendation": (
            "Abstain or treat as research-only until more games accumulate; "
            "do not average models to hide the gap."
        ),
    }


def enrich_player_for_finder(
    player: dict[str, Any],
    game: dict[str, Any],
    *,
    league_id: str,
    price_captured_at: str | None = None,
    now: datetime | None = None,
    fbs_scope_ctx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scoped = player
    if league_id == "fbs" and fbs_scope_ctx is not None:
        from spe.research.fbs_game_level_scope import (
            apply_scope_to_research_player,
            classify_fbs_projection_scope,
        )

        scope = classify_fbs_projection_scope(
            player_id=str(player.get("player_id") or ""),
            team_id=str(player.get("team_id") or "") or None,
            position_group=str(player.get("position_group") or "") or None,
            ctx=fbs_scope_ctx,
        )
        scoped = apply_scope_to_research_player(player, scope)

    p_v1 = scoped.get("p_atd") if league_id == "nfl" else scoped.get("p_player_td_v1_ref")
    p_gbm = scoped.get("p_m6_hist_gbm")
    # FBS research primary = locked C_recent (experimental). Never silently use M5 as C_recent.
    p_c_recent = scoped.get("p_c_recent") if league_id == "fbs" else None
    p_m5 = scoped.get("p_m5_legacy") if league_id == "fbs" else None
    if p_m5 is None and league_id == "fbs":
        p_m5 = scoped.get("p_m5")
    if league_id == "fbs":
        # May be None when C_recent unavailable OR demoted from game-level ranking.
        # When both player_td_v1 and C_recent exist, primary is their blend (same as props overlay)
        # so research board and props board stay consistent. Never silently use M5.
        if scoped.get("suppress_game_level_primary") or scoped.get("game_level_supported") is False:
            p_primary = None
        elif p_c_recent is not None and p_v1 is not None:
            p_primary = 0.60 * float(p_v1) + 0.40 * float(p_c_recent)
        else:
            p_primary = p_c_recent  # may be None → UI shows unavailable; do not fall back to M5
        if p_primary is not None:
            try:
                from spe.shadow.atd_role_calibration import apply_role_calibration

                carries = scoped.get("prior_carries")
                if carries is None:
                    carries = scoped.get("exp_carries")
                p_primary = apply_role_calibration(
                    float(p_primary),
                    position_group=str(scoped.get("position_group") or ""),
                    prior_carries=None if carries is None else float(carries),
                )
            except Exception:  # noqa: BLE001
                pass
    else:
        p_primary = scoped.get("p_atd")
        if p_primary is not None:
            try:
                from spe.shadow.atd_role_calibration import apply_role_calibration

                carries = scoped.get("prior_carries")
                if carries is None:
                    carries = scoped.get("exp_carries")
                p_primary = apply_role_calibration(
                    float(p_primary),
                    position_group=str(scoped.get("position_group") or ""),
                    prior_carries=None if carries is None else float(carries),
                )
            except Exception:  # noqa: BLE001
                pass

    warnings = build_warnings(
        scoped,
        league_id=league_id,
        p_v1=float(p_v1) if p_v1 is not None else None,
        p_gbm=float(p_gbm) if p_gbm is not None else None,
        price_captured_at=price_captured_at,
        now=now,
    )
    # Preserve scope warnings attached before enrich.
    for w in scoped.get("research_warnings") or []:
        if isinstance(w, dict) and w.get("code"):
            warnings.append(w)
    disagreement = None
    if league_id == "nfl" and p_v1 is not None and p_gbm is not None:
        if abs(float(p_v1) - float(p_gbm)) >= DISAGREE_THRESHOLD:
            disagreement = explain_disagreement(scoped, game)

    sample = int(scoped.get("sample_size") or 0)
    sparse = None
    if sample < SPARSE_N and league_id == "nfl":
        sparse = investigate_sparse_player(scoped, game, league_id)
        if sparse.get("hierarchical_fallback_tests", {}).get("m6_hist_gbm", {}).get("fallback_suggested"):
            warnings.append(
                {
                    "code": "hierarchical_fallback_suggested",
                    "message": WARNING_CATALOG["hierarchical_fallback_suggested"],
                }
            )
        if sparse.get("diagnosis", "").startswith("insufficient"):
            warnings.append(
                {
                    "code": "sparse_history_extrapolation",
                    "message": WARNING_CATALOG["sparse_history_extrapolation"],
                }
            )

    # FBS research: C_recent primary; legacy M5 comparison only.
    if league_id == "fbs":
        if p_c_recent is None and not scoped.get("suppress_game_level_primary") and scoped.get("game_level_supported") is not False:
            warnings.append(
                {
                    "code": "c_recent_estimate_unavailable",
                    "message": scoped.get("c_recent_unavailable_reason")
                    or "Research estimate unavailable — C_recent could not score this row. Legacy M5 is not substituted.",
                }
            )
        recent8 = scoped.get("recent8_td_rate")
        try:
            r8 = float(recent8) if recent8 is not None else None
        except (TypeError, ValueError):
            r8 = None
        if p_c_recent is not None and r8 is not None and r8 <= 0.05 and float(p_c_recent) >= 0.35:
            warnings.append(
                {
                    "code": "recent_form_conflict",
                    "message": (
                        "C_recent primary is elevated while recent-8 TD rate is near zero — "
                        "inspect role/usage; experimental research only."
                    ),
                }
            )
        if scoped.get("opp_def_z_defaulted") or "opp_def_z_defaulted_to_league_average" in (
            scoped.get("missing_information") or []
        ):
            warnings.append(
                {
                    "code": "missing_or_defaulted_defense",
                    "message": WARNING_CATALOG["missing_or_defaulted_defense"],
                }
            )
        if "fbs_true_targets_unavailable_receptions_proxy_separate" in (scoped.get("missing_information") or []):
            warnings.append(
                {
                    "code": "fbs_receptions_proxy_not_targets",
                    "message": "FBS receptions are a usage proxy — not measured targets or target share.",
                }
            )

    # Deduplicate warnings again
    seen: set[str] = set()
    uniq_warnings: list[dict[str, str]] = []
    for w in warnings:
        if w["code"] in seen:
            continue
        seen.add(w["code"])
        uniq_warnings.append(w)

    primary_label = (
        scoped.get("primary_model_label")
        if league_id == "fbs"
        else "Player TD v1 — research"
    )
    if league_id == "fbs":
        if p_primary is not None and p_c_recent is not None and p_v1 is not None:
            primary_label = "player_td_v1 + C_recent blend — experimental research"
        elif not primary_label:
            primary_label = "C_recent — experimental research"

    raw_involved = None
    if league_id == "fbs" and p_c_recent is not None and p_primary is None:
        raw_involved = float(p_c_recent)

    return {
        "player_id": scoped.get("player_id"),
        "player_name": scoped.get("player_name"),
        "team_id": scoped.get("team_id"),
        "team_name": scoped.get("team_name"),
        "position_group": scoped.get("position_group"),
        "availability_status": scoped.get("availability_status"),
        "availability_prob": scoped.get("availability_prob"),
        "sample_size": sample,
        "probabilities": {
            "player_td_v1": p_v1,
            "m6_hist_gbm": p_gbm,
            "m5": p_m5,
            "c_recent": p_c_recent if p_primary is not None else None,
            "c_recent_raw_involved_conditional": raw_involved,
            "primary_research": p_primary,
            "ensemble_reference_only": scoped.get("p_ensemble_m6_gbm_player_td"),
        },
        "model_source": {
            "primary": (
                "blend_player_td_v1_c_recent"
                if (
                    league_id == "fbs"
                    and p_primary is not None
                    and p_c_recent is not None
                    and p_v1 is not None
                )
                else (scoped.get("primary_model") if league_id == "fbs" else "player_td_v1")
            ),
            "primary_label": primary_label if league_id == "fbs" else "Player TD v1 — research",
            "legacy_m5_label": "Legacy M5 comparison" if league_id == "fbs" else None,
            "locked_version": scoped.get("locked_version"),
            "pipeline_version": scoped.get("pipeline_version"),
            "prediction_timestamp": scoped.get("prediction_timestamp"),
            "prediction_source": scoped.get("prediction_source"),
            "price_captured_at": price_captured_at,
            "accuracy_unproven": True if league_id == "fbs" else None,
            "projection_scope": scoped.get("projection_scope"),
            "scope_version": scoped.get("scope_version"),
        },
        "projection_scope": scoped.get("projection_scope"),
        "game_level_supported": scoped.get("game_level_supported"),
        "projection_scope_evidence": scoped.get("projection_scope_evidence"),
        "projection_scope_reasons": scoped.get("projection_scope_reasons"),
        "team_scoring": {
            "team_lambda_off_td": scoped.get("team_lambda_off_td"),
            "rush_td_share": scoped.get("rush_td_share"),
            "rec_td_share": scoped.get("rec_td_share"),
            "lambda_player_td": scoped.get("lambda_player_td"),
        },
        "key_features": {
            "prior_td_rate": scoped.get("prior_td_rate"),
            "prior_td_rate_all_teams": scoped.get("prior_td_rate_all_teams"),
            "season_td_rate": scoped.get("season_td_rate"),
            "recent8_td_rate": scoped.get("recent8_td_rate"),
            "season_td_rate_current_team": scoped.get("season_td_rate_current_team"),
            "recent8_td_rate_current_team": scoped.get("recent8_td_rate_current_team"),
            "roll3_td_rate": scoped.get("roll3_td_rate"),
            "exp_carries": scoped.get("exp_carries"),
            "exp_targets": scoped.get("exp_targets"),
            "exp_rz_targets": scoped.get("exp_rz_targets"),
            "exp_gl_carries": scoped.get("exp_gl_carries"),
            "prior_target_share": scoped.get("prior_target_share"),
            "prior_carry_share": scoped.get("prior_carry_share"),
            "opp_def_z": scoped.get("opp_def_z"),
            "fbs_receptions_usage_proxy": scoped.get("fbs_receptions_usage_proxy"),
            "current_team_sample_size": scoped.get("current_team_sample_size"),
        },
        "missing_information": scoped.get("missing_information") or [],
        "research_warnings": uniq_warnings,
        "disagreement": (
            {
                "abs_delta": disagreement.get("abs_delta") if disagreement else None,
                "delta_gbm_minus_v1": disagreement.get("delta_gbm_minus_v1") if disagreement else None,
                "root_cause": disagreement.get("root_cause") if disagreement else None,
                "root_detail": disagreement.get("root_detail") if disagreement else None,
                "causes": (disagreement.get("causes") if disagreement else None),
            }
            if disagreement
            else None
        ),
        "sparse_history": sparse,
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "validated_play": False,
        "recommendation_status": "no_bet",
        "disclaimer": (
            "Research forecast only. Not betting advice. "
            "Probabilities are not averaged to hide disagreement. Validated Play is disabled."
        ),
    }


def build_finder_payload(
    session: Session,
    *,
    now: datetime | None = None,
    refresh_fbs_c_recent: bool = True,
) -> dict[str, Any]:
    """Materialize Anytime TD Finder research rows with dual probs and warnings.

    FBS primary research estimate is locked C_recent (experimental). NFL unchanged.
    """
    moment = as_utc(now) or datetime.now(UTC)
    forecasts = _load_forecasts()
    fbs_serve_meta: dict[str, Any] = {}
    if refresh_fbs_c_recent:
        from spe.research.fbs_c_recent_serve import score_fbs_forecasts_with_c_recent

        fbs_live = score_fbs_forecasts_with_c_recent(session, now=moment)
        fbs_serve_meta = {
            "serving": fbs_live.get("serving"),
            "counts": fbs_live.get("counts"),
            "status": fbs_live.get("status"),
        }
        if fbs_live.get("status") == "ok":
            forecasts = {
                **forecasts,
                "leagues": {
                    **(forecasts.get("leagues") or {}),
                    "fbs": (fbs_live.get("leagues") or {}).get("fbs") or {},
                },
            }

    from spe.research.atd_research_prices import attach_research_price, load_anytime_td_prices_for_games
    from spe.research.fbs_game_level_scope import SCOPE_VERSION, load_fbs_usage_context

    season_year = moment.year if moment.month >= 2 else moment.year - 1
    fbs_scope_ctx = load_fbs_usage_context(session, season_year=season_year, as_of=moment)

    # Existing DB quotes only — no Odds API spend.
    all_game_ids: list[str] = []
    for lg, block in (forecasts.get("leagues") or {}).items():
        for game in block.get("games") or []:
            gid = game.get("game_id")
            if gid:
                all_game_ids.append(str(gid))
    price_index = load_anytime_td_prices_for_games(session, all_game_ids, now=moment)

    games_out: list[dict[str, Any]] = []
    n_warn = 0
    n_disagree = 0
    n_scope_demoted = 0
    n_game_level = 0
    n_priced = 0
    n_unpriced = 0
    for lg, block in (forecasts.get("leagues") or {}).items():
        for game in block.get("games") or []:
            players_out = []
            for pl in game.get("players") or []:
                enriched = enrich_player_for_finder(
                    pl,
                    game,
                    league_id=lg,
                    now=moment,
                    fbs_scope_ctx=fbs_scope_ctx if lg == "fbs" else None,
                )
                enriched = attach_research_price(
                    enriched,
                    game_id=str(game.get("game_id") or ""),
                    price_index=price_index,
                )
                sb = enriched.get("sportsbook") or {}
                if sb.get("status") == "priced":
                    n_priced += 1
                else:
                    n_unpriced += 1
                if enriched.get("disagreement"):
                    n_disagree += 1
                n_warn += len(enriched.get("research_warnings") or [])
                if lg == "fbs":
                    if enriched.get("game_level_supported") is False:
                        n_scope_demoted += 1
                    elif enriched.get("game_level_supported") is True:
                        n_game_level += 1
                players_out.append(enriched)
            players_out.sort(
                key=lambda p: -(
                    float(p["probabilities"].get("primary_research") or 0)
                    if p["probabilities"].get("primary_research") is not None
                    else -1
                )
            )
            games_out.append(
                {
                    "game_id": game.get("game_id"),
                    "league_id": lg,
                    "kickoff_utc": game.get("kickoff_utc"),
                    "home": game.get("home"),
                    "away": game.get("away"),
                    "team_expectations": game.get("team_expectations"),
                    "players": players_out,
                }
            )
    payload = {
        "status": "ok",
        "as_of": moment.isoformat(),
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "validated_play": False,
        "fbs_primary_model": "m5_c_recent",
        "fbs_primary_label": "player_td_v1 + C_recent blend — experimental research",
        "fbs_scope_version": SCOPE_VERSION,
        "fbs_scope_counts": {
            "game_level_supported": n_game_level,
            "demoted_not_game_level": n_scope_demoted,
        },
        "sportsbook_counts": {
            "priced_players": n_priced,
            "unpriced_players": n_unpriced,
            "source": "existing_odds_ticks_only",
        },
        "fbs_serving": fbs_serve_meta,
        "disclaimer": (
            "Caliber Anytime TD Finder — research mode. "
            "FBS primary estimate is C_recent (experimental research); accuracy superiority unproven. "
            "Not a validated betting recommendation. Validated Play disabled."
        ),
        "warning_catalog": WARNING_CATALOG,
        "n_games": len(games_out),
        "n_disagreement_flags": n_disagree,
        "n_warning_instances": n_warn,
        "games": games_out,
    }
    finder_payload_path().write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def run_guardrails(
    session: Session,
    *,
    now: datetime | None = None,
    run_settle: bool = True,
) -> dict[str, Any]:
    moment = as_utc(now) or datetime.now(UTC)
    forecasts = _load_forecasts()
    print("[guardrails] disagreement detection…", flush=True)
    disagreements = audit_disagreements(forecasts)

    print("[guardrails] sparse-history investigation…", flush=True)
    sparse_cases: list[dict[str, Any]] = []
    focus_hits: list[dict[str, Any]] = []
    for game in ((forecasts.get("leagues") or {}).get("nfl") or {}).get("games") or []:
        for pl in game.get("players") or []:
            name = str(pl.get("player_name") or "")
            sample = int(pl.get("sample_size") or 0)
            if name in FOCUS_PLAYERS or sample < SPARSE_N:
                inv = investigate_sparse_player(pl, game, "nfl")
                sparse_cases.append(inv)
                if name in FOCUS_PLAYERS:
                    focus_hits.append(inv)

    print("[guardrails] building finder payload…", flush=True)
    finder = build_finder_payload(session, now=moment)

    print("[guardrails] freeze + settlement check…", flush=True)
    freeze_check = verify_prospective_freezes(session)
    settle_summary: dict[str, Any] = {"status": "skipped"}
    if run_settle:
        settle_summary = settle_shadow_predictions(session, now=moment)
        session.commit()
    performance = settled_performance(session)

    # Warning inventory from finder
    warning_counts: dict[str, int] = {}
    for g in finder.get("games") or []:
        for pl in g.get("players") or []:
            for w in pl.get("research_warnings") or []:
                warning_counts[w["code"]] = warning_counts.get(w["code"], 0) + 1

    report = {
        "status": "ok",
        "as_of": moment.isoformat(),
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "validated_play": False,
        "official_probabilities_unchanged": True,
        "1_model_disagreements": {
            "n": disagreements.get("n_disagreements"),
            "threshold": DISAGREE_THRESHOLD,
            "by_root_cause": disagreements.get("by_root_cause"),
            "top": [
                {
                    "player": r.get("player_name"),
                    "p_v1": r.get("p_player_td_v1"),
                    "p_gbm": r.get("p_m6_hist_gbm"),
                    "abs_delta": r.get("abs_delta"),
                    "sample_size": (r.get("workload") or {}).get("sample_size"),
                    "root_cause": r.get("root_cause"),
                    "availability": next(
                        (
                            c
                            for c in (r.get("consistency_checks") or [])
                            if c.startswith("availability=")
                        ),
                        None,
                    ),
                }
                for r in (disagreements.get("players") or [])[:20]
            ],
        },
        "2_sparse_history": {
            "focus_players": focus_hits,
            "all_sparse_n": len(sparse_cases),
            "cases": sparse_cases,
            "hierarchical_fallback_policy": (
                "Tested and recorded only. Official probabilities not modified. "
                "Requires out-of-sample validation before any production fallback."
            ),
        },
        "3_warnings_added": {
            "catalog": WARNING_CATALOG,
            "counts": warning_counts,
            "n_warning_instances": finder.get("n_warning_instances"),
            "note": "Warnings are research triage labels, not statistical confidence intervals.",
        },
        "4_finder": {
            "payload_path": str(finder_payload_path()),
            "n_games": finder.get("n_games"),
            "n_disagreement_flags": finder.get("n_disagreement_flags"),
            "api": "/v1/anytime-td/research",
        },
        "5_prospective_tracking": {
            "freeze": freeze_check,
            "settle": settle_summary,
            "accuracy": performance,
        },
        "6_remaining_blockers": [
            "Validated Play disabled",
            "Hierarchical fallbacks not applied to official forecasts (await OOS validation)",
            "Need settled prospective sample to decide which model wins when they disagree",
            "Incomplete sportsbook coverage on some FBS freeze games",
        ],
    }
    path = report_path()
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    print(f"[guardrails] wrote {path}", flush=True)
    return report
