"""Debug why NFL live spreads are not reaching ATS predict."""
from __future__ import annotations

from sqlalchemy import text

from spe.db.session import init_db, session_factory
from spe.models.ats.predict import _live_spread_lines


def main() -> None:
    init_db()
    with session_factory() as s:
        print("odds_ticks cols ok")
        snaps = s.execute(
            text(
                """
                SELECT provider_record_id, observed_at
                FROM raw_snapshots
                WHERE provider_record_id LIKE :pat
                ORDER BY observed_at DESC
                LIMIT 8
                """
            ),
            {"pat": "%:spreads"},
        ).fetchall()
        print("recent spreads snapshots", len(snaps))
        for row in snaps[:5]:
            print(" ", row)

        ticks = s.execute(
            text(
                """
                SELECT COUNT(*), MAX(captured_at)
                FROM odds_ticks
                WHERE captured_at > datetime('now', '-2 hours')
                """
            )
        ).fetchone()
        print("ticks last 2h", ticks)

        by_lg = s.execute(
            text(
                """
                SELECT g.league_id, COUNT(*), COUNT(DISTINCT g.game_id)
                FROM odds_ticks t
                JOIN outcomes o ON o.outcome_id = t.outcome_id
                JOIN games g ON g.game_id = o.game_id
                WHERE t.captured_at > datetime('now', '-2 hours')
                  AND o.market_id = 'point_spread'
                GROUP BY g.league_id
                """
            )
        ).fetchall()
        print("point_spread by league last 2h", by_lg)

        sample = s.execute(
            text(
                """
                SELECT g.game_id, g.league_id, o.side, o.line, t.bookmaker_id,
                       t.price_american, t.captured_at
                FROM odds_ticks t
                JOIN outcomes o ON o.outcome_id = t.outcome_id
                JOIN games g ON g.game_id = o.game_id
                WHERE o.market_id = 'point_spread'
                  AND t.captured_at > datetime('now', '-2 hours')
                ORDER BY t.captured_at DESC
                LIMIT 15
                """
            )
        ).fetchall()
        print("sample ticks:")
        for row in sample:
            print(" ", row)

        live = _live_spread_lines(s, "nfl")
        print("live_spread_lines nfl rows", 0 if live is None else len(live))
        if live is not None and not live.empty:
            print(live.head(10).to_string())


if __name__ == "__main__":
    main()
