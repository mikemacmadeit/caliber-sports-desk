"""College roles from recent touch share and portal moves.

A transferred player with fewer than two games at the new school stays
``new_transfer``. Touch share is carries plus the stored target column
(receptions, for CFBD box scores). It is not a depth-chart label.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.db.models import Game, PlayerGameStat, Roster

ROLE_NEW_TRANSFER = "new_transfer"
ROLE_STARTER = "starter"
ROLE_COMMITTEE = "committee"
ROLE_RESERVE = "reserve"
ROLE_USAGE_UNKNOWN = "usage_unknown"

# Applied to prior-school workload until two games exist at the new school.
NEW_TRANSFER_WORKLOAD_FACTOR = 0.35
RESERVE_WORKLOAD_FACTOR = 0.60
STARTER_SHARE = 0.28
COMMITTEE_SHARE = 0.12
TRANSFER_GAMES_REQUIRED = 2


def workload_factor(role: str | None) -> float:
    if role == ROLE_NEW_TRANSFER:
        return NEW_TRANSFER_WORKLOAD_FACTOR
    if role == ROLE_RESERVE:
        return RESERVE_WORKLOAD_FACTOR
    return 1.0


def annotate_fbs_roles(session: Session, game: Game, players: list) -> list:
    """Return the same players with ``role`` set from recent usage."""
    if game.league_id != "fbs" or not players:
        return players
    kick = as_utc(game.kickoff_utc)
    if kick is None:
        return players
    current = {player.player_id: player.team_id for player in players}
    prior_club = _players_with_prior_club(session, current)
    touches, games_played = _recent_touches(session, current, kick)
    by_pos: dict[tuple[str, str], float] = {}
    for player in players:
        key = (player.team_id, player.position_group)
        by_pos[key] = by_pos.get(key, 0.0) + touches.get(player.player_id, 0.0)
    out = []
    for player in players:
        denom = by_pos.get((player.team_id, player.position_group), 0.0)
        share = (touches.get(player.player_id, 0.0) / denom) if denom > 0 else 0.0
        played = games_played.get(player.player_id, 0)
        role = _role(played, share, player.player_id in prior_club)
        out.append(replace(player, role=role))
    return out


def _role(games_played: int, share: float, had_prior_club: bool) -> str:
    if games_played < TRANSFER_GAMES_REQUIRED and had_prior_club:
        return ROLE_NEW_TRANSFER
    if games_played < TRANSFER_GAMES_REQUIRED:
        return ROLE_USAGE_UNKNOWN
    if share >= STARTER_SHARE:
        return ROLE_STARTER
    if share >= COMMITTEE_SHARE:
        return ROLE_COMMITTEE
    return ROLE_RESERVE


def _players_with_prior_club(session: Session, current: dict[str, str]) -> set[str]:
    if not current:
        return set()
    rows = (
        session.query(Roster.player_id, Roster.team_id)
        .filter(Roster.player_id.in_(list(current)), Roster.valid_to.isnot(None))
        .all()
    )
    return {pid for pid, team_id in rows if current.get(pid) and team_id != current[pid]}


def _recent_touches(
    session: Session,
    current: dict[str, str],
    kick: datetime,
) -> tuple[dict[str, float], dict[str, int]]:
    """Touches and game counts on the current club, last three finals before kickoff."""
    team_ids = set(current.values())
    if not team_ids:
        return {}, {}
    games = (
        session.query(Game)
        .filter(
            Game.league_id == "fbs",
            Game.status == "final",
            Game.kickoff_utc < kick,
        )
        .all()
    )
    recent_ids: set[str] = set()
    by_team: dict[str, list[Game]] = {team_id: [] for team_id in team_ids}
    for game in games:
        for team_id in (game.home_team_id, game.away_team_id):
            if team_id in by_team:
                by_team[team_id].append(game)
    for team_id, rows in by_team.items():
        rows.sort(key=lambda row: as_utc(row.kickoff_utc) or row.kickoff_utc, reverse=True)
        for game in rows[:3]:
            recent_ids.add(game.game_id)
    if not recent_ids:
        return {}, {}
    stats = (
        session.query(
            PlayerGameStat.player_id,
            PlayerGameStat.team_id,
            PlayerGameStat.game_id,
            PlayerGameStat.targets,
            PlayerGameStat.carries,
        )
        .filter(
            PlayerGameStat.player_id.in_(list(current)),
            PlayerGameStat.game_id.in_(recent_ids),
        )
        .all()
    )
    touches: dict[str, float] = {}
    played: dict[str, set[str]] = {}
    for player_id, team_id, game_id, targets, carries in stats:
        if current.get(player_id) != team_id:
            continue
        touches[player_id] = touches.get(player_id, 0.0) + float(targets or 0) + float(carries or 0)
        played.setdefault(player_id, set()).add(game_id)
    return touches, {player_id: len(games) for player_id, games in played.items()}
