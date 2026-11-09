"""Historical sportsbook odds.

The Odds API historical endpoint is paid-only. Player-prop history starts 2023-05-03.
This module never fabricates ticks.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from spe.config import settings
from spe.ingest.store import upsert_coverage

HISTORICAL_PROPS_START = "2023-05-03T05:30:00Z"
NOTE = (
    "GET /v4/historical/sports/{sport}/odds and event-level historical odds are paid plans. "
    "Player prop history begins 2023-05-03. Live ODDS_API_KEY without a historical plan cannot "
    "backfill prices. Do not invent no-vig or closing player-prop numbers."
)


def ingest_odds_history(session: Session) -> dict[str, Any]:
    has_key = bool(settings.odds_api_key)
    status = "paid_plan" if has_key else "needs_key"
    notes = NOTE
    if has_key:
        notes = (
            "ODDS_API_KEY is set, but historical snapshots are a paid add-on. "
            "No ticks written until a historical plan is confirmed. " + NOTE
        )
    for league in ("nfl", "fbs"):
        upsert_coverage(
            session,
            provider="odds_api",
            dataset="historical_player_props",
            league_id=league,
            season_year=0,
            row_count=0,
            status=status,
            notes=notes,
        )
        upsert_coverage(
            session,
            provider="odds_api",
            dataset="historical_game_odds",
            league_id=league,
            season_year=0,
            row_count=0,
            status=status,
            notes="Historical moneylines/spreads/totals also require the paid historical endpoint.",
        )
    session.flush()
    return {
        "status": status,
        "ticks_upserted": 0,
        "player_prop_history_starts": HISTORICAL_PROPS_START,
        "note": notes,
    }
