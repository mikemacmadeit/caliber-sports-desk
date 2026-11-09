"""Attach existing Anytime TD sportsbook ticks to research finder rows.

Read-only against odds already in the DB. Does not call the Odds API or spend credits.
Players/games without a quote stay explicitly unpriced — never invent prices.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from spe.config import settings
from spe.db.base import as_utc


def load_anytime_td_prices_for_games(
    session: Session,
    game_ids: list[str],
    *,
    now: datetime | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Return {(game_id, player_id): price payload} for latest anytime_td tick per player."""
    if not game_ids:
        return {}
    moment = as_utc(now) or datetime.now(UTC)
    from spe.db.models import Game
    from spe.market.price_age import max_fresh_minutes

    kickoffs = {
        row.game_id: row.kickoff_utc
        for row in session.query(Game.game_id, Game.kickoff_utc).filter(Game.game_id.in_(game_ids))
    }
    # One latest tick per (game, player) among existing outcomes.
    stmt = text(
        """
        WITH ranked AS (
          SELECT o.game_id, o.player_id, o.outcome_id,
                 t.price_american, t.captured_at, t.observed_at, t.source_at,
                 b.name AS bookmaker_name,
                 ROW_NUMBER() OVER (
                   PARTITION BY o.game_id, o.player_id
                   ORDER BY COALESCE(t.observed_at, t.captured_at) DESC
                 ) AS rn
          FROM outcomes o
          JOIN odds_ticks t ON t.outcome_id = o.outcome_id
          LEFT JOIN bookmakers b ON b.bookmaker_id = t.bookmaker_id
          WHERE o.market_id = 'anytime_td'
            AND o.player_id IS NOT NULL
            AND o.game_id IN :gids
        )
        SELECT game_id, player_id, outcome_id, price_american, captured_at,
               observed_at, source_at, bookmaker_name
        FROM ranked WHERE rn = 1
        """
    ).bindparams(bindparam("gids", expanding=True))
    rows = session.execute(stmt, {"gids": list(game_ids)}).mappings().all()

    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        gid = str(row["game_id"])
        pid = str(row["player_id"])
        captured = row["observed_at"] or row["captured_at"]
        age_min = None
        status = "missing"
        max_age = max_fresh_minutes(kickoffs.get(gid), moment)
        if captured is not None:
            cap = as_utc(captured) if not isinstance(captured, datetime) else captured
            if cap is not None:
                if cap.tzinfo is None:
                    cap = cap.replace(tzinfo=UTC)
                age_min = (moment - cap).total_seconds() / 60.0
                status = "fresh" if age_min <= max_age else "stale"
        out[(gid, pid)] = {
            "status": "priced",
            "book_price": int(row["price_american"]) if row["price_american"] is not None else None,
            "bookmaker": row["bookmaker_name"],
            "captured_at": str(captured) if captured is not None else None,
            "source_timestamp": str(row["source_at"]) if row["source_at"] is not None else None,
            "price_freshness": {
                "status": status,
                "age_minutes": age_min,
                "captured_at": str(captured) if captured is not None else None,
                "source_timestamp": str(row["source_at"]) if row["source_at"] is not None else None,
                "max_age_minutes": max_age,
                "is_current_opportunity": status == "fresh" and row["price_american"] is not None,
            },
        }
    return out


def attach_research_price(
    player: dict[str, Any],
    *,
    game_id: str,
    price_index: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Copy price block onto finder player; mark unpriced when no tick exists."""
    out = dict(player)
    pid = str(player.get("player_id") or "")
    hit = price_index.get((str(game_id), pid))
    if hit is None:
        out["sportsbook"] = {
            "status": "unpriced",
            "book_price": None,
            "bookmaker": None,
            "captured_at": None,
            "source_timestamp": None,
            "price_freshness": {
                "status": "missing",
                "age_minutes": None,
                "captured_at": None,
                "source_timestamp": None,
                "max_age_minutes": float(settings.max_price_age_minutes),
                "is_current_opportunity": False,
            },
            "note": "No anytime_td sportsbook quote in DB for this player/game",
        }
        ms = dict(out.get("model_source") or {})
        ms["price_captured_at"] = None
        out["model_source"] = ms
        return out

    out["sportsbook"] = hit
    ms = dict(out.get("model_source") or {})
    ms["price_captured_at"] = hit.get("captured_at")
    out["model_source"] = ms
    # Stale price warning without inventing a fresh quote.
    fresh = (hit.get("price_freshness") or {}).get("status")
    if fresh == "stale":
        warnings = list(out.get("research_warnings") or [])
        if not any(isinstance(w, dict) and w.get("code") == "stale_sportsbook_price" for w in warnings):
            warnings.append(
                {
                    "code": "stale_sportsbook_price",
                    "message": "Latest sportsbook tick is older than the research freshness window.",
                }
            )
        out["research_warnings"] = warnings
    return out
