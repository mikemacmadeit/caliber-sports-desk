"""CollegeFootballData historical ingest for NCAA FBS.

Requires CFBD_API_KEY. Does not fabricate missing seasons, PPA, or player stats.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from spe.adapters.base import AdapterError
from spe.adapters.cfbd import CfbdAdapter
from spe.config import settings
from spe.db.models import Game, Player, PlayerGameStat, Play, Roster, Team
from spe.identity.resolve import ensure_cfbd_player
from spe.integrity.assignments import historical_fbs_id
from spe.ingest.canonical import (
    fbs_game_id,
    fbs_player_id,
    fbs_season_id,
    fbs_team_id,
    fbs_venue_id,
    parse_kickoff,
    position_group,
)
from spe.ingest.store import (
    ensure_football_leagues,
    ensure_season,
    ensure_venue,
    finish_run,
    map_provider,
    start_run,
    upsert_coverage,
)
from spe.ingest.values import as_float, as_int, as_str

PROVIDER = "cfbd"
# Inclusive through the active college season (matches cfbd_players).
DEFAULT_SEASONS = tuple(range(2016, 2027))


def ingest_fbs_history(
    session: Session,
    *,
    seasons: tuple[int, ...] | list[int] = DEFAULT_SEASONS,
    include_plays: bool = False,
    adapter: CfbdAdapter | None = None,
    fixtures: dict[str, list[dict[str, Any]]] | None = None,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    observed = observed_at or datetime.now(UTC)
    adapter = adapter or CfbdAdapter()
    ensure_football_leagues(session)
    if not fixtures and not settings.cfbd_api_key and not adapter.api_key:
        for year in seasons:
            upsert_coverage(
                session,
                provider=PROVIDER,
                dataset="games",
                league_id="fbs",
                season_year=year,
                row_count=0,
                status="needs_key",
                notes="Set CFBD_API_KEY. Tier 2 (~$5/mo) is the lean FBS historical feed.",
            )
        session.flush()
        return {"status": "needs_key", "games_upserted": 0, "seasons": list(seasons)}

    totals = {"games_upserted": 0, "teams_upserted": 0, "players_upserted": 0, "stats_upserted": 0, "plays_upserted": 0}
    teams = _records(adapter, "teams", {}, fixtures)
    totals["teams_upserted"] = ingest_teams(session, teams, observed_at=observed)

    for year in seasons:
        ensure_season(session, "fbs", year)
        games = _records(adapter, "games", {"year": year, "seasonType": "regular"}, fixtures, season=year)
        g_up, _ = ingest_games(session, games, year, observed_at=observed)
        totals["games_upserted"] += g_up
        session.commit()
        print(f"SEASON_DONE fbs games {year} games={g_up} total={totals['games_upserted']}", flush=True)
        if fixtures:
            roster = _records(adapter, "roster", {"year": year}, fixtures, season=year)
            totals["players_upserted"] += ingest_roster(session, roster, year, observed_at=observed)
            ppa = _records(adapter, "ppa_players", {"year": year, "seasonType": "regular"}, fixtures, season=year)
            totals["stats_upserted"] += ingest_ppa_games(session, ppa, year, observed_at=observed)
        else:
            upsert_coverage(
                session,
                provider=PROVIDER,
                dataset="roster",
                league_id="fbs",
                season_year=year,
                row_count=0,
                status="deferred",
                notes="Roster/PPA deferred on live free-key runs so games+plays can be measured first.",
            )

    if include_plays:
        if fixtures:
            for year in seasons:
                plays = _records(adapter, "plays", {"year": year, "seasonType": "regular"}, fixtures, season=year)
                totals["plays_upserted"] += ingest_plays(session, plays, year, observed_at=observed)
        else:
            stopped = False
            for year in sorted(seasons, reverse=True):
                if stopped:
                    break
                for week in range(1, 17):
                    if adapter.calls_made >= settings.cfbd_session_call_budget:
                        upsert_coverage(
                            session,
                            provider=PROVIDER,
                            dataset="plays",
                            league_id="fbs",
                            season_year=year,
                            row_count=totals["plays_upserted"],
                            status="budget_stop",
                            notes=f"Stopped at {year} week {week} after {adapter.calls_made} measured CFBD calls (free-key budget).",
                        )
                        stopped = True
                        break
                    plays = _records(
                        adapter,
                        "plays",
                        {"year": year, "week": week, "seasonType": "regular"},
                        None,
                        season=year,
                    )
                    added = ingest_plays(session, plays, year, observed_at=observed)
                    totals["plays_upserted"] += added
                    session.commit()
                    if added:
                        print(
                            f"SEASON_DONE fbs plays {year} week {week} plays={added} total={totals['plays_upserted']} calls={adapter.calls_made}",
                            flush=True,
                        )
    else:
        for year in seasons:
            upsert_coverage(
                session,
                provider=PROVIDER,
                dataset="plays",
                league_id="fbs",
                season_year=year,
                row_count=0,
                status="skipped",
                notes="Pass --plays to ingest week-by-week PBP. Free-key usage is counted; paid Tier 2 is not assumed.",
            )
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="charting_routes_rz",
        league_id="fbs",
        season_year=0,
        row_count=0,
        status="requires_contract",
        notes="CFBD standard plays do not include licensed route/coverage charting.",
    )
    session.flush()
    totals["status"] = "ok"
    totals["seasons"] = list(seasons)
    totals["cfbd_calls_made"] = adapter.calls_made
    totals["cfbd_session_budget"] = settings.cfbd_session_call_budget
    return totals


def ingest_teams(session: Session, records: list[dict[str, Any]], *, observed_at: datetime) -> int:
    run = start_run(session, PROVIDER, "teams", "fbs", 0)
    upserted = 0
    for raw in records:
        classification = (as_str(raw.get("classification")) or "").lower()
        if classification != "fbs":
            continue
        ext = as_str(raw.get("id")) or as_str(raw.get("school"))
        name = as_str(raw.get("school")) or as_str(raw.get("displayName"))
        if not ext or not name:
            continue
        tid = fbs_team_id(ext)
        if session.get(Team, tid) is None:
            abbr = (as_str(raw.get("abbreviation")) or name[:8])[:16]
            session.add(
                Team(
                    team_id=tid,
                    league_id="fbs",
                    name=name[:128],
                    abbreviation=abbr,
                    conference=as_str(raw.get("conference")),
                    aliases=[],
                )
            )
            map_provider(session, PROVIDER, "team", ext, tid)
            upserted += 1
    finish_run(session, run, status="ok", upserted=upserted)
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="teams",
        league_id="fbs",
        season_year=0,
        row_count=upserted,
        status="available",
    )
    session.flush()
    return upserted


def ingest_games(session: Session, records: list[dict[str, Any]], year: int, *, observed_at: datetime) -> tuple[int, int]:
    run = start_run(session, PROVIDER, "games", "fbs", year)
    upserted = skipped = 0
    for raw in records:
        src = raw.get("raw") if isinstance(raw.get("raw"), dict) else raw
        ext = as_str(raw.get("provider_record_id")) or as_str(src.get("id"))
        home_id = as_str(raw.get("home_id")) or as_str(src.get("homeId")) or as_str(src.get("home_id"))
        away_id = as_str(raw.get("away_id")) or as_str(src.get("awayId")) or as_str(src.get("away_id"))
        home_name = as_str(raw.get("home_team")) or as_str(src.get("homeTeam"))
        away_name = as_str(raw.get("away_team")) or as_str(src.get("awayTeam"))
        if not ext or not home_id or not away_id:
            skipped += 1
            continue
        game_id = fbs_game_id(ext)
        espn_id = f"espn:fbs:{ext}"
        # Prefer the ESPN slate row when present — do not create a parallel fbs:{id} duplicate.
        existing = session.get(Game, game_id) or session.get(Game, espn_id)
        home_pts = as_int(src.get("homePoints") or src.get("home_points"), None)
        away_pts = as_int(src.get("awayPoints") or src.get("away_points"), None)
        status = "final" if home_pts is not None and away_pts is not None else "scheduled"
        if existing is not None:
            if status == "final" and existing.status != "final":
                existing.status = "final"
            if status == "final" and existing.data_tier == "upcoming":
                existing.data_tier = "historical"
            if home_pts is not None and away_pts is not None and (
                existing.home_score is None or existing.away_score is None
            ):
                existing.home_score = home_pts
                existing.away_score = away_pts
            map_provider(session, PROVIDER, "game", ext, existing.game_id)
            skipped += 1
            continue
        _ensure_fbs_team(session, home_id, home_name, as_str(raw.get("home_conference")))
        _ensure_fbs_team(session, away_id, away_name, as_str(raw.get("away_conference")))
        venue_name = as_str(raw.get("venue")) or as_str(src.get("venue")) or f"{home_name} stadium"
        venue = ensure_venue(session, fbs_venue_id(venue_name), venue_name)
        kickoff = parse_kickoff(raw.get("kickoff") or src.get("startDate") or src.get("start_date"), None)
        if kickoff is None:
            kickoff = datetime(year, 9, 1, tzinfo=UTC)
        session.add(
            Game(
                game_id=game_id,
                season_id=fbs_season_id(year),
                league_id="fbs",
                home_team_id=fbs_team_id(home_id),
                away_team_id=fbs_team_id(away_id),
                kickoff_utc=kickoff,
                venue_id=venue.venue_id,
                status=status,
                week=as_int(raw.get("week") or src.get("week"), None),
                neutral_site=bool(raw.get("neutral_site") or src.get("neutralSite")),
                data_tier="historical",
                home_score=home_pts,
                away_score=away_pts,
            )
        )
        map_provider(session, PROVIDER, "game", ext, game_id)
        upserted += 1
    finish_run(session, run, status="ok", upserted=upserted, skipped=skipped)
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="games",
        league_id="fbs",
        season_year=year,
        row_count=upserted,
        status="available",
    )
    session.flush()
    return upserted, skipped


def ingest_roster(session: Session, records: list[dict[str, Any]], year: int, *, observed_at: datetime) -> int:
    run = start_run(session, PROVIDER, "roster", "fbs", year)
    upserted = 0
    start = datetime(year, 8, 1, tzinfo=UTC)
    for raw in records:
        ext = as_str(raw.get("id")) or as_str(raw.get("athleteId"))
        name = as_str(raw.get("firstName") or raw.get("first_name") or "")
        last = as_str(raw.get("lastName") or raw.get("last_name") or "")
        display = " ".join(p for p in [name, last] if p) or as_str(raw.get("name"))
        team_ext = as_str(raw.get("teamId") or raw.get("team_id") or raw.get("team"))
        if not ext or not display:
            continue
        pid = fbs_player_id(ext)
        pos = as_str(raw.get("position")) or "UNK"
        existed = session.get(Player, pid) is not None or pid in session.info.get("cfbd_players", {})
        ensure_cfbd_player(session, ext, display, pos)
        if not existed:
            upserted += 1
        if team_ext and session.get(Team, fbs_team_id(team_ext)) is not None:
            exists = (
                session.query(Roster)
                .filter(Roster.team_id == fbs_team_id(team_ext), Roster.player_id == pid)
                .first()
            )
            if exists is None:
                session.add(
                    Roster(
                        team_id=fbs_team_id(team_ext),
                        player_id=pid,
                        valid_from=start,
                        status="active",
                        jersey=as_str(raw.get("jersey")),
                    )
                )
    finish_run(session, run, status="ok", upserted=upserted)
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="roster",
        league_id="fbs",
        season_year=year,
        row_count=upserted,
        status="available",
    )
    session.flush()
    return upserted


def ingest_ppa_games(session: Session, records: list[dict[str, Any]], year: int, *, observed_at: datetime) -> int:
    """Store PPA-backed player-game rows when CFBD provides them. Missing fields stay unset."""
    run = start_run(session, PROVIDER, "ppa_players", "fbs", year)
    upserted = skipped = 0
    for raw in records:
        player_ext = as_str(raw.get("playerId") or raw.get("id") or raw.get("athleteId"))
        game_ext = as_str(raw.get("gameId") or raw.get("game_id"))
        if not player_ext or not game_ext:
            skipped += 1
            continue
        pid = fbs_player_id(player_ext)
        gid = fbs_game_id(game_ext)
        if session.get(Game, gid) is None or session.get(Player, pid) is None:
            skipped += 1
            continue
        existing = (
            session.query(PlayerGameStat)
            .filter_by(game_id=gid, player_id=pid, provider=PROVIDER)
            .first()
        )
        if existing:
            skipped += 1
            continue
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        session.add(
            PlayerGameStat(
                game_id=gid,
                player_id=pid,
                attempts=as_int(usage.get("pass"), 0) or 0,
                routes=0,
                targets=as_int(raw.get("targets"), 0) or 0,
                carries=as_int(usage.get("rush"), 0) or 0,
                receptions=as_int(raw.get("receptions"), 0) or 0,
                rush_yards=as_float(raw.get("rushingYards")) or 0.0,
                rec_yards=as_float(raw.get("receivingYards")) or 0.0,
                rush_td=as_int(raw.get("rushingTDs"), 0) or 0,
                rec_td=as_int(raw.get("receivingTDs"), 0) or 0,
                pass_td=as_int(raw.get("passingTDs"), 0) or 0,
                rz_targets=0,
                rz_carries=0,
                provider=PROVIDER,
                provider_record_id=f"{gid}:{pid}"[:160],
                observed_at=observed_at,
                effective_at=observed_at,
                quality_status="partial_no_charting",
            )
        )
        upserted += 1
    finish_run(session, run, status="ok", upserted=upserted, skipped=skipped)
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="ppa_players",
        league_id="fbs",
        season_year=year,
        row_count=upserted,
        status="available" if records else "empty",
        notes="PPA/usage when present. Route and RZ charting still unavailable.",
    )
    session.flush()
    return upserted


def _fbs_play_game_index(session: Session) -> dict[str, str]:
    """Map CFBD / ESPN-style game keys onto the canonical DB ``game_id``.

    2026 slate games live as ``espn:fbs:{id}`` while CFBD plays return the numeric
    CFBD id (normalized to ``fbs:{id}``). Historical CFBD games use ``fbs:{id}``.
    Without this bridge, 2026 PBP never attaches and team analytics stay empty.
    """
    index: dict[str, str] = {}
    for (gid,) in session.query(Game.game_id).filter(Game.league_id == "fbs"):
        index[gid] = gid
        if gid.startswith("espn:fbs:"):
            numeric = gid.rsplit(":", 1)[-1]
            index[f"fbs:{numeric}"] = gid
            index[numeric] = gid
        elif gid.startswith("fbs:"):
            numeric = gid.split(":", 1)[-1]
            index[f"espn:fbs:{numeric}"] = gid
            index[numeric] = gid
    return index


def ingest_plays(session: Session, records: list[dict[str, Any]], year: int, *, observed_at: datetime) -> int:
    from uuid import uuid4

    import pandas as pd
    from sqlalchemy import insert

    from spe.analytics.plays import cfbd_to_canonical

    run = start_run(session, PROVIDER, "plays", "fbs", year)
    canon = cfbd_to_canonical(records)
    if canon.empty:
        finish_run(session, run, status="empty", upserted=0)
        return 0
    game_key_to_id = _fbs_play_game_index(session)
    game_sides = {
        row.game_id: (row.home_team_id, row.away_team_id)
        for row in session.query(Game.game_id, Game.home_team_id, Game.away_team_id).filter(Game.league_id == "fbs")
    }
    existing_teams = {row[0] for row in session.query(Team.team_id).filter(Team.league_id == "fbs")}
    name_map: dict[str, str] = {}
    for row in session.query(Team.team_id, Team.name, Team.abbreviation).filter(Team.league_id == "fbs"):
        _remember_team_name(name_map, _norm_name(row.name), row.team_id)
        _remember_team_name(name_map, _norm_name(row.abbreviation), row.team_id)
    provider_ids = {str(g) for g in canon["provider_game_id"].dropna().unique()}
    gids = {
        game_key_to_id[key]
        for pid in provider_ids
        for key in (fbs_game_id(pid), str(pid), f"espn:fbs:{str(pid).split(':')[-1]}")
        if key in game_key_to_id
    }
    if not gids:
        finish_run(session, run, status="empty", upserted=0)
        return 0
    seen = {
        (g, p)
        for g, p in session.query(Play.game_id, Play.play_id).filter(Play.provider == PROVIDER, Play.game_id.in_(gids))
    }
    mappings: list[dict[str, Any]] = []
    for rec in canon.itertuples(index=False):
        if not rec.provider_game_id or not rec.play_id:
            continue
        raw_pid = str(rec.provider_game_id)
        gid = (
            game_key_to_id.get(fbs_game_id(raw_pid))
            or game_key_to_id.get(raw_pid)
            or game_key_to_id.get(f"espn:fbs:{raw_pid.split(':')[-1]}")
        )
        play_id = str(rec.play_id)[:64]
        if not gid or (gid, play_id) in seen or gid not in game_sides:
            continue
        home_id, away_id = game_sides[gid]
        offense, defense = _resolve_play_teams(rec, home_id, away_id, existing_teams, name_map)
        if not offense or not defense:
            continue
        success = rec.success
        if success is not None:
            try:
                success = None if pd.isna(success) else bool(success)
            except (TypeError, ValueError):
                success = bool(success)
        mappings.append(
            {
                "play_pk": str(uuid4()),
                "game_id": gid,
                "play_id": play_id,
                "period": _maybe_int(rec.period, 1) or 1,
                "clock_seconds": (_maybe_int(rec.game_seconds_remaining, 0) or 0) % 900,
                "down": _maybe_int(rec.down, None),
                "distance": _maybe_float(rec.distance),
                "yardline": _maybe_int(rec.yardline, None),
                "offense_id": offense,
                "defense_id": defense,
                "play_type": str(rec.play_type)[:64] if rec.play_type else "unknown",
                "yards": _maybe_float(rec.yards) or 0.0,
                "epa": _maybe_float(rec.epa),
                "ppa": _maybe_float(rec.ppa),
                "wp": None,
                "success": success,
                "is_pass": bool(rec.is_pass),
                "is_rush": bool(rec.is_rush),
                "is_td": bool(rec.is_td),
                "is_turnover": bool(rec.is_turnover),
                "is_red_zone": bool(rec.is_red_zone),
                "is_goal_line": bool(rec.is_goal_line),
                "is_scrimmage": bool(rec.is_scrimmage),
                "first_down": bool(rec.first_down),
                "game_seconds_remaining": _maybe_int(rec.game_seconds_remaining, None),
                "drive_id": str(rec.drive_id)[:32] if rec.drive_id else None,
                "score_before_off": _maybe_int(rec.score_off, 0) or 0,
                "score_before_def": _maybe_int(rec.score_def, 0) or 0,
                "provider": PROVIDER,
                "provider_record_id": f"{rec.provider_game_id}:{play_id}"[:160],
                "observed_at": observed_at,
                "effective_at": observed_at,
                "ingested_at": observed_at,
                "revision": 1,
                "quality_status": "ok",
            }
        )
        seen.add((gid, play_id))
    upserted = 0
    stmt = insert(Play)
    for i in range(0, len(mappings), 1500):
        chunk = mappings[i : i + 1500]
        session.execute(stmt, chunk)
        upserted += len(chunk)
        session.flush()
    finish_run(session, run, status="ok", upserted=upserted)
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="plays",
        league_id="fbs",
        season_year=year,
        row_count=upserted,
        status="available" if upserted else "empty",
        notes="PPA mapped onto epa when present. Player IDs are often absent on CFBD plays.",
    )
    session.flush()
    return upserted


def _ensure_fbs_team(session: Session, ext: str, name: str | None, conference: str | None) -> Team:
    tid = fbs_team_id(ext)
    row = session.get(Team, tid)
    if row:
        return row
    row = Team(
        team_id=tid,
        league_id="fbs",
        name=(name or ext)[:128],
        abbreviation=(name or ext)[:16],
        conference=conference,
        aliases=[],
    )
    session.add(row)
    map_provider(session, PROVIDER, "team", ext, tid)
    session.flush()
    return row


def _records(
    adapter: CfbdAdapter,
    key: str,
    params: dict[str, Any],
    fixtures: dict[str, list[dict[str, Any]]] | None,
    season: int | None = None,
) -> list[dict[str, Any]]:
    fixture_key = f"{key}:{season}" if season is not None and fixtures and f"{key}:{season}" in fixtures else key
    if fixtures and fixture_key in fixtures:
        return adapter.fetch(key, params, fixture=fixtures[fixture_key]).records
    try:
        return adapter.fetch(key, params).records
    except AdapterError:
        return []


def _norm_name(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _remember_team_name(name_map: dict[str, str], key: str, team_id: str) -> None:
    """Keep ``fbs:61`` when a short id such as ``uga`` shares the abbreviation."""
    if not key:
        return
    current = name_map.get(key)
    if current is None or (historical_fbs_id(team_id) and not historical_fbs_id(current)):
        name_map[key] = team_id


def _resolve_play_teams(
    rec: Any,
    home_id: str,
    away_id: str,
    existing_teams: set[str],
    name_map: dict[str, str],
) -> tuple[str | None, str | None]:
    off_raw = str(rec.offense_abbr or "")
    def_raw = str(rec.defense_abbr or "")
    off_id = fbs_team_id(off_raw)
    def_id = fbs_team_id(def_raw)
    if off_id in existing_teams and def_id in existing_teams:
        return off_id, def_id
    off_n = _norm_name(off_raw)
    home_n = _norm_name(getattr(rec, "home_name", ""))
    away_n = _norm_name(getattr(rec, "away_name", ""))
    if home_n and off_n == home_n:
        return home_id, away_id
    if away_n and off_n == away_n:
        return away_id, home_id
    offense = name_map.get(off_n)
    defense = name_map.get(_norm_name(def_raw))
    if offense and defense:
        return offense, defense
    return None, None


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _maybe_int(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        import pandas as pd

        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
