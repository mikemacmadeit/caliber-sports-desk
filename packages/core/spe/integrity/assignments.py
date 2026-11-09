"""Player-to-game checks for published forecasts.

A forecast is served only when the player is on one of the two teams at kickoff.
Name similarity is not enough. Odds ticks and forecast probabilities are not rewritten.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.db.models import (
    Forecast,
    Game,
    OddsTick,
    Outcome,
    Player,
    RawSnapshot,
    Roster,
    RosterWeek,
    Season,
    Team,
)
from spe.ingest.canonical import NFL_TEAM_NAMES
from spe.identity.match import normalize_name

IDENTITY_CONFLICT = "IDENTITY_CONFLICT"

# The same club is stored under both the nflverse abbreviation and the ESPN one.
_NFL_ID_ALIAS = {
    "nfl:LA": "nfl:LAR",
    "nfl:LAR": "nfl:LA",
    "nfl:WAS": "nfl:WSH",
    "nfl:WSH": "nfl:WAS",
    "nfl:JAC": "nfl:JAX",
    "nfl:JAX": "nfl:JAC",
}

# A moved club keeps its old abbreviation on the schedule and the current one
# on the plays. The seasons do not overlap, so both belong to the club playing now.
NFL_CONTINUITY = {
    "nfl:OAK": "nfl:LV",
    "nfl:SD": "nfl:LAC",
    "nfl:STL": "nfl:LA",
}


def linked_team_ids(team: Team | None) -> set[str]:
    """Scoreboard team plus the historical id stored on its ESPN alias."""
    if team is None:
        return set()
    found = {team.team_id}
    alias = _NFL_ID_ALIAS.get(team.team_id)
    if alias:
        found.add(alias)
    if team.league_id == "fbs":
        for alias in team.aliases or []:
            espn_id = alias.get("espn_id") if isinstance(alias, dict) else None
            if espn_id:
                found.add(f"fbs:{espn_id}")
    return found


def historical_fbs_id(team_id: str) -> bool:
    """True for CFBD ids such as ``fbs:61``, not short scoreboard ids such as ``uga``."""
    if not str(team_id).startswith("fbs:"):
        return False
    return str(team_id).split(":", 1)[1].isdigit()


def canonical_team_map(teams: list[Team]) -> dict[str, str]:
    """Map every stored id for an FBS club onto its historical ``fbs:{espn}`` id.

    A few 2026 scoreboard rows were saved under a short id (``uga``) while the
    earlier seasons live on ``fbs:61``. Ratings have to accumulate both.
    """
    known = {team.team_id for team in teams}
    canon: dict[str, str] = {}
    for team in teams:
        if team.league_id != "fbs":
            continue
        target = team.team_id
        for alias in team.aliases or []:
            espn_id = alias.get("espn_id") if isinstance(alias, dict) else None
            candidate = f"fbs:{espn_id}" if espn_id else ""
            if candidate and candidate in known:
                target = candidate
                break
        for link in linked_team_ids(team):
            previous = canon.get(link)
            if previous is None or (historical_fbs_id(target) and not historical_fbs_id(previous)):
                canon[link] = target
    return canon


def same_club(left_id: str, right_id: str, canon: dict[str, str]) -> bool:
    return canon.get(left_id, left_id) == canon.get(right_id, right_id)


def participating_ids(game: Game, teams: dict[str, Team]) -> set[str]:
    ids: set[str] = set()
    for team_id in (game.home_team_id, game.away_team_id):
        ids |= linked_team_ids(teams.get(team_id))
        ids.add(team_id)
    return ids


def current_roster_team(rows: list[Roster], kickoff: datetime) -> str | None:
    """Latest roster still open at kickoff. An older open row does not count."""
    kick = as_utc(kickoff)
    active: list[Roster] = []
    for row in rows:
        start = as_utc(row.valid_from)
        end = as_utc(row.valid_to)
        if start is None or kick is None or start > kick:
            continue
        if end is not None and end <= kick:
            continue
        active.append(row)
    if not active:
        return None
    active.sort(key=lambda row: as_utc(row.valid_from) or row.valid_from)
    return active[-1].team_id


def _name_key(description: str) -> str:
    text = (description or "").strip()
    lowered = text.lower()
    if lowered.endswith(" anytime td"):
        text = text[: -len(" anytime td")]
    return normalize_name(text)


def names_agree(player: Player | None, description: str) -> bool:
    if player is None or not (description or "").strip():
        return True
    wanted = _name_key(description)
    if not wanted:
        return True
    options = [player.name, *(alias for alias in (player.aliases or []) if isinstance(alias, str))]
    return any(normalize_name(option) == wanted for option in options if option)


def _event_names(team: Team | None) -> set[str]:
    if team is None:
        return set()
    names = {team.name, team.abbreviation}
    full = NFL_TEAM_NAMES.get(team.abbreviation)
    if full:
        names.add(full)
    if team.abbreviation in {"WAS", "WSH"}:
        names.add("Washington Commanders")
    for alias in team.aliases or []:
        if isinstance(alias, str):
            names.add(alias)
        elif isinstance(alias, dict):
            location = str(alias.get("location") or "")
            nick = str(alias.get("nick") or "")
            if location:
                names.add(location)
            if location and nick:
                names.add(f"{location} {nick}")
    return {"".join(ch for ch in name.lower() if ch.isalnum()) for name in names if name}


def event_matches_game(game: Game, teams: dict[str, Team], home: str | None, away: str | None) -> bool:
    if not home or not away:
        return True

    def norm(value: str) -> str:
        return "".join(ch for ch in value.lower() if ch.isalnum())

    def side_ok(label: str, team: Team | None) -> bool:
        wanted = norm(label)
        if not wanted:
            return True
        names = _event_names(team)
        if wanted in names:
            return True
        # Odds often adds "State" or the nickname ("Sam Houston State Bearkats"
        # vs stored "Sam Houston"). Accept when one normalized name contains the other.
        for name in names:
            if len(name) < 6:
                continue
            if name in wanted or wanted in name:
                return True
        return False

    return side_ok(home, teams.get(game.home_team_id)) and side_ok(away, teams.get(game.away_team_id))


def assignment_problems(
    *,
    game: Game,
    player: Player | None,
    description: str,
    participant_ids: set[str],
    roster_rows: list[Roster],
    roster_week_team_id: str | None,
    event_home: str | None = None,
    event_away: str | None = None,
    teams: dict[str, Team] | None = None,
) -> list[str]:
    """Reasons the player cannot be forecast on this game. Empty means it is safe."""
    problems: list[str] = []
    if player is None:
        problems.append("MISSING_PLAYER")
        return problems
    prefix = player.player_id.split(":", 1)[0]
    if prefix in {"nfl", "fbs"} and prefix != game.league_id:
        problems.append("LEAGUE_ID_MISMATCH")
    if not names_agree(player, description):
        problems.append("NAME_MISMATCH")
    week_team = roster_week_team_id
    current = current_roster_team(roster_rows, game.kickoff_utc)
    if week_team is not None:
        if week_team not in participant_ids:
            problems.append("ROSTER_WEEK_OTHER_TEAM")
    elif current is not None:
        if current not in participant_ids:
            problems.append("ROSTER_OTHER_TEAM")
    else:
        problems.append("NO_TEAM_MEMBERSHIP")
    if teams is not None and not event_matches_game(game, teams, event_home, event_away):
        problems.append("ODDS_EVENT_OTHER_GAME")
    return problems


def odds_events(session: Session) -> dict[str, tuple[str | None, str | None]]:
    return _event_index(session)


def _event_index(session: Session) -> dict[str, tuple[str | None, str | None]]:
    found: dict[str, tuple[str | None, str | None]] = {}
    rows = session.query(RawSnapshot).filter(RawSnapshot.provider == "odds_api").all()
    for row in rows:
        event = (row.payload or {}).get("event") or {}
        event_id = str(event.get("id") or row.provider_record_id)
        found[event_id] = (event.get("home_team"), event.get("away_team"))
    return found


def _tick_event_id(provider_record_id: str) -> str:
    return (provider_record_id or "").split(":", 1)[0]


def audit_published_forecasts(session: Session) -> dict[str, Any]:
    """Latest official NFL and FBS forecast on each outcome."""
    games = {
        game.game_id: game
        for game in session.query(Game).filter(Game.league_id.in_(("nfl", "fbs")), Game.data_tier != "demo").all()
    }
    seasons = {row.season_id: row for row in session.query(Season).all()}
    teams = {row.team_id: row for row in session.query(Team).filter(Team.league_id.in_(("nfl", "fbs"))).all()}
    forecasts = (
        session.query(Forecast)
        .join(Outcome, Outcome.outcome_id == Forecast.outcome_id)
        .join(Game, Game.game_id == Outcome.game_id)
        .filter(Forecast.is_official.is_(True), Game.league_id.in_(("nfl", "fbs")), Game.data_tier != "demo")
        .all()
    )
    latest: dict[str, Forecast] = {}
    for row in forecasts:
        current = latest.get(row.outcome_id)
        if current is None or (as_utc(row.as_of) or row.as_of) >= (as_utc(current.as_of) or current.as_of):
            latest[row.outcome_id] = row
    outcome_ids = list(latest)
    outcomes = {
        row.outcome_id: row
        for row in session.query(Outcome).filter(Outcome.outcome_id.in_(outcome_ids or [""])).all()
    }
    player_ids = {row.player_id for row in outcomes.values() if row.player_id}
    players = {
        row.player_id: row
        for row in session.query(Player).filter(Player.player_id.in_(player_ids or {""})).all()
    }
    rosters: dict[str, list[Roster]] = defaultdict(list)
    if player_ids:
        for row in session.query(Roster).filter(Roster.player_id.in_(player_ids)).all():
            rosters[row.player_id].append(row)
    weeks: dict[tuple[str, int, int], str] = {}
    if player_ids:
        for row in session.query(RosterWeek).filter(RosterWeek.player_id.in_(player_ids)).all():
            weeks[(row.player_id, row.season_year, row.week)] = row.team_id
    events = _event_index(session)
    tick_event: dict[str, str] = {}
    if outcome_ids:
        for outcome_id, record_id in (
            session.query(OddsTick.outcome_id, OddsTick.provider_record_id)
            .filter(OddsTick.outcome_id.in_(outcome_ids), OddsTick.provider == "odds_api")
            .all()
        ):
            tick_event.setdefault(outcome_id, _tick_event_id(record_id))

    conflicts: list[dict[str, Any]] = []
    by_reason: dict[str, int] = defaultdict(int)
    audited = 0
    events_checked = 0
    for outcome_id, forecast in latest.items():
        outcome = outcomes.get(outcome_id)
        if outcome is None:
            continue
        game = games.get(outcome.game_id)
        if game is None:
            continue
        audited += 1
        if not outcome.player_id:
            continue
        season = seasons.get(game.season_id)
        week_team = None
        if season is not None and game.week is not None:
            week_team = weeks.get((outcome.player_id, season.year, game.week))
        event_id = tick_event.get(outcome_id)
        event_home, event_away = events.get(event_id, (None, None)) if event_id else (None, None)
        if event_home or event_away:
            events_checked += 1
        problems = assignment_problems(
            game=game,
            player=players.get(outcome.player_id),
            description=outcome.description or "",
            participant_ids=participating_ids(game, teams),
            roster_rows=rosters.get(outcome.player_id, []),
            roster_week_team_id=week_team,
            event_home=event_home,
            event_away=event_away,
            teams=teams,
        )
        if not problems:
            continue
        for reason in problems:
            by_reason[reason] += 1
        player = players.get(outcome.player_id)
        conflicts.append(
            {
                "forecast_id": forecast.forecast_id,
                "outcome_id": outcome.outcome_id,
                "game_id": game.game_id,
                "league_id": game.league_id,
                "player_id": outcome.player_id,
                "player_name": player.name if player else None,
                "description": outcome.description,
                "market_id": outcome.market_id,
                "model_version_id": forecast.model_version_id,
                "problems": problems,
            }
        )
    return {
        "audited": audited,
        "conflicts": len(conflicts),
        "events_checked": events_checked,
        "by_reason": dict(by_reason),
        "rows": conflicts,
    }


def quarantine_conflicts(session: Session, report: dict[str, Any]) -> int:
    """Stop serving conflicting forecasts. Leave probability, timestamp, and odds ticks unchanged."""
    outcome_ids = {row["outcome_id"] for row in report["rows"]}
    if not outcome_ids:
        return 0
    flagged = 0
    rows = (
        session.query(Forecast)
        .filter(Forecast.outcome_id.in_(outcome_ids), Forecast.is_official.is_(True))
        .all()
    )
    reasons_for = {row["outcome_id"]: row["problems"] for row in report["rows"]}
    for forecast in rows:
        reasons = list(forecast.reason_codes or [])
        if IDENTITY_CONFLICT not in reasons:
            reasons.append(IDENTITY_CONFLICT)
        for problem in reasons_for.get(forecast.outcome_id, []):
            if problem not in reasons:
                reasons.append(problem)
        forecast.reason_codes = reasons
        forecast.is_official = False
        flagged += 1
    session.flush()
    return flagged
