"""How old a book price can be and still count as fresh.

Inside two hours of kickoff the poll is a few minutes, and a price older than
that window is stale. Farther out, the wider daily ceiling still applies.
"""

from __future__ import annotations

from datetime import datetime

from spe.config import settings
from spe.db.base import as_utc


def max_fresh_minutes(kickoff: datetime | None, now: datetime) -> float:
    """Freshness ceiling in minutes for a game kicking off at ``kickoff``."""
    kick = as_utc(kickoff) if kickoff is not None else None
    moment = as_utc(now) or now
    if kick is None:
        return float(settings.max_price_age_minutes)
    hours = (kick - moment).total_seconds() / 3600.0
    if 0 <= hours <= 2:
        return float(settings.odds_near_fresh_minutes)
    return float(settings.max_price_age_minutes)
