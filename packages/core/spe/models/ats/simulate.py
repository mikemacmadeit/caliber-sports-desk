"""Calibrated game-margin simulation for ATS — separate from ATD correlated sims.

Uses verified expected scores from the statistical model. Does not reuse
contaminated historical-roster ATD simulation paths.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from spe.models.ats.labels import grade_home_spread
from spe.models.ats.model import AtsModelArtifact, cover_metrics


def simulate_margins(
    predicted_margin: float,
    sigma: float,
    *,
    home_spread_line: float | None = None,
    draw_count: int = 5000,
    seed: int = 42,
) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    draws = rng.normal(loc=float(predicted_margin), scale=max(float(sigma), 1.0), size=int(draw_count))
    out: dict[str, Any] = {
        "draw_count": int(draw_count),
        "mean_margin": float(np.mean(draws)),
        "std_margin": float(np.std(draws)),
        "p_home_win": float(np.mean(draws > 0)),
        "quantiles": {
            "p05": float(np.quantile(draws, 0.05)),
            "p25": float(np.quantile(draws, 0.25)),
            "p50": float(np.quantile(draws, 0.50)),
            "p75": float(np.quantile(draws, 0.75)),
            "p95": float(np.quantile(draws, 0.95)),
        },
    }
    if home_spread_line is not None and not (isinstance(home_spread_line, float) and np.isnan(home_spread_line)):
        results = [grade_home_spread(float(m), float(home_spread_line)) for m in draws]
        n = len(results)
        out["p_home_cover"] = float(sum(1 for r in results if r == "cover") / n)
        out["p_away_cover"] = float(sum(1 for r in results if r == "loss") / n)
        out["p_push"] = float(sum(1 for r in results if r == "push") / n)
        out["spread_line"] = float(home_spread_line)
    return out


def compare_simulation_to_model(scored: pd.DataFrame, artifact: AtsModelArtifact) -> dict[str, Any]:
    """Check whether MC cover probs improve Brier vs closed-form normal probs.

    Only runs on rows with historical spreads and known margins.
    """
    labeled = scored.dropna(subset=["margin", "spread_line"]).head(300).copy()
    if labeled.empty:
        return {
            "status": "skipped",
            "reason": "No labeled rows with spreads for simulation comparison.",
            "improves_brier": False,
        }

    y = []
    p_model = []
    p_sim = []
    for i, row in enumerate(labeled.itertuples(index=False)):
        # grade / simulate against sportsbook handicap h (not raw nflverse L).
        handicap = getattr(row, "home_handicap", None)
        if handicap is None or (isinstance(handicap, float) and np.isnan(handicap)):
            handicap = -float(row.spread_line)
        edge = float(row.margin) + float(handicap)
        if abs(edge) < 1e-9:
            continue
        y.append(1.0 if edge > 0 else 0.0)
        p_model.append(float(row.p_home_cover))
        sim = simulate_margins(
            float(row.predicted_margin),
            float(getattr(row, "margin_sigma", artifact.sigma)),
            home_spread_line=float(handicap),
            draw_count=2000,
            seed=1000 + i,
        )
        p_sim.append(float(sim["p_home_cover"]))

    if len(y) < 30:
        return {"status": "skipped", "reason": "Too few graded rows", "improves_brier": False, "n": len(y)}

    model_m = cover_metrics(np.asarray(y), np.asarray(p_model))
    sim_m = cover_metrics(np.asarray(y), np.asarray(p_sim))
    improves = (
        model_m["brier"] is not None
        and sim_m["brier"] is not None
        and sim_m["brier"] < model_m["brier"] - 0.001
    )
    return {
        "status": "ok",
        "n": len(y),
        "model": model_m,
        "simulation": sim_m,
        "improves_brier": bool(improves),
        "decision": (
            "include_simulation"
            if improves
            else "keep_closed_form_normal — simulation did not measurably improve cover Brier"
        ),
        "note": "ATS margin sim is independent of Anytime TD correlated simulation paths.",
    }
