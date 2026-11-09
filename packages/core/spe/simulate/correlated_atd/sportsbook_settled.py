"""Sportsbook performance only where real settlements exist.

Does not invent no-vig for yes-only markets. Does not claim profitability without evidence.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.models.metrics import brier_score, expected_calibration_error, log_loss
from spe.simulate.correlated_atd.proof_isolation import proof_dir
from spe.simulate.correlated_atd.scoring import bootstrap_ci


def sportsbook_performance(session: Session) -> dict[str, Any]:
    rows = session.execute(
        text(
            """
            SELECT s.shadow_id, s.league_id, s.model_version_id, s.forecast_probability,
                   s.price_american, s.market_implied_raw, s.market_implied_novig,
                   s.expected_value, s.forecast_as_of,
                   g.kickoff_utc,
                   st.result, st.flat_stake_return, st.closing_price_american, st.closing_line_value
            FROM shadow_predictions s
            JOIN games g ON g.game_id = s.game_id
            JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
            WHERE s.market_id = 'anytime_td'
              AND st.result IN ('win', 'loss')
              AND s.forecast_as_of < g.kickoff_utc
              AND (
                s.model_version_id LIKE 'm3-%'
                OR s.model_version_id LIKE 'm5-%'
                OR s.model_version_id LIKE 'm6-%'
                OR s.model_version_id LIKE 'atd_sim_v1-%'
              )
            """
        )
    ).mappings().all()

    report: dict[str, Any] = {
        "eligibility": "pre_kickoff_freeze AND settled win/loss only",
        "n_eligible_settled": len(rows),
        "disclaimer": (
            "No profitability claim without adequate settled sample and proper market identification. "
            "Yes-only anytime TD prices do not identify no-vig probabilities."
        ),
        "families": {},
        "measurable": False,
    }
    if not rows:
        report["status"] = "no_genuine_settled_prekickoff_rows"
        report["action"] = "Continue live shadow; re-run after finals settle."
        path = proof_dir() / "sportsbook_settled_performance.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        report["artifact_path"] = str(path)
        return report

    by_fam: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_fam.setdefault(_family(str(r["model_version_id"])), []).append(dict(r))

    report["measurable"] = True
    for fam, items in by_fam.items():
        y = np.array([1.0 if x["result"] == "win" else 0.0 for x in items])
        p = np.array([float(x["forecast_probability"]) for x in items])
        raw = np.array([float(x["market_implied_raw"]) for x in items])
        novig_n = sum(1 for x in items if x.get("market_implied_novig") is not None)
        flats = [x.get("flat_stake_return") for x in items if x.get("flat_stake_return") is not None]
        clvs = [x.get("closing_line_value") for x in items if x.get("closing_line_value") is not None]
        report["families"][fam] = {
            "n": len(items),
            "hit_rate": float(y.mean()),
            "brier": brier_score(y, p),
            "log_loss": log_loss(y, p),
            "ece": expected_calibration_error(y, p),
            "brier_ci": bootstrap_ci(y, p, brier_score, n_boot=300, seed=4),
            "mean_model_p": float(p.mean()),
            "mean_breakeven_raw_implied": float(raw.mean()),
            "mean_ev_at_capture": float(np.nanmean([x.get("expected_value") for x in items])),
            "flat_stake_return_mean": float(np.mean(flats)) if flats else None,
            "flat_stake_return_sum": float(np.sum(flats)) if flats else None,
            "clv_mean": float(np.mean(clvs)) if clvs else None,
            "novig_identified_n": novig_n,
            "novig_identified_share": float(novig_n / len(items)),
            "by_odds_range": _by_odds(items),
            "claim_profitable": False,
        }

    path = proof_dir() / "sportsbook_settled_performance.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["artifact_path"] = str(path)
    return report


def _family(mv: str) -> str:
    if mv.startswith("atd_sim_v1"):
        return "atd_sim_v1"
    if mv.startswith("m6-"):
        return "m6"
    if mv.startswith("m5-"):
        return "m5"
    if mv.startswith("m3-"):
        return "m3"
    return "other"


def _by_odds(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for lo, hi in ((0.0, 0.15), (0.15, 0.25), (0.25, 0.35), (0.35, 0.5), (0.5, 1.01)):
        subset = [x for x in items if lo <= float(x["market_implied_raw"]) < hi]
        if len(subset) < 8:
            continue
        y = np.array([1.0 if x["result"] == "win" else 0.0 for x in subset])
        p = np.array([float(x["forecast_probability"]) for x in subset])
        raw = np.array([float(x["market_implied_raw"]) for x in subset])
        out.append(
            {
                "implied_lo": lo,
                "implied_hi": hi,
                "n": len(subset),
                "mean_y": float(y.mean()),
                "mean_model_p": float(p.mean()),
                "mean_market_raw": float(raw.mean()),
                "brier": brier_score(y, p),
            }
        )
    return out
