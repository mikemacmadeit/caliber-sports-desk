"""ESPN gameday availability.

NFL: inside the inactive window, an explicit inactive list is the only report
that marks the rest of the roster available. A missing list stays unknown.

FBS: named statuses are stored. The game is never marked complete from ESPN,
because college lists are not an official full report.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from spe.config import settings
from spe.db.base import new_id
from spe.db.models import Game, InjuryEvent, Player, ProviderIdMap
from spe.identity.match import match_name
from spe.identity.roster import eligible_players_for_game
from spe.ingest.store import finish_run, start_run, upsert_coverage
from spe.ops.availability import INACTIVE_LIST_SOURCE

ESPN_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/football/{league}/summary?event={event}"
USER_AGENT = "caliber-availability/1.0"
FBS_SOURCE = "espn.fbs_injuries"


def parse_summary_availability(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull named statuses. ``inactive_list`` is true only for an explicit inactive array."""
    rows: list[dict[str, str]] = []
    inactive_list = False

    def add(item: dict[str, Any], *, inactive: bool) -> None:
        athlete = item.get("athlete") if isinstance(item.get("athlete"), dict) else {}
        name = str(athlete.get("displayName") or athlete.get("fullName") or item.get("displayName") or "").strip()
        status = str(item.get("status") or item.get("type") or "").strip()
        if isinstance(item.get("type"), dict):
            status = str(item["type"].get("description") or item["type"].get("name") or status).strip()
        if not name or not status:
            return
        rows.append({"name": name, "status": status, "inactive_list": "1" if inactive else "0"})

    injuries = payload.get("injuries")
    if isinstance(injuries, list):
        for block in injuries:
            if not isinstance(block, dict):
                continue
            nested = block.get("injuries")
            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict):
                        add(item, inactive=False)
            elif block.get("athlete") or block.get("displayName"):
                add(block, inactive=False)

    for key in ("inactives", "inactivePlayers", "inactive"):
        block = payload.get(key)
        if not isinstance(block, list):
            continue
        inactive_list = True
        for item in block:
            if isinstance(item, dict):
                add(item, inactive=True)
            elif isinstance(item, str) and item.strip():
                rows.append({"name": item.strip(), "status": "Inactive", "inactive_list": "1"})
    return {"rows": rows, "inactive_list": inactive_list}


def ingest_espn_availability(
    session: Session,
    *,
    now: datetime | None = None,
    horizon_hours: float | None = None,
    fixture_by_event: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Refresh NFL inactives near kickoff and named FBS injury statuses."""
    now = now or datetime.now(UTC)
    window = float(horizon_hours if horizon_hours is not None else settings.inactive_window_hours)
    summary: dict[str, Any] = {
        "status": "ok",
        "nfl_games": 0,
        "fbs_games": 0,
        "inactive_lists": 0,
        "injuries_upserted": 0,
        "unmatched": 0,
        "play_enabled": False,
    }
    horizon = now + timedelta(hours=window)
    games = (
        session.query(Game)
        .filter(
            Game.data_tier != "demo",
            Game.kickoff_utc >= now - timedelta(hours=1),
            Game.kickoff_utc <= horizon,
            Game.status.in_(["scheduled", "preview", "pregame"]),
            Game.league_id.in_(["nfl", "fbs"]),
        )
        .all()
    )
    session.commit()
    run = start_run(session, "espn", "gameday_availability", "nfl", now.year)
    for game in games:
        event_id = _espn_event_id(session, game)
        if not event_id and fixture_by_event is None:
            continue
        payload = None
        if fixture_by_event is not None:
            payload = fixture_by_event.get(event_id or game.game_id)
        else:
            payload = _fetch_summary(game.league_id, event_id)
        if not isinstance(payload, dict):
            continue
        parsed = parse_summary_availability(payload)
        if game.league_id == "nfl":
            summary["nfl_games"] += 1
            written = _write_nfl(session, game, parsed, now)
        else:
            summary["fbs_games"] += 1
            written = _write_fbs(session, game, parsed, now)
        summary["injuries_upserted"] += written["upserted"]
        summary["unmatched"] += written["unmatched"]
        summary["inactive_lists"] += written["lists"]
        session.commit()
    finish_run(
        session,
        run,
        status="ok",
        upserted=int(summary["injuries_upserted"]),
        skipped=int(summary["unmatched"]),
        notes="NFL inactive list completes coverage. FBS named rows never complete the game.",
    )
    upsert_coverage(
        session,
        provider="espn",
        dataset="gameday_availability",
        league_id="nfl",
        season_year=now.year,
        row_count=int(summary["injuries_upserted"]),
        status="available" if summary["nfl_games"] or summary["fbs_games"] else "empty",
        notes=(
            "NFL coverage completes only when an inactive list is explicit. "
            "FBS ESPN rows are named statuses only and do not mark unpublished players healthy."
        ),
    )
    session.flush()
    return summary


def _write_nfl(session: Session, game: Game, parsed: dict[str, Any], now: datetime) -> dict[str, int]:
    catalog = [(row.player_id, row.name) for row in eligible_players_for_game(session, game, include_inactive_for_props=True)]
    upserted = unmatched = 0
    lists = 1 if parsed["inactive_list"] else 0
    if parsed["inactive_list"]:
        _marker(session, game, now, source=INACTIVE_LIST_SOURCE)
        upserted += 1
    for row in parsed["rows"]:
        on_list = row["inactive_list"] == "1" or parsed["inactive_list"]
        player_id = _match(session, row["name"], catalog)
        if player_id is None:
            unmatched += 1
            continue
        if _add_event(
            session,
            game=game,
            player_id=player_id,
            designation=_designation(row["status"]),
            source=INACTIVE_LIST_SOURCE if on_list else "espn.nfl_injuries",
            now=now,
            coverage_complete=bool(on_list and parsed["inactive_list"]),
        ):
            upserted += 1
    return {"upserted": upserted, "unmatched": unmatched, "lists": lists}


def _write_fbs(session: Session, game: Game, parsed: dict[str, Any], now: datetime) -> dict[str, int]:
    """Named college statuses only. Never a complete-game report."""
    catalog = [(row.player_id, row.name) for row in eligible_players_for_game(session, game, include_inactive_for_props=True)]
    upserted = unmatched = 0
    for row in parsed["rows"]:
        player_id = _match(session, row["name"], catalog)
        if player_id is None:
            unmatched += 1
            continue
        if _add_event(
            session,
            game=game,
            player_id=player_id,
            designation=_designation(row["status"]),
            source=FBS_SOURCE,
            now=now,
            coverage_complete=False,
        ):
            upserted += 1
    return {"upserted": upserted, "unmatched": unmatched, "lists": 0}


def _designation(status: str) -> str:
    text = status.strip()
    if text.lower() in {"inactive", "deactivated"}:
        return "Inactive"
    return text[:32] or "unknown"


def _match(session: Session, name: str, catalog: list[tuple[str, str]]) -> str | None:
    decision = match_name(name, catalog) if catalog else None
    if decision is None or decision.review_status != "auto" or not decision.canonical_id:
        return None
    if session.get(Player, decision.canonical_id) is None:
        return None
    return decision.canonical_id


def _add_event(
    session: Session,
    *,
    game: Game,
    player_id: str,
    designation: str,
    source: str,
    now: datetime,
    coverage_complete: bool,
) -> bool:
    rec_id = f"{source}:{game.game_id}:{player_id}:{designation}"[:160]
    existing = (
        session.query(InjuryEvent)
        .filter(InjuryEvent.provider == "espn", InjuryEvent.provider_record_id == rec_id)
        .first()
    )
    if existing is not None:
        existing.reported_at = now
        existing.observed_at = now
        existing.coverage_complete = coverage_complete
        return False
    session.add(
        InjuryEvent(
            injury_event_id=new_id(),
            game_id=game.game_id,
            player_id=player_id,
            designation=designation[:32],
            practice_status=None,
            reported_at=now,
            source=source,
            confidence=0.8 if coverage_complete else 0.55,
            coverage_complete=coverage_complete,
            provider="espn",
            provider_record_id=rec_id,
            observed_at=now,
            effective_at=now,
            quality_status="ok",
        )
    )
    return True


def _marker(session: Session, game: Game, now: datetime, *, source: str) -> None:
    anchor = _coverage_player(session)
    rec_id = f"{source}:coverage:{game.game_id}"[:160]
    if (
        session.query(InjuryEvent)
        .filter(InjuryEvent.provider == "espn", InjuryEvent.provider_record_id == rec_id)
        .first()
    ):
        return
    session.add(
        InjuryEvent(
            injury_event_id=new_id(),
            game_id=game.game_id,
            player_id=anchor,
            designation="report_clear",
            practice_status=None,
            reported_at=now,
            source=source,
            confidence=0.8,
            coverage_complete=True,
            provider="espn",
            provider_record_id=rec_id,
            observed_at=now,
            effective_at=now,
            quality_status="ok",
        )
    )


def _coverage_player(session: Session) -> str:
    pid = "nfl:availability-coverage"
    if session.get(Player, pid) is None:
        session.add(
            Player(
                player_id=pid,
                name="Availability coverage",
                position_group="UNK",
                position="UNK",
                aliases=[],
            )
        )
        session.flush()
    return pid


def _espn_event_id(session: Session, game: Game) -> str | None:
    for prefix in ("espn:nfl:", "espn:fbs:"):
        if game.game_id.startswith(prefix):
            return game.game_id.split(":")[-1]
    row = (
        session.query(ProviderIdMap)
        .filter_by(provider="espn", entity_type="game", canonical_id=game.game_id)
        .first()
    )
    if row is None:
        return None
    return str(row.external_id)


def _fetch_summary(league_id: str, event_id: str | None) -> dict[str, Any] | None:
    if not event_id:
        return None
    path = "nfl" if league_id == "nfl" else "college-football"
    url = ESPN_SUMMARY.format(league=path, event=event_id)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None

