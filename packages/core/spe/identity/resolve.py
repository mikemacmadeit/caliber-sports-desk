"""Stable CFBD athlete IDs across seasons/transfers. Name match only when IDs are missing."""

from __future__ import annotations

from sqlalchemy.orm import Session

from spe.db.models import Player, Team
from spe.identity.match import match_name
from spe.ingest.canonical import fbs_player_id, position_group
from spe.ingest.store import map_provider
from spe.ingest.values import as_str


def norm_team_name(value: object) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def team_name_map(session: Session, league_id: str = "fbs") -> dict[str, str]:
    out: dict[str, str] = {}
    for row in session.query(Team).filter(Team.league_id == league_id):
        out[norm_team_name(row.name)] = row.team_id
        out[norm_team_name(row.abbreviation)] = row.team_id
        for alias in row.aliases or []:
            out[norm_team_name(alias)] = row.team_id
    return out


def resolve_team_id(session: Session, raw: dict, name_map: dict[str, str] | None = None) -> str | None:
    from spe.ingest.canonical import fbs_team_id

    ext = as_str(raw.get("teamId") or raw.get("team_id") or raw.get("homeId"))
    if ext:
        tid = fbs_team_id(ext)
        if session.get(Team, tid) is not None:
            return tid
    name = as_str(raw.get("team") or raw.get("school") or raw.get("homeTeam"))
    if not name:
        return None
    mapping = name_map if name_map is not None else team_name_map(session)
    return mapping.get(norm_team_name(name))


def ensure_cfbd_player(
    session: Session,
    athlete_id: str,
    name: str,
    position: str | None,
    *,
    catalog: list[tuple[str, str]] | None = None,
) -> str:
    """Prefer fbs:{athlete_id}. Never silently merge two CFBD IDs."""
    pid = fbs_player_id(athlete_id)
    pos = (position or "UNK")[:16]
    display = (name or "")[:128]
    cache: dict[str, Player] = session.info.setdefault("cfbd_players", {})
    row = cache.get(pid) or session.get(Player, pid)
    if row is None:
        row = Player(
            player_id=pid,
            name=display or pid,
            position_group=position_group(pos),
            position=pos,
            aliases=[],
        )
        session.add(row)
        map_provider(session, "cfbd", "player", str(athlete_id), pid)
        cache[pid] = row
        return pid
    cache[pid] = row
    aliases = list(row.aliases or [])
    if display and display != row.name and display not in aliases:
        aliases.append(display)
        row.aliases = aliases
    if row.position_group == "UNK" and pos != "UNK":
        row.position = pos
        row.position_group = position_group(pos)
    return pid


def match_player_on_roster(
    query_name: str,
    roster_catalog: list[tuple[str, str]],
) -> str | None:
    decision = match_name(query_name, roster_catalog)
    if decision.review_status == "auto":
        return decision.canonical_id
    return None
