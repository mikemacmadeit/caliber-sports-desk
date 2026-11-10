"""ESPN box scores for finished games. A player who is not in the box stays missing.

Missing is not a zero. A listed player with no rushing, receiving, or return
touchdown is a real zero for the anytime-scorer grade.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from spe.db.base import new_id
from spe.db.models import Game, Player, PlayerGameStat, ShadowPrediction
from spe.identity.match import match_name
from spe.ingest.espn_availability import _espn_event_id

USER_AGENT = "caliber-box/1.0"
ESPN_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/football/{league}/summary?event={event}"
RUSH_GROUPS = {"rushing"}
REC_GROUPS = {"receiving"}
RETURN_GROUPS = {"kickreturns", "puntreturns"}
PROVIDER = "espn.box"


def _td_index(labels: list[Any]) -> int | None:
    for index, label in enumerate(labels):
        if str(label).strip().upper() == "TD":
            return index
    return None


def _int_stat(values: list[Any], index: int | None) -> int:
    if index is None or index >= len(values):
        return 0
    raw = str(values[index]).strip()
    if not raw or raw in {"-", "—"}:
        return 0
    try:
        return int(float(raw))
    except ValueError:
        return 0


def parse_scoring_touchdowns(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Rushing, receiving, and return touchdowns for athletes listed in the box."""
    box = payload.get("boxscore") if isinstance(payload.get("boxscore"), dict) else {}
    found: dict[str, dict[str, Any]] = {}
    for team_block in box.get("players") or []:
        if not isinstance(team_block, dict):
            continue
        for group in team_block.get("statistics") or []:
            if not isinstance(group, dict):
                continue
            name = str(group.get("name") or "").replace(" ", "").lower()
            if name not in RUSH_GROUPS | REC_GROUPS | RETURN_GROUPS:
                continue
            index = _td_index(list(group.get("labels") or group.get("keys") or []))
            for athlete_row in group.get("athletes") or []:
                if not isinstance(athlete_row, dict):
                    continue
                athlete = athlete_row.get("athlete") if isinstance(athlete_row.get("athlete"), dict) else {}
                display = str(athlete.get("displayName") or "").strip()
                espn_id = str(athlete.get("id") or "").strip()
                if not display:
                    continue
                key = espn_id or display
                row = found.setdefault(
                    key,
                    {"espn_id": espn_id, "name": display, "rush_td": 0, "rec_td": 0, "return_td": 0},
                )
                scored = _int_stat(list(athlete_row.get("stats") or []), index)
                if name in RUSH_GROUPS:
                    row["rush_td"] += scored
                elif name in REC_GROUPS:
                    row["rec_td"] += scored
                else:
                    row["return_td"] += scored
    return list(found.values())


def _fetch_summary(league_id: str, event_id: str) -> dict[str, Any] | None:
    path = "nfl" if league_id == "nfl" else "college-football"
    url = ESPN_SUMMARY.format(league=path, event=event_id)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _catalog(session: Session, game_id: str) -> list[tuple[str, str]]:
    rows = (
        session.query(ShadowPrediction)
        .filter(ShadowPrediction.game_id == game_id, ShadowPrediction.market_id == "anytime_td")
        .all()
    )
    catalog: dict[str, str] = {}
    for row in rows:
        name = str((row.components or {}).get("player_name") or "").strip()
        if row.player_id and name:
            catalog[row.player_id] = name
    return list(catalog.items())


def ingest_final_box_scores(
    session: Session,
    *,
    league_id: str = "nfl",
    now: datetime | None = None,
    lookback_days: int = 8,
    game_id: str | None = None,
    fetch=_fetch_summary,
) -> dict[str, Any]:
    """Write listed touchdowns for recent finals. Players absent from the box are left blank."""
    moment = now or datetime.now(UTC)
    since = moment - timedelta(days=lookback_days)
    games = (
        session.query(Game)
        .filter(
            Game.league_id == league_id,
            Game.status == "final",
            Game.kickoff_utc >= since,
        )
        .all()
    )
    summary: dict[str, Any] = {
        "status": "ok",
        "league_id": league_id,
        "games": 0,
        "players_written": 0,
        "unmatched": 0,
        "no_event": 0,
    }
    for game in games:
        if game_id and game.game_id != game_id:
            continue
        event_id = _espn_event_id(session, game)
        if not event_id:
            summary["no_event"] += 1
            continue
        payload = fetch(game.league_id, event_id)
        if not isinstance(payload, dict):
            continue
        summary["games"] += 1
        catalog = _catalog(session, game.game_id)
        have = {
            player_id
            for (player_id,) in session.query(PlayerGameStat.player_id).filter(PlayerGameStat.game_id == game.game_id)
        }
        for athlete in parse_scoring_touchdowns(payload):
            decision = match_name(athlete["name"], catalog) if catalog else None
            player_id = None if decision is None or decision.review_status != "auto" else decision.canonical_id
            if player_id is None or session.get(Player, player_id) is None:
                summary["unmatched"] += 1
                continue
            if player_id in have:
                continue
            rush_td = int(athlete["rush_td"]) + int(athlete["return_td"])
            session.add(
                PlayerGameStat(
                    id=new_id(),
                    game_id=game.game_id,
                    player_id=player_id,
                    attempts=0,
                    routes=0,
                    targets=0,
                    carries=0,
                    receptions=0,
                    rush_yards=0.0,
                    rec_yards=0.0,
                    rush_td=rush_td,
                    rec_td=int(athlete["rec_td"]),
                    pass_td=0,
                    rz_targets=0,
                    rz_carries=0,
                    provider=PROVIDER,
                    provider_record_id=f"{game.game_id}:{player_id}"[:160],
                    observed_at=moment,
                    effective_at=game.kickoff_utc,
                    quality_status="espn_box_listed",
                )
            )
            have.add(player_id)
            summary["players_written"] += 1
    session.flush()
    return summary
