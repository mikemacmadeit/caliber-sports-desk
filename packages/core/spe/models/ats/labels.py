"""ATS labels: scoring margin and cover outcomes. Leakage-safe consumers must filter by kickoff.

Grading helpers below take a sportsbook-style home handicap ``h``
(negative = home favorite). For nflverse consensus ``spread_line`` L
(expected home margin; positive = home favored), convert with
``home_handicap_from_nflverse_L(L) == -L`` before calling these helpers.
See ``spe.models.ats.spread_contract``.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd


CoverResult = Literal["cover", "loss", "push"]


def home_margin(home_score: float, away_score: float) -> float:
    return float(home_score) - float(away_score)


def grade_home_spread(margin: float, home_spread_line: float) -> CoverResult:
    """Grade the home side vs a sportsbook home handicap h (negative = home favorite).

    home covers when margin + h > 0.
    Equivalent to margin - L > 0 when L is the nflverse expected-home-margin.
    """
    edge = float(margin) + float(home_spread_line)
    if abs(edge) < 1e-9:
        return "push"
    return "cover" if edge > 0 else "loss"


def grade_side(margin: float, home_spread_line: float, side: str) -> CoverResult:
    home_result = grade_home_spread(margin, home_spread_line)
    if side == "home":
        return home_result
    if side == "away":
        if home_result == "push":
            return "push"
        return "loss" if home_result == "cover" else "cover"
    raise ValueError(f"side must be home|away, got {side}")


def cover_indicator(margin: float, home_spread_line: float, side: str) -> float | None:
    """1 = cover, 0 = loss, None = push (excluded from Brier)."""
    result = grade_side(margin, home_spread_line, side)
    if result == "push":
        return None
    return 1.0 if result == "cover" else 0.0


def attach_margins(games: pd.DataFrame) -> pd.DataFrame:
    out = games.copy()
    out["home_score"] = pd.to_numeric(out["home_score"], errors="coerce")
    out["away_score"] = pd.to_numeric(out["away_score"], errors="coerce")
    out["margin"] = out["home_score"] - out["away_score"]
    out["total_points"] = out["home_score"] + out["away_score"]
    return out


def normal_cover_probs(
    predicted_margin: float,
    home_spread_line: float,
    sigma: float,
) -> dict[str, float]:
    """P(home covers), P(away covers), P(push) under N(predicted_margin, sigma^2).

    For non-integer lines, push probability is ~0 under a continuous margin model.
    For integer lines, treat exact push as a thin band around 0 after applying the line.
    """
    from math import erf, sqrt

    sigma = max(float(sigma), 1.0)
    # Home covers when margin + line > 0 ⇒ margin > -line
    threshold = -float(home_spread_line)
    mu = float(predicted_margin)
    z = (threshold - mu) / (sigma * sqrt(2.0))
    # P(margin > threshold) = 1 - Phi((threshold-mu)/sigma)
    p_home_strict = 0.5 * (1.0 - erf(z))
    line_is_whole = abs(home_spread_line - round(home_spread_line)) < 1e-9
    if line_is_whole:
        # Approximate push mass in [-0.5, 0.5] around the push point in cover-space.
        # Cover-space edge = margin + line; push when edge == 0.
        half = 0.5 / (sigma * sqrt(2.0))
        z0 = (0.0 - (mu + home_spread_line)) / (sigma * sqrt(2.0))
        p_push = 0.5 * (erf(z0 + half) - erf(z0 - half))
        p_push = float(np.clip(p_push, 0.0, 0.15))
        remainder = max(0.0, 1.0 - p_push)
        # Renormalize strict probs over non-push outcomes.
        p_home = float(np.clip(p_home_strict, 1e-4, 1 - 1e-4))
        p_away_strict = 1.0 - p_home
        total = p_home + p_away_strict
        p_home = remainder * (p_home / total)
        p_away = remainder * (p_away_strict / total)
    else:
        p_push = 0.0
        p_home = float(np.clip(p_home_strict, 1e-4, 1 - 1e-4))
        p_away = 1.0 - p_home
    return {
        "p_home_cover": float(p_home),
        "p_away_cover": float(p_away),
        "p_push": float(p_push),
    }
