"""Find why NFL spreads ingest spent credits but left no NFL ticks."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from spe.adapters.odds_api import OddsApiAdapter
from spe.db.session import init_db, session_factory
from spe.ingest.odds_budget import remaining_soft_budget, used_credits
from spe.ingest.odds_live import _match_events
from spe.config import settings


def main() -> None:
    init_db()
    now = datetime.now(UTC)
    with session_factory() as s:
        print("used", used_credits(s), "remaining", remaining_soft_budget(s))
        print("db", settings.database_url)

        nfl_any = s.execute(
            text(
                """
                SELECT COUNT(*), COUNT(DISTINCT o.game_id), MAX(t.captured_at)
                FROM odds_ticks t
                JOIN outcomes o ON o.outcome_id = t.outcome_id
                JOIN games g ON g.game_id = o.game_id
                WHERE o.market_id = 'point_spread' AND g.league_id = 'nfl'
                """
            )
        ).fetchone()
        print("all-time nfl point_spread ticks/games/max", nfl_any)

        # snapshots from last 30 min
        snaps = s.execute(
            text(
                """
                SELECT provider_record_id, observed_at, endpoint
                FROM raw_snapshots
                WHERE observed_at > :since
                ORDER BY observed_at DESC
                LIMIT 30
                """
            ),
            {"since": (now - timedelta(minutes=30)).isoformat()},
        ).fetchall()
        print("snapshots last 30m", len(snaps))
        for row in snaps[:15]:
            print(" ", row)

        adapter = OddsApiAdapter()
        events = adapter.fetch_events("nfl").records
        print("provider nfl events", len(events))
        matched = _match_events(s, "nfl", events, now, now + timedelta(hours=settings.odds_upcoming_hours))
        print("matched nfl games", len(matched))
        for game, event in matched[:8]:
            print(
                " ",
                game.game_id,
                game.league_id,
                game.kickoff_utc,
                event.get("id"),
                event.get("home_team"),
                event.get("away_team"),
            )

        # Any outcomes for those game ids
        if matched:
            ids = [g.game_id for g, _ in matched[:5]]
            q = s.execute(
                text(
                    """
                    SELECT o.game_id, o.side, o.line, COUNT(t.tick_id)
                    FROM outcomes o
                    LEFT JOIN odds_ticks t ON t.outcome_id = o.outcome_id
                    WHERE o.market_id = 'point_spread' AND o.game_id IN :ids
                    GROUP BY o.game_id, o.side, o.line
                    """
                ).bindparams(),
                {},
            )
            # sqlite IN tuple
            rows = s.execute(
                text(
                    f"""
                    SELECT o.game_id, o.side, o.line, COUNT(t.tick_id) AS n
                    FROM outcomes o
                    LEFT JOIN odds_ticks t ON t.outcome_id = o.outcome_id
                    WHERE o.market_id = 'point_spread'
                      AND o.game_id IN ({",".join(repr(i) for i in ids)})
                    GROUP BY o.game_id, o.side, o.line
                    """
                )
            ).fetchall()
            print("outcomes for sample matched games", rows)


if __name__ == "__main__":
    main()
