"""Current NFL and FBS schedules from the public ESPN scoreboard.

These rows use data_tier=upcoming so the day board can show them. They are
schedule facts (kickoff, clubs, venue), not model prices.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from spe.db.base import as_utc
from spe.db.models import Game, Team
from spe.ingest.canonical import nfl_team_id
from spe.integrity.assignments import canonical_team_map, same_club
from spe.ingest.nws_live import board_tier, refresh_board_tiers
from spe.ingest.store import ensure_football_leagues, ensure_season, ensure_venue, map_provider

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/{league}/scoreboard"
USER_AGENT = "caliber-scoreboard/1.0"

_ABBR = {"LAR": "LA", "WSH": "WAS", "JAC": "JAX"}


def _board_abbr(abbr: str) -> str:
    return _ABBR.get(abbr, abbr)


_STATUS = {
    "STATUS_SCHEDULED": "scheduled",
    "STATUS_IN_PROGRESS": "in_progress",
    "STATUS_HALFTIME": "in_progress",
    "STATUS_END_PERIOD": "in_progress",
    "STATUS_FINAL": "final",
    "STATUS_FULL_TIME": "final",
    "STATUS_POSTPONED": "postponed",
    "STATUS_DELAYED": "delayed",
    "STATUS_CANCELED": "canceled",
    "STATUS_CANCELLED": "canceled",
    "STATUS_SUSPENDED": "suspended",
}


def fetch_scoreboard_week(
    league_path: str,
    year: int,
    season_type: int,
    week: int,
    *,
    groups: str | None = None,
    timeout: float = 30,
) -> list[dict[str, Any]]:
    query = f"dates={int(year)}&seasontype={int(season_type)}&week={int(week)}&limit=400"
    if groups:
        query += f"&groups={groups}"
    url = ESPN_SCOREBOARD.format(league=league_path) + "?" + query
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {400, 404}:
            return []
        raise
    events = payload.get("events") or []
    return [event for event in events if isinstance(event, dict)]


def ingest_football_scoreboard(session: Session, year: int = 2026) -> dict[str, int]:
    """Pull regular season and postseason for one season year."""
    ensure_football_leagues(session)
    plans = (
        ("nfl", "nfl", None, (2, 18), (3, 5)),
        ("college-football", "fbs", "80", (2, 20), (3, 6)),
    )
    upserted = 0
    skipped = 0
    for path, league_id, groups, regular, post in plans:
        for season_type, max_week in (regular, post):
            empty = 0
            for week in range(1, max_week + 1):
                events = fetch_scoreboard_week(path, year, season_type, week, groups=groups)
                if not events:
                    empty += 1
                    if empty >= 2:
                        break
                    continue
                empty = 0
                added, skipped_week = upsert_scoreboard_events(session, league_id, events)
                upserted += added
                skipped += skipped_week
                time.sleep(0.12)
    # Finals must graduate to historical so player_game_stats enter feature frames.
    tier_promotions = refresh_board_tiers(session)
    return {
        "upserted": upserted,
        "skipped": skipped,
        "year": int(year),
        "tier_promotions": tier_promotions,
    }


def upsert_scoreboard_events(session: Session, league_id: str, events: list[dict[str, Any]]) -> tuple[int, int]:
    taken = _matchup_index(session)
    upserted = 0
    skipped = 0
    now = datetime.now(UTC)
    for event in events:
        row = _parse_event(league_id, event)
        if row is None:
            skipped += 1
            continue
        game_id = row["game_id"]
        existing = session.get(Game, game_id)
        key = (league_id, _board_abbr(row["away_abbr"]), _board_abbr(row["home_abbr"]), row["day"])
        canonical_id = taken.get(key)
        if canonical_id and canonical_id != game_id:
            canonical = session.get(Game, canonical_id)
            if canonical is not None:
                _copy_result(canonical, row)
                map_provider(session, "espn", "game", row["espn_id"], canonical.game_id)
                upserted += 1
            if existing is None:
                continue
        season = ensure_season(session, league_id, row["year"])
        away = _ensure_team(session, league_id, row["away"])
        home = _ensure_team(session, league_id, row["home"])
        venue = ensure_venue(
            session,
            row["venue_id"],
            row["venue_name"],
            roof=row["roof"],
            surface=row["surface"],
        )
        tier = board_tier(row["status"], row["kickoff"], now)
        if existing is None:
            created = Game(
                game_id=game_id,
                season_id=season.season_id,
                league_id=league_id,
                home_team_id=home.team_id,
                away_team_id=away.team_id,
                kickoff_utc=row["kickoff"],
                venue_id=venue.venue_id,
                status=row["status"],
                week=row["week"],
                neutral_site=row["neutral"],
                data_tier=tier,
            )
            _apply_scores(created, row)
            session.add(created)
            upserted += 1
        else:
            existing.kickoff_utc = row["kickoff"]
            existing.status = row["status"]
            existing.week = row["week"]
            existing.neutral_site = row["neutral"]
            existing.venue_id = venue.venue_id
            if not _same_club(session, existing.home_team_id, home):
                existing.home_team_id = home.team_id
            if not _same_club(session, existing.away_team_id, away):
                existing.away_team_id = away.team_id
            _apply_scores(existing, row)
            # Never demote completed games back to upcoming (previous bug blocked 2026 features).
            if existing.data_tier not in {"demo", "alias"}:
                existing.data_tier = tier
            upserted += 1
        taken[key] = canonical_id if canonical_id and canonical_id != game_id else game_id
        map_provider(session, "espn", "game", row["espn_id"], game_id)
    session.flush()
    return upserted, skipped


def _matchup_index(session: Session) -> dict[tuple[str, str, str, str], str]:
    games = session.scalars(select(Game).options(joinedload(Game.home_team), joinedload(Game.away_team))).unique()
    index: dict[tuple[str, str, str, str], str] = {}
    for game in games:
        if game.home_team is None or game.away_team is None:
            continue
        day = _day(game.kickoff_utc)
        if not day:
            continue
        index[(game.league_id, game.away_team.abbreviation.upper(), game.home_team.abbreviation.upper(), day)] = game.game_id
    return index


def _day(kickoff: datetime) -> str:
    from zoneinfo import ZoneInfo

    aware = as_utc(kickoff)
    if aware is None:
        return ""
    return aware.astimezone(ZoneInfo("America/Chicago")).date().isoformat()


def _parse_event(league_id: str, event: dict[str, Any]) -> dict[str, Any] | None:
    competitions = event.get("competitions") or []
    if not competitions:
        return None
    competition = competitions[0]
    competitors = competition.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None
    home_team = _team_bits(home.get("team") or {})
    away_team = _team_bits(away.get("team") or {})
    if home_team is None or away_team is None:
        return None
    kickoff = _kickoff(event.get("date"))
    if kickoff is None:
        return None
    espn_id = str(event.get("id") or "").strip()
    if not espn_id:
        return None
    season = event.get("season") or {}
    week = event.get("week") or {}
    venue = competition.get("venue") or {}
    venue_name = str(venue.get("fullName") or venue.get("displayName") or "TBD")
    venue_key = str(venue.get("id") or venue_name)
    status_name = str(((competition.get("status") or {}).get("type") or {}).get("name") or "STATUS_SCHEDULED")
    year = int(season.get("year") or kickoff.year)
    status = _STATUS.get(status_name, "scheduled")
    return {
        "game_id": f"espn:{league_id}:{espn_id}",
        "espn_id": espn_id,
        "year": year,
        "week": int(week.get("number")) if week.get("number") is not None else None,
        "kickoff": kickoff,
        "day": _day(kickoff),
        "status": status,
        "home_score": _played_score(home.get("score"), status),
        "away_score": _played_score(away.get("score"), status),
        "neutral": bool(competition.get("neutralSite")),
        "home": home_team,
        "away": away_team,
        "home_abbr": home_team["abbr"],
        "away_abbr": away_team["abbr"],
        "venue_id": f"espn-venue:{league_id}:{venue_key}"[:64],
        "venue_name": venue_name[:128],
        "roof": "dome" if venue.get("indoor") else "open",
        "surface": str(venue.get("grass") and "grass" or "unknown"),
    }


def _copy_result(game: Game, row: dict[str, Any]) -> None:
    """A finished score belongs on the game the board already has."""
    incoming = str(row.get("status") or "")
    if game.status != "final" and incoming in {"final", "in_progress", "postponed", "canceled", "delayed", "suspended"}:
        game.status = incoming
    _apply_scores(game, row)


def _apply_scores(game: Game, row: dict[str, Any]) -> None:
    """Write the score ESPN already has. A scheduled 0–0 is not a result."""
    if row.get("home_score") is None or row.get("away_score") is None:
        return
    game.home_score = int(row["home_score"])
    game.away_score = int(row["away_score"])


def _played_score(raw: Any, status: str) -> int | None:
    if status not in {"final", "in_progress"}:
        return None
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _team_bits(team: dict[str, Any]) -> dict[str, str] | None:
    abbr = str(team.get("abbreviation") or "").strip().upper()
    espn_id = str(team.get("id") or "").strip()
    if not abbr or not espn_id:
        return None
    if abbr == "JAC":
        abbr = "JAX"
    return {
        "abbr": abbr[:16],
        "espn_id": espn_id,
        "name": str(team.get("displayName") or team.get("name") or abbr)[:128],
        "location": str(team.get("location") or team.get("shortDisplayName") or abbr)[:64],
        "nick": str(team.get("name") or abbr)[:64],
        "primary": _hex(team.get("color"), "#2C3848"),
        "secondary": _hex(team.get("alternateColor"), "#F4F6FB"),
    }


def pick_scoreboard_team(matches: list[Team], league_id: str, espn_id: str) -> Team | None:
    """Prefer the historical ``fbs:{espn}`` row over a short abbreviation id."""
    if not matches:
        return None
    if league_id == "fbs":
        wanted = f"fbs:{espn_id}"
        for team in matches:
            if team.team_id == wanted:
                return team
        historical = [team for team in matches if team.team_id.startswith("fbs:") and team.team_id.split(":", 1)[1].isdigit()]
        if historical:
            return sorted(historical, key=lambda team: team.team_id)[0]
    return sorted(matches, key=lambda team: (":" in team.team_id, len(team.team_id), team.team_id))[0]


def _same_club(session: Session, stored_id: str, chosen: Team) -> bool:
    """Keep a game on its current id when that id is the same club.

    Rewriting ``uga`` to ``fbs:61`` on a game whose plays are still keyed
    ``uga`` would drop those plays out of the rating join.
    """
    if stored_id == chosen.team_id:
        return True
    stored = session.get(Team, stored_id)
    if stored is None:
        return False
    canon = canonical_team_map([stored, chosen])
    return same_club(stored_id, chosen.team_id, canon)


def _ensure_team(session: Session, league_id: str, bits: dict[str, str]) -> Team:
    matches = (
        session.query(Team)
        .filter(Team.league_id == league_id, Team.abbreviation == bits["abbr"])
        .all()
    )
    row = pick_scoreboard_team(matches, league_id, bits["espn_id"])
    if row is None:
        team_id = nfl_team_id(bits["abbr"]) if league_id == "nfl" else f"fbs:{bits['espn_id']}"
        row = session.get(Team, team_id)
    if row is None:
        row = Team(
            team_id=nfl_team_id(bits["abbr"]) if league_id == "nfl" else f"fbs:{bits['espn_id']}",
            league_id=league_id,
            name=bits["name"],
            abbreviation=bits["abbr"],
            conference=None,
            aliases=[],
        )
        session.add(row)
        session.flush()
    mark = {
        "espn_id": bits["espn_id"],
        "location": bits["location"],
        "nick": bits["nick"],
        "primary": bits["primary"],
        "secondary": bits["secondary"],
    }
    aliases = [item for item in (row.aliases or []) if not (isinstance(item, dict) and item.get("espn_id"))]
    aliases.append(mark)
    row.aliases = aliases
    if not row.name or row.name == row.abbreviation:
        row.name = bits["name"]
    map_provider(session, "espn", "team", bits["espn_id"], row.team_id)
    return row


def _kickoff(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _hex(value: object, fallback: str) -> str:
    raw = str(value or "").strip().lstrip("#")
    if len(raw) == 6 and all(c in "0123456789abcdefABCDEF" for c in raw):
        return f"#{raw.upper()}"
    return fallback
