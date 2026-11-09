"""CFBD transfer portal. Moves an existing athlete to the destination school.

Name-only rows that do not match a rostered player are left unmatched.
A portal entry with no destination does not close the current roster.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from spe.adapters.cfbd import CfbdAdapter
from spe.db.base import as_utc
from spe.db.models import Player, Roster
from spe.identity.match import match_name
from spe.identity.resolve import ensure_cfbd_player, norm_team_name, team_name_map
from spe.ingest.canonical import fbs_player_id
from spe.ingest.store import finish_run, start_run, upsert_coverage
from spe.ingest.values import as_str

PROVIDER = "cfbd"


def ingest_cfbd_portal(
    session: Session,
    *,
    year: int,
    adapter: CfbdAdapter | None = None,
    fixture: list[dict[str, Any]] | None = None,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    observed = observed_at or datetime.now(UTC)
    adapter = adapter or CfbdAdapter()
    summary: dict[str, Any] = {
        "status": "ok",
        "year": year,
        "rows": 0,
        "moved": 0,
        "already_there": 0,
        "unmatched": 0,
        "no_destination": 0,
    }
    if not adapter.api_key and fixture is None:
        summary["status"] = "needs_key"
        upsert_coverage(
            session,
            provider=PROVIDER,
            dataset="transfer_portal",
            league_id="fbs",
            season_year=year,
            row_count=0,
            status="needs_key",
            notes="Set CFBD_API_KEY to ingest the transfer portal.",
        )
        return summary

    run = start_run(session, PROVIDER, "transfer_portal", "fbs", year)
    fetched = adapter.fetch("player_portal", {"year": year}, fixture=fixture)
    names = team_name_map(session)
    for raw in fetched.records:
        if not isinstance(raw, dict):
            continue
        summary["rows"] += 1
        destination = _school_id(raw.get("destination"), names)
        if destination is None:
            summary["no_destination"] += 1
            continue
        origin = _school_id(raw.get("origin"), names)
        player_id = _player_id(session, raw, origin)
        if player_id is None:
            summary["unmatched"] += 1
            continue
        moved = _move_roster(
            session,
            player_id=player_id,
            destination=destination,
            when=_transfer_at(raw, year),
        )
        summary["moved" if moved else "already_there"] += 1

    finish_run(session, run, status="ok", upserted=int(summary["moved"]), skipped=int(summary["unmatched"]))
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="transfer_portal",
        league_id="fbs",
        season_year=year,
        row_count=int(summary["moved"]),
        status="available" if summary["rows"] else "empty",
        notes=(
            "Athlete stays on the same id. Origin roster closes at the transfer date. "
            "Role stays new_transfer until two games of usage at the new school. "
            f"unmatched={summary['unmatched']} no_destination={summary['no_destination']}."
        ),
    )
    session.flush()
    return summary


def _school_id(name: Any, names: dict[str, str]) -> str | None:
    text = as_str(name)
    if not text or text.lower() in {"none", "null", "n/a", "undecided"}:
        return None
    return names.get(norm_team_name(text))


def _player_id(session: Session, raw: dict[str, Any], origin: str | None) -> str | None:
    athlete = as_str(raw.get("athleteId") or raw.get("athlete_id") or raw.get("playerId") or raw.get("id"))
    if athlete and athlete.isdigit():
        pid = fbs_player_id(athlete)
        if session.get(Player, pid) is None:
            first = as_str(raw.get("firstName") or raw.get("first_name"))
            last = as_str(raw.get("lastName") or raw.get("last_name"))
            display = " ".join(part for part in (first, last) if part) or as_str(raw.get("name")) or pid
            ensure_cfbd_player(session, athlete, display, as_str(raw.get("position")))
        return pid
    if origin is None:
        return None
    first = as_str(raw.get("firstName") or raw.get("first_name"))
    last = as_str(raw.get("lastName") or raw.get("last_name"))
    display = " ".join(part for part in (first, last) if part)
    if not display:
        return None
    catalog = [
        (player.player_id, player.name)
        for _roster, player in (
            session.query(Roster, Player)
            .join(Player, Player.player_id == Roster.player_id)
            .filter(Roster.team_id == origin, Roster.valid_to.is_(None))
            .all()
        )
    ]
    decision = match_name(display, catalog) if catalog else None
    if decision is None or decision.review_status != "auto" or not decision.canonical_id:
        return None
    return decision.canonical_id


def _transfer_at(raw: dict[str, Any], year: int) -> datetime:
    text = as_str(raw.get("transferDate") or raw.get("transfer_date"))
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except ValueError:
            pass
    return datetime(year, 8, 1, tzinfo=UTC)


def _move_roster(session: Session, *, player_id: str, destination: str, when: datetime) -> bool:
    open_rows = (
        session.query(Roster)
        .filter(Roster.player_id == player_id, Roster.valid_to.is_(None))
        .all()
    )
    already = False
    for row in open_rows:
        if row.team_id == destination:
            already = True
            continue
        start = as_utc(row.valid_from)
        if start is not None and start >= when:
            continue
        row.valid_to = when
    if already:
        return False
    session.add(
        Roster(
            team_id=destination,
            player_id=player_id,
            valid_from=when,
            status="active",
        )
    )
    return True
