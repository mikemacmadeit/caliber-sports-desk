"""Live CFBD roster, game box scores, and season PPA/usage. Does not parse play text."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from spe.adapters.base import AdapterError
from spe.adapters.cfbd import CfbdAdapter
from spe.config import settings
from spe.db.models import Game, Player, PlayerAnalytics, PlayerGameStat, Roster
from spe.identity.resolve import ensure_cfbd_player, resolve_team_id, team_name_map
from spe.ingest.canonical import fbs_game_id, fbs_player_id
from spe.ingest.cfbd_box import flatten_game_players
from spe.ingest.nws_live import refresh_board_tiers
from spe.ingest.store import finish_run, map_provider, start_run, upsert_coverage
from spe.ingest.values import as_float, as_str

PROVIDER = "cfbd"
PPA_SOURCE = "cfbd_ppa"
# Inclusive through the active college season. range(2016, 2026) previously dropped 2026.
DEFAULT_SEASONS = tuple(range(2016, 2027))


def _known_fbs_games(session: Session) -> dict[str, tuple[Any, ...]]:
    """Map CFBD-style game keys to DB rows.

    2026 slate games are stored as ``espn:fbs:{espn_id}`` while CFBD box scores
    return the same numeric id. Historical CFBD rows use ``fbs:{id}``. Index both
    so /games/players can attach stats without inventing rows.
    """
    known: dict[str, tuple[Any, ...]] = {}
    rows = (
        session.query(Game)
        .filter(Game.league_id == "fbs", Game.data_tier.in_(("historical", "upcoming", "season")))
        .all()
    )
    for row in rows:
        payload = (row.week or 0, row.season_id, row.home_team_id, row.away_team_id, row.game_id)
        known[row.game_id] = payload
        if row.game_id.startswith("espn:fbs:"):
            numeric = row.game_id.rsplit(":", 1)[-1]
            known[f"fbs:{numeric}"] = payload
            known[numeric] = payload
        elif row.game_id.startswith("fbs:"):
            numeric = row.game_id.split(":", 1)[-1]
            known[f"espn:fbs:{numeric}"] = payload
            known[numeric] = payload
    return known


def ingest_fbs_players(
    session: Session,
    *,
    seasons: tuple[int, ...] | list[int] = DEFAULT_SEASONS,
    adapter: CfbdAdapter | None = None,
    fixtures: dict[str, list[dict[str, Any]]] | None = None,
    observed_at: datetime | None = None,
    include_postseason: bool = True,
) -> dict[str, Any]:
    observed = observed_at or datetime.now(UTC)
    adapter = adapter or CfbdAdapter()
    totals = {
        "players_upserted": 0,
        "roster_rows": 0,
        "stats_upserted": 0,
        "ppa_season_rows": 0,
        "usage_rows": 0,
        "weeks_ingested": 0,
        "status": "ok",
        "missing": [
            "play_level_player_ids",
            "rz_targets_from_pbp",
            "rz_carries_from_pbp",
            "gl_carries_from_pbp",
            "targets_vs_receptions_cfbd_box_uses_rec_as_targets",
        ],
    }
    name_map = team_name_map(session)
    known_games = _known_fbs_games(session)

    for year in seasons:
        roster = _fetch(adapter, "roster", {"year": year}, fixtures, year)
        p_up, r_up = _retry_locked(
            session, lambda: ingest_roster(session, roster, year, observed_at=observed, name_map=name_map)
        ) or (0, 0)
        totals["players_upserted"] += p_up
        totals["roster_rows"] += r_up
        _commit(session)
        print(f"SEASON_DONE fbs roster {year} players={p_up} roster={r_up} calls={adapter.calls_made}", flush=True)

        usage = _fetch(adapter, "player_usage", {"year": year}, fixtures, year)
        ppa = _fetch(adapter, "ppa_players_season", {"year": year}, fixtures, year)
        totals["usage_rows"] += _retry_locked(
            session, lambda: ingest_usage_season(session, usage, year, observed_at=observed, name_map=name_map)
        )
        totals["ppa_season_rows"] += _retry_locked(
            session, lambda: ingest_ppa_season(session, ppa, year, observed_at=observed, name_map=name_map)
        )
        _commit(session)

        season_types = ["regular"]
        if include_postseason:
            season_types.append("postseason")
        for season_type in season_types:
            for week in range(1, 17 if season_type == "regular" else 5):
                if not fixtures and adapter.calls_made >= settings.cfbd_session_call_budget:
                    totals["status"] = "budget_stop"
                    upsert_coverage(
                        session,
                        provider=PROVIDER,
                        dataset="player_game_stats",
                        league_id="fbs",
                        season_year=year,
                        row_count=totals["stats_upserted"],
                        status="budget_stop",
                        notes=f"Stopped at {year} {season_type} week {week} after {adapter.calls_made} CFBD calls.",
                    )
                    session.commit()
                    totals["cfbd_calls_made"] = adapter.calls_made
                    return totals
                records = _fetch(
                    adapter,
                    "games_players",
                    {"year": year, "week": week, "seasonType": season_type, "classification": "fbs"},
                    fixtures,
                    year,
                )
                added = ingest_games_players(
                    session,
                    records,
                    year,
                    observed_at=observed,
                    name_map=name_map,
                    known_games=known_games,
                )
                totals["stats_upserted"] += added
                if added:
                    totals["weeks_ingested"] += 1
                _commit(session)
                if added:
                    print(
                        f"SEASON_DONE fbs box {year} {season_type} w{week} stats={added} total={totals['stats_upserted']} calls={adapter.calls_made}",
                        flush=True,
                    )
                if fixtures:
                    break
            if fixtures:
                break

    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="player_game_stats",
        league_id="fbs",
        season_year=0,
        row_count=totals["stats_upserted"],
        status="available" if totals["stats_upserted"] else "empty",
        notes=(
            "CFBD /games/players box scores. Targets approximated by receptions "
            "(CFBD does not publish targets). RZ/GL not filled from plays."
        ),
    )
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="roster",
        league_id="fbs",
        season_year=0,
        row_count=totals["players_upserted"],
        status="available",
        notes="Athlete IDs persist across transfers. Team resolved from school name.",
    )
    # Promote completed slate games so load_model_frame (data_tier=historical) sees new box scores.
    totals["tier_promotions"] = refresh_board_tiers(session, now=observed)
    session.flush()
    totals["cfbd_calls_made"] = adapter.calls_made
    totals["cfbd_session_budget"] = settings.cfbd_session_call_budget
    return totals


def ingest_roster(
    session: Session,
    records: list[dict[str, Any]],
    year: int,
    *,
    observed_at: datetime,
    name_map: dict[str, str] | None = None,
) -> tuple[int, int]:
    run = start_run(session, PROVIDER, "roster", "fbs", year)
    name_map = name_map or team_name_map(session)
    start = datetime(year, 8, 1, tzinfo=UTC)
    players_new = roster_new = 0
    existing_roster = {
        (row.team_id, row.player_id)
        for row in session.query(Roster.team_id, Roster.player_id)
    }
    for raw in records:
        ext = as_str(raw.get("id")) or as_str(raw.get("athleteId"))
        first = as_str(raw.get("firstName") or raw.get("first_name") or "")
        last = as_str(raw.get("lastName") or raw.get("last_name") or "")
        display = " ".join(p for p in [first, last] if p) or as_str(raw.get("name"))
        if not ext or not display:
            continue
        pos = as_str(raw.get("position")) or "UNK"
        existed = session.get(Player, fbs_player_id(ext)) is not None
        pid = ensure_cfbd_player(session, ext, display, pos)
        if not existed:
            players_new += 1
        team_id = resolve_team_id(session, raw, name_map)
        if team_id and (team_id, pid) not in existing_roster:
            session.add(
                Roster(
                    team_id=team_id,
                    player_id=pid,
                    valid_from=start,
                    status="active",
                    jersey=as_str(raw.get("jersey")),
                )
            )
            existing_roster.add((team_id, pid))
            roster_new += 1
        if players_new and players_new % 2000 == 0:
            session.flush()
    finish_run(session, run, status="ok", upserted=players_new, skipped=len(records) - players_new)
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="roster",
        league_id="fbs",
        season_year=year,
        row_count=players_new,
        status="available",
    )
    session.flush()
    return players_new, roster_new


def ingest_games_players(
    session: Session,
    records: list[dict[str, Any]],
    year: int,
    *,
    observed_at: datetime,
    name_map: dict[str, str],
    known_games: dict[str, tuple],
) -> int:
    for attempt in range(8):
        try:
            return _ingest_games_players_once(
                session, records, year, observed_at=observed_at, name_map=name_map, known_games=known_games
            )
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 7:
                raise
            session.rollback()
            session.info.pop("cfbd_players", None)
            time.sleep(2.0 * (attempt + 1))
    return 0


def _ingest_games_players_once(
    session: Session,
    records: list[dict[str, Any]],
    year: int,
    *,
    observed_at: datetime,
    name_map: dict[str, str],
    known_games: dict[str, tuple],
) -> int:
    run = start_run(session, PROVIDER, "player_game_stats", "fbs", year)
    rows: list[dict[str, Any]] = []
    seen = {
        (g, p)
        for g, p in session.query(PlayerGameStat.game_id, PlayerGameStat.player_id).filter(
            PlayerGameStat.provider == PROVIDER
        )
    }
    for raw in records:
        raw_id = str(raw.get("id") or "")
        lookup_keys = [fbs_game_id(raw_id), f"espn:fbs:{raw_id}", raw_id]
        meta = None
        for key in lookup_keys:
            meta = known_games.get(key)
            if meta is not None:
                break
        if meta is None:
            continue
        # Prefer the DB game_id (5th slot when present) so ESPN 2026 rows receive stats.
        gid = str(meta[4]) if len(meta) > 4 and meta[4] else fbs_game_id(raw_id)
        if raw_id:
            map_provider(session, PROVIDER, "game", raw_id, gid, confidence=0.99)
        for player in flatten_game_players(raw):
            pid = ensure_cfbd_player(session, player["athlete_id"], player["name"], None)
            if (gid, pid) in seen:
                continue
            team_id = name_map.get("".join(ch for ch in str(player["team"]).lower() if ch.isalnum()))
            home_id, away_id = meta[2], meta[3]
            if team_id not in {home_id, away_id}:
                team_id = None
            rows.append(
                {
                    "id": str(uuid4()),
                    "game_id": gid,
                    "player_id": pid,
                    "team_id": team_id,
                    "attempts": int(player["attempts"] or 0),
                    "routes": 0,
                    "targets": int(player["targets"] or 0),
                    "carries": int(player["carries"] or 0),
                    "receptions": int(player["receptions"] or 0),
                    "rush_yards": float(player["rush_yards"] or 0),
                    "rec_yards": float(player["rec_yards"] or 0),
                    "rush_td": int(player["rush_td"] or 0),
                    "rec_td": int(player["rec_td"] or 0),
                    "pass_td": int(player["pass_td"] or 0),
                    "rz_targets": 0,
                    "rz_carries": 0,
                    "gl_carries": 0,
                    "provider": PROVIDER,
                    "provider_record_id": f"{gid}:{pid}"[:160],
                    "observed_at": observed_at,
                    "effective_at": observed_at,
                    "ingested_at": observed_at,
                    "revision": 1,
                    "quality_status": "partial_no_charting",
                }
            )
            seen.add((gid, pid))
    session.flush()
    upserted = 0
    stmt = insert(PlayerGameStat)
    for i in range(0, len(rows), 1500):
        chunk = rows[i : i + 1500]
        session.execute(stmt, chunk)
        upserted += len(chunk)
        session.flush()
    finish_run(session, run, status="ok", upserted=upserted)
    session.flush()
    return upserted


def ingest_ppa_season(
    session: Session,
    records: list[dict[str, Any]],
    year: int,
    *,
    observed_at: datetime,
    name_map: dict[str, str],
) -> int:
    return _store_advanced_season(session, records, year, kind="ppa", name_map=name_map)


def ingest_usage_season(
    session: Session,
    records: list[dict[str, Any]],
    year: int,
    *,
    observed_at: datetime,
    name_map: dict[str, str],
) -> int:
    return _store_advanced_season(session, records, year, kind="usage", name_map=name_map)


def _store_advanced_season(
    session: Session,
    records: list[dict[str, Any]],
    year: int,
    *,
    kind: str,
    name_map: dict[str, str],
) -> int:
    if not records:
        upsert_coverage(
            session,
            provider=PROVIDER,
            dataset=f"player_{kind}_season",
            league_id="fbs",
            season_year=year,
            row_count=0,
            status="empty",
        )
        return 0
    pending: dict[str, PlayerAnalytics] = {}
    for row in session.query(PlayerAnalytics).filter_by(
        league_id="fbs",
        grain="advanced_season",
        split="all",
        season_year=year,
        week=0,
        game_id="",
        source=PPA_SOURCE,
    ):
        pending[row.player_id] = row
    n = 0
    for raw in records:
        ext = as_str(raw.get("id") or raw.get("playerId"))
        name = as_str(raw.get("name") or raw.get("player"))
        if not ext or not name:
            continue
        pid = ensure_cfbd_player(session, ext, name, as_str(raw.get("position")))
        team_id = resolve_team_id(session, raw, name_map)
        avg = raw.get("averagePPA") if isinstance(raw.get("averagePPA"), dict) else {}
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        row = pending.get(pid)
        metrics = dict(row.metrics) if row else {}
        if kind == "ppa":
            metrics.update(
                {
                    "avg_ppa_all": as_float(avg.get("all")),
                    "avg_ppa_pass": as_float(avg.get("pass")),
                    "avg_ppa_rush": as_float(avg.get("rush")),
                }
            )
        else:
            metrics.update(
                {
                    "usage_overall": as_float(usage.get("overall")),
                    "usage_pass": as_float(usage.get("pass")),
                    "usage_rush": as_float(usage.get("rush")),
                }
            )
        clean = {k: v for k, v in metrics.items() if v is not None}
        if row is None:
            row = PlayerAnalytics(
                league_id="fbs",
                player_id=pid,
                team_id=team_id,
                season_year=year,
                week=0,
                game_id="",
                grain="advanced_season",
                split="all",
                metrics=clean,
                coverage={"play_player_ids": False, "kind": kind},
                sample_size=1,
                source=PPA_SOURCE,
            )
            session.add(row)
            pending[pid] = row
            n += 1
        else:
            row.metrics = clean
            if team_id:
                row.team_id = team_id
            n += 1
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset=f"player_{kind}_season",
        league_id="fbs",
        season_year=year,
        row_count=n,
        status="available",
        notes="Season-grain CFBD advanced metrics. Not a pregame snapshot by itself.",
    )
    session.flush()
    return n


def _commit(session: Session) -> None:
    for attempt in range(8):
        try:
            session.commit()
            return
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 7:
                raise
            time.sleep(1.5 * (attempt + 1))


def _retry_locked(session: Session, fn):
    for attempt in range(8):
        try:
            return fn()
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 7:
                raise
            session.rollback()
            session.info.pop("cfbd_players", None)
            time.sleep(2.0 * (attempt + 1))
    return 0


def _fetch(
    adapter: CfbdAdapter,
    key: str,
    params: dict[str, Any],
    fixtures: dict[str, list[dict[str, Any]]] | None,
    season: int | None,
) -> list[dict[str, Any]]:
    fixture_key = f"{key}:{season}" if season is not None and fixtures and f"{key}:{season}" in fixtures else key
    if fixtures and fixture_key in fixtures:
        return adapter.fetch(key, params, fixture=fixtures[fixture_key]).records
    if fixtures:
        return []
    try:
        return adapter.fetch(key, params).records
    except AdapterError as exc:
        print(f"CFBD_FETCH_FAIL {key} {params} {exc}", flush=True)
        return []
