"""Programmatic 5 NFL + 5 FBS sample: identity + freshness (offline DB)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from spe.config import settings
from spe.db.models import Game, Outcome, Player, Team
from spe.db.session import session_factory
from spe.identity.roster import eligible_players_for_game
from spe.db.models import OddsTick
from spe.db.base import as_utc


def main() -> None:
    now = datetime.now(UTC)
    horizon = now + timedelta(hours=settings.odds_upcoming_hours)
    with session_factory() as s:
        samples: dict[str, list] = {"nfl": [], "fbs": []}
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
                home = s.get(Team, game.home_team_id)
                away = s.get(Team, game.away_team_id)
                eligible = {p.player_id for p in eligible_players_for_game(s, game, include_inactive_for_props=True)}
                outcomes = (
                    s.query(Outcome)
                    .filter(Outcome.game_id == game.game_id, Outcome.player_id.isnot(None))
                    .limit(40)
                    .all()
                )
                bad_links = []
                for o in outcomes:
                    if o.player_id not in eligible:
                        pl = s.get(Player, o.player_id)
                        bad_links.append(
                            {
                                "outcome_id": o.outcome_id,
                                "player_id": o.player_id,
                                "name": pl.name if pl else None,
                                "description": o.description,
                            }
                        )
                ticks = (
                    s.query(OddsTick)
                    .join(Outcome, Outcome.outcome_id == OddsTick.outcome_id)
                    .filter(Outcome.game_id == game.game_id)
                    .order_by(OddsTick.observed_at.desc())
                    .limit(1)
                    .all()
                )
                latest = ticks[0] if ticks else None
                obs = as_utc(latest.observed_at) if latest else None
                age = None if obs is None else (now - obs).total_seconds() / 60.0
                samples[league].append(
                    {
                        "game_id": game.game_id,
                        "matchup": f"{away.abbreviation if away else '?'}@{home.abbreviation if home else '?'}",
                        "kickoff_utc": as_utc(game.kickoff_utc),
                        "eligible_players": len(eligible),
                        "priced_linked_outcomes_sampled": len(outcomes),
                        "bad_links_in_sample": len(bad_links),
                        "bad_link_examples": bad_links[:3],
                        "latest_observed_age_minutes": age,
                        "fresh_enough": age is not None and age <= settings.max_price_age_minutes,
                    }
                )
        print(json.dumps(samples, indent=2, default=str))


if __name__ == "__main__":
    main()
