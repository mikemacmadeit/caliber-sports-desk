"""Sportsbook comparison for frozen Anytime TD shadow predictions.

Research-only. Does not claim profitable performance without settlement evidence.
Uses no-vig probabilities only when both sides exist at the same book.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.market.odds import american_to_implied, ev_per_dollar
from spe.models.metrics import brier_score, expected_calibration_error, log_loss
from spe.simulate.correlated_atd.phase5_isolation import phase5_dir
from spe.simulate.correlated_atd.scoring import bucket_metrics, bootstrap_ci


def compare_sportsbooks(session: Session) -> dict[str, Any]:
    rows = session.execute(
        text(
            """
            SELECT s.shadow_id, s.league_id, s.model_version_id, s.forecast_probability,
                   s.price_american, s.market_implied_raw, s.market_implied_novig,
                   s.expected_value, s.bookmaker_id, s.player_id, s.game_id,
                   s.recommendation_status, s.research_only, s.play_enabled,
                   st.result, st.flat_stake_return, st.closing_price_american, st.closing_line_value,
                   st.actual_value
            FROM shadow_predictions s
            LEFT JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
            WHERE s.market_id = 'anytime_td'
              AND (
                s.model_version_id LIKE 'm6-%'
                OR s.model_version_id LIKE 'atd_sim_v1-%'
                OR s.model_version_id LIKE 'm3-%anytime_td'
                OR s.model_version_id LIKE 'm5-%'
              )
            """
        )
    ).mappings().all()

    report: dict[str, Any] = {
        "n_rows": len(rows),
        "disclaimer": (
            "Hypothetical EV and flat-stake figures are research diagnostics only. "
            "Do not claim profitable performance before real settlement evidence exists. "
            "Validated Play remains disabled."
        ),
        "families": {},
        "settled_only": {},
        "novig_policy": "no_vig used only when market_implied_novig is present (both sides at same book)",
    }
    if not rows:
        report["status"] = "no_shadow_anytime_rows"
        path = phase5_dir() / "sportsbook_comparison.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        report["artifact_path"] = str(path)
        return report

    by_family: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        fam = _family(r["model_version_id"])
        by_family.setdefault(fam, []).append(dict(r))

    for fam, items in by_family.items():
        report["families"][fam] = _family_block(items, settled=False)
        settled = [x for x in items if x.get("result") in {"win", "loss"}]
        report["settled_only"][fam] = _family_block(settled, settled=True) if settled else {"n": 0, "note": "no_settlements_yet"}

    path = phase5_dir() / "sportsbook_comparison.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["artifact_path"] = str(path)
    return report


def _family(model_version_id: str) -> str:
    mv = model_version_id or ""
    if mv.startswith("atd_sim_v1"):
        return "atd_sim_v1"
    if mv.startswith("m6-"):
        return "m6"
    if mv.startswith("m5-"):
        return "m5"
    if mv.startswith("m3-"):
        return "m3"
    return "other"


def _family_block(items: list[dict[str, Any]], *, settled: bool) -> dict[str, Any]:
    p = np.array([float(x["forecast_probability"]) for x in items], dtype=float)
    raw = np.array([float(x["market_implied_raw"]) for x in items], dtype=float)
    prices = np.array([int(x["price_american"]) for x in items], dtype=int)
    breakeven = raw  # raw implied is the price breakeven (with vig)
    ev = np.array([float(x["expected_value"]) if x["expected_value"] is not None else ev_per_dollar(float(x["forecast_probability"]), int(x["price_american"])) for x in items])

    novig_mask = np.array([x.get("market_implied_novig") is not None for x in items])
    novig = np.array([float(x["market_implied_novig"]) if x.get("market_implied_novig") is not None else np.nan for x in items])

    out: dict[str, Any] = {
        "n": len(items),
        "mean_model_p": float(np.mean(p)),
        "mean_market_raw": float(np.mean(raw)),
        "mean_ev": float(np.mean(ev)),
        "pct_positive_ev": float(np.mean(ev > 0)),
        "breakeven_vs_model": {
            "mean_model_minus_breakeven": float(np.mean(p - breakeven)),
            "note": "Positive means model p above priced breakeven (still includes vig).",
        },
        "by_odds_range": _odds_range_calibration(items, settled=settled),
        "novig": {
            "n_identified": int(novig_mask.sum()),
            "share_identified": float(novig_mask.mean()) if len(items) else 0.0,
            "mean_model_minus_novig": float(np.nanmean(p[novig_mask] - novig[novig_mask])) if novig_mask.any() else None,
        },
        "research_only": True,
        "play_enabled": False,
    }

    if settled:
        y = np.array([1.0 if x["result"] == "win" else 0.0 for x in items], dtype=float)
        out["settlement"] = {
            "n": len(items),
            "hit_rate": float(np.mean(y)),
            "brier": brier_score(y, p),
            "log_loss": log_loss(y, p),
            "ece": expected_calibration_error(y, p),
            "brier_ci": bootstrap_ci(y, p, brier_score, n_boot=250, seed=3),
            "flat_stake_return_sum": float(np.nansum([x.get("flat_stake_return") for x in items if x.get("flat_stake_return") is not None])),
            "flat_stake_return_mean": float(np.nanmean([x.get("flat_stake_return") for x in items if x.get("flat_stake_return") is not None])) if any(x.get("flat_stake_return") is not None for x in items) else None,
            "clv_mean": float(np.nanmean([x.get("closing_line_value") for x in items if x.get("closing_line_value") is not None])) if any(x.get("closing_line_value") is not None for x in items) else None,
            "disclaimer": "Flat-stake and CLV are hypothetical diagnostics until a large settled sample exists.",
        }
        # Calibration vs market raw implied as a reference
        out["market_raw_as_model"] = {
            "brier": brier_score(y, raw),
            "log_loss": log_loss(y, raw),
        }
    else:
        out["settlement"] = {"n": 0, "note": "unsettled_open_or_mixed"}

    # By league
    by_lg: dict[str, int] = {}
    for x in items:
        by_lg[x["league_id"]] = by_lg.get(x["league_id"], 0) + 1
    out["by_league_n"] = by_lg
    return out


def _odds_range_calibration(items: list[dict[str, Any]], *, settled: bool) -> list[dict[str, Any]]:
    """Bucket by American odds / implied probability range."""
    buckets = [(0.0, 0.15), (0.15, 0.25), (0.25, 0.35), (0.35, 0.50), (0.50, 1.01)]
    out = []
    for lo, hi in buckets:
        subset = [x for x in items if lo <= float(x["market_implied_raw"]) < hi]
        if len(subset) < 5:
            continue
        p = np.array([float(x["forecast_probability"]) for x in subset])
        raw = np.array([float(x["market_implied_raw"]) for x in subset])
        row: dict[str, Any] = {
            "implied_lo": lo,
            "implied_hi": hi,
            "n": len(subset),
            "mean_model_p": float(np.mean(p)),
            "mean_market_raw": float(np.mean(raw)),
            "mean_ev": float(np.mean([float(x["expected_value"] or 0) for x in subset])),
        }
        if settled:
            y = np.array([1.0 if x["result"] == "win" else 0.0 for x in subset])
            row["mean_y"] = float(np.mean(y))
            row["brier"] = brier_score(y, p)
            row["gap_model"] = float(np.mean(p) - np.mean(y))
            row["gap_market"] = float(np.mean(raw) - np.mean(y))
        out.append(row)
    return out
