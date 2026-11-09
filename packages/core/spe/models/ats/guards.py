"""Stops a broken team identity from being published as a score.

The margin model turns games-played into points. Two ids for one club
(``uga`` with 8 games, ``fbs:61`` with 120) look like a blowout. That gap is
not a football edge, so the projection stays off the board.
"""

from __future__ import annotations

import math

# A full FBS season is about 12 games. Two seasons of missing history is
# enough to mint a fake margin and too wide for an ordinary schedule.
HISTORY_GAP_LIMIT = 24


def withhold_for_history_gap(home_games: float, away_games: float, *, limit: float = HISTORY_GAP_LIMIT) -> bool:
    if not math.isfinite(home_games) or not math.isfinite(away_games):
        return True
    return abs(home_games - away_games) >= limit
