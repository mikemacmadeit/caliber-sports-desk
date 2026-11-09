"""API-verify /v1/props for 5 NFL + 5 FBS upcoming games."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

from spe.config import settings
from spe.db.base import as_utc
from spe.db.models import Game, Team
from spe.db.session import session_factory


def fetch_props(league: str) -> list[dict]:
    url = f"http://127.0.0.1:8000/v1/props?league={urllib.parse.quote(league)}"
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.loads(r.read().decode())
    return list(data.get("rows") or [])


def main() -> None:
    now = datetime.now(UTC)
    horizon = now + timedelta(hours=settings.odds_upcoming_hours)
    out: dict[str, list] = {"nfl": [], "fbs": []}
    by_league_rows = {lg: fetch_props(lg) for lg in ("nfl", "fbs")}
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
            rows = by_league_rows[league]
            for g in games:
                home = s.get(Team, g.home_team_id)
                away = s.get(Team, g.away_team_id)
                game_rows = [r for r in rows if r.get("game_id") == g.game_id]
                stale = fresh = opp = 0
                for row in game_rows:
                    pf = row.get("price_freshness") or (row.get("forecast") or {}).get("price_freshness") or {}
                    if pf.get("is_current_opportunity"):
                        opp += 1
                    if pf.get("status") == "fresh":
                        fresh += 1
                    elif pf.get("status") == "stale":
                        stale += 1
                out[league].append(
                    {
                        "game_id": g.game_id,
                        "matchup": f"{away.abbreviation if away else '?'}@{home.abbreviation if home else '?'}",
                        "kickoff": str(as_utc(g.kickoff_utc)),
                        "prop_rows": len(game_rows),
                        "fresh": fresh,
                        "stale": stale,
                        "current_opportunities": opp,
                        "verification_sample": (game_rows[0].get("price_freshness") if game_rows else None),
                    }
                )
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
