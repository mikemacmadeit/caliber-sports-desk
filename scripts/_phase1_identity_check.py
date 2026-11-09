"""Phase 1 identity / roster audit against live upcoming games."""

from __future__ import annotations

from datetime import UTC, datetime

from spe.db.base import as_utc
from spe.db.models import Game, Outcome
from spe.db.session import session_factory
from spe.identity.roster import eligible_players_for_game
from spe.integrity.assignments import audit_published_forecasts, quarantine_conflicts
from spe.integrity.forecast_audit import audit_and_invalidate_forecasts


def main() -> None:
    s = session_factory()
    try:
        now = datetime.now(UTC)
        games = (
            s.query(Game)
            .filter(
                Game.data_tier == "upcoming",
                Game.status.in_(("scheduled", "preview", "pregame")),
                Game.league_id.in_(("nfl", "fbs")),
            )
            .all()
        )
        nfl = [g for g in games if g.league_id == "nfl"]
        fbs = [g for g in games if g.league_id == "fbs"]
        print("upcoming_games", len(games), "nfl", len(nfl), "fbs", len(fbs))

        identity = audit_published_forecasts(s)
        print("identity_summary", {k: identity.get(k) for k in sorted(identity.keys()) if not isinstance(identity.get(k), (list, dict))})
        print("identity_list_sizes", {k: len(v) for k, v in identity.items() if isinstance(v, list)})
        flagged = quarantine_conflicts(s, identity)
        print("quarantined_now", flagged)
        s.commit()

        fa = audit_and_invalidate_forecasts(s)
        s.commit()
        print(
            "forecast_audit",
            {k: fa.get(k) for k in sorted(fa.keys()) if not isinstance(fa.get(k), (list, dict))},
        )
        print("forecast_audit_lists", {k: len(v) for k, v in fa.items() if isinstance(v, list)})

        bad_outcomes = 0
        checked = 0
        samples: list[tuple] = []
        empty_rosters = 0
        for g in sorted(games, key=lambda x: as_utc(x.kickoff_utc) or now):
            roster = {
                r.player_id for r in eligible_players_for_game(s, g, include_inactive_for_props=True)
            }
            if not roster:
                empty_rosters += 1
            outs = (
                s.query(Outcome)
                .filter(Outcome.game_id == g.game_id, Outcome.player_id.isnot(None))
                .all()
            )
            for o in outs:
                checked += 1
                if o.player_id not in roster:
                    bad_outcomes += 1
                    if len(samples) < 12:
                        samples.append((g.game_id, o.player_id, o.market_id))
        print("outcomes_checked", checked, "off_roster_outcomes", bad_outcomes)
        print("empty_rosters", empty_rosters)
        print("samples", samples)
    finally:
        s.close()


if __name__ == "__main__":
    main()
