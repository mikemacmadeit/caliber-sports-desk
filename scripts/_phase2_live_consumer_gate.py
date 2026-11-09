"""Prove quarantined / off-roster outcomes are excluded from live prop_row consumers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.queries import prop_row
from spe.config import settings
from spe.db.models import Game, Outcome
from spe.db.session import session_factory
from spe.identity.roster import eligible_players_for_game


def main() -> None:
    now = datetime.now(UTC)
    horizon = now + timedelta(hours=settings.odds_upcoming_hours)
    results = []
    with session_factory() as s:
        for league in ("nfl", "fbs"):
            games = (
                s.query(Game)
                .filter(
                    Game.league_id == league,
                    Game.data_tier.notin_(["demo", "alias"]),
                    Game.kickoff_utc >= now - timedelta(hours=1),
                    Game.kickoff_utc <= horizon,
                    Game.status.in_(["scheduled", "preview", "pregame"]),
                )
                .order_by(Game.kickoff_utc.asc())
                .limit(5)
                .all()
            )
            for game in games:
                # Same shape as eligible_roster_index; skip week eligibility audit for speed.
                roster = {
                    row.player_id: row
                    for row in eligible_players_for_game(
                        s, game, include_inactive_for_props=True, apply_eligibility_audit=False
                    )
                }
                outcomes = (
                    s.query(Outcome)
                    .filter(Outcome.game_id == game.game_id, Outcome.player_id.isnot(None))
                    .limit(80)
                    .all()
                )
                off = [o for o in outcomes if o.player_id not in roster]
                leaked = [o.outcome_id for o in off if prop_row(s, o, roster_index=roster) is not None]
                on_roster_shown = sum(
                    1 for o in outcomes if o.player_id in roster and prop_row(s, o, roster_index=roster) is not None
                )
                results.append(
                    {
                        "league": league,
                        "game_id": game.game_id,
                        "off_roster_outcomes": len(off),
                        "off_roster_leaked_via_prop_row": len(leaked),
                        "on_roster_prop_rows": on_roster_shown,
                    }
                )
    print(json.dumps(results, indent=2))
    assert all(r["off_roster_leaked_via_prop_row"] == 0 for r in results)
    print("OK: no off-roster leaks via prop_row")


if __name__ == "__main__":
    main()
