from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

from spe.db.session import session_factory


def main() -> None:
    now = datetime.now(UTC)
    with session_factory() as s:
        print("all_ps", s.execute(text("SELECT COUNT(*) FROM outcomes WHERE market_id='point_spread'")).scalar())
        print(
            "all_ticks",
            s.execute(
                text(
                    """
                    SELECT COUNT(*) FROM odds_ticks t
                    JOIN outcomes o ON o.outcome_id=t.outcome_id
                    WHERE o.market_id='point_spread'
                    """
                )
            ).scalar(),
        )
        print(
            "raw_spreads",
            s.execute(
                text("SELECT COUNT(*) FROM raw_snapshots WHERE provider_record_id LIKE :pat"),
                {"pat": "%spreads%"},
            ).scalar(),
        )
        rows = s.execute(
            text(
                """
                SELECT provider_record_id, observed_at, credit_cost
                FROM raw_snapshots
                WHERE provider='odds_api'
                ORDER BY observed_at DESC
                LIMIT 20
                """
            )
        ).fetchall()
        for r in rows:
            print(r)
        print("now", now.isoformat())
        print(
            "upcoming_games",
            s.execute(text("SELECT COUNT(*) FROM games WHERE kickoff_utc > :n"), {"n": now}).scalar(),
        )
        print(
            "upcoming_ps_join",
            s.execute(
                text(
                    """
                    SELECT COUNT(DISTINCT o.game_id), COUNT(*)
                    FROM outcomes o
                    JOIN odds_ticks t ON t.outcome_id=o.outcome_id
                    JOIN games g ON g.game_id=o.game_id
                    WHERE o.market_id='point_spread' AND g.kickoff_utc > :n
                    """
                ),
                {"n": now},
            ).fetchone(),
        )
        latest_tick = s.execute(
            text(
                """
                SELECT g.game_id, g.kickoff_utc, t.captured_at, t.bookmaker_id, o.side, o.line
                FROM odds_ticks t
                JOIN outcomes o ON o.outcome_id=t.outcome_id
                JOIN games g ON g.game_id=o.game_id
                WHERE o.market_id='point_spread'
                ORDER BY t.captured_at DESC
                LIMIT 10
                """
            )
        ).fetchall()
        print("latest_ticks")
        for r in latest_tick:
            print(r)


if __name__ == "__main__":
    main()
