"""Ingest nflverse CC-BY historical schedules, players, weekly stats, optional PBP.

Do not invent route/RZ charting. Do not treat observed weather as a pregame forecast.
Closing spread/total on the schedule are consensus numbers, not bookmaker ticks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from spe.adapters.nflverse import NflverseAdapter
from spe.config import settings
from spe.db.models import Game, InjuryEvent, Player, PlayerGameStat, Play, Roster, Team, WeatherSnapshot
from spe.ingest.canonical import (
    f_to_c,
    mph_to_mps,
    nfl_game_id,
    nfl_player_id,
    nfl_season_id,
    nfl_team_id,
    nfl_venue_id,
    parse_kickoff,
    position_group,
)
from spe.ingest.http import download_file
from spe.ingest.nws_live import board_tier
from spe.ingest.stadiums import lookup_stadium
from spe.ingest.store import (
    ensure_football_leagues,
    ensure_nfl_team,
    ensure_season,
    ensure_venue,
    finish_run,
    map_provider,
    start_run,
    upsert_coverage,
)
from spe.ingest.values import as_float, as_int, as_str, is_missing

DEFAULT_SEASONS = tuple(range(2016, 2027))
PROVIDER = "nflverse"
FTN_PREFIX = "ftn_"


def cache_dir() -> Path:
    path = Path(settings.parquet_dir).resolve().parent / "cache" / "nflverse"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_asset(key: str, season: int | None = None, *, refresh: bool = False) -> pd.DataFrame:
    adapter = NflverseAdapter()
    url = adapter.asset_url(key, season)
    filename = url.rsplit("/", 1)[-1]
    dest = cache_dir() / filename
    download_file(url, dest, refresh=refresh)
    return pd.read_parquet(dest)


def ingest_nfl_history(
    session: Session,
    *,
    seasons: tuple[int, ...] | list[int] = DEFAULT_SEASONS,
    include_pbp: bool = False,
    refresh: bool = False,
    schedules: pd.DataFrame | None = None,
    players: pd.DataFrame | None = None,
    weekly: dict[int, pd.DataFrame] | None = None,
    pbp: dict[int, pd.DataFrame] | None = None,
    injuries: dict[int, pd.DataFrame] | None = None,
    observed_at: datetime | None = None,
    injuries_only: bool = False,
) -> dict[str, Any]:
    observed = observed_at or datetime.now(UTC)
    ensure_football_leagues(session)
    for year in seasons:
        ensure_season(session, "nfl", year)

    g_up = g_skip = p_up = p_skip = weekly_up = weekly_skip = 0
    if not injuries_only:
        games_df = schedules if schedules is not None else load_asset("schedules", refresh=refresh)
        players_df = players if players is not None else load_asset("players", refresh=refresh)

        g_up, g_skip = ingest_schedules(session, games_df, seasons=seasons, observed_at=observed)
        p_up, p_skip = ingest_players(session, players_df, observed_at=observed)

        for year in seasons:
            df = (weekly or {}).get(year)
            if df is None:
                try:
                    df = load_asset("player_stats", year, refresh=refresh)
                except Exception as exc:  # noqa: BLE001
                    upsert_coverage(
                        session,
                        provider=PROVIDER,
                        dataset="player_week_stats",
                        league_id="nfl",
                        season_year=year,
                        row_count=0,
                        status="error",
                        notes=str(exc),
                    )
                    continue
            u, s = ingest_weekly_stats(session, df, year, observed_at=observed)
            weekly_up += u
            weekly_skip += s

    inj_up = 0
    for year in seasons:
        df = (injuries or {}).get(year)
        if df is None:
            try:
                df = load_asset("injuries", year, refresh=refresh)
            except Exception:
                upsert_coverage(
                    session,
                    provider=PROVIDER,
                    dataset="injuries",
                    league_id="nfl",
                    season_year=year,
                    row_count=0,
                    status="unavailable",
                    notes="injuries parquet not downloaded; injury coverage remains incomplete.",
                )
                continue
        inj_up += ingest_injuries(session, df, year, observed_at=observed)

    pbp_up = 0
    if include_pbp and not injuries_only:
        for year in seasons:
            df = (pbp or {}).get(year)
            if df is None:
                try:
                    df = load_asset("pbp", year, refresh=refresh)
                except Exception as exc:  # noqa: BLE001
                    upsert_coverage(
                        session,
                        provider=PROVIDER,
                        dataset="pbp",
                        league_id="nfl",
                        season_year=year,
                        row_count=0,
                        status="error",
                        notes=str(exc),
                    )
                    continue
            added = ingest_pbp(session, df, year, observed_at=observed)
            pbp_up += added
            session.commit()
            print(f"SEASON_DONE nfl pbp {year} plays={added} total={pbp_up}", flush=True)
    elif not injuries_only:
        for year in seasons:
            upsert_coverage(
                session,
                provider=PROVIDER,
                dataset="pbp",
                league_id="nfl",
                season_year=year,
                row_count=0,
                status="skipped",
                notes="Play-by-play ingest is optional (large). Pass --pbp to download.",
            )

    if not injuries_only:
        upsert_coverage(
            session,
            provider=PROVIDER,
            dataset="charting_routes_rz",
            league_id="nfl",
            season_year=0,
            row_count=0,
            status="requires_contract",
            notes="Route participation, end-zone targets, and inside-5 carries are not in weekly CC-BY box scores. Do not treat stored 0s as observed zeros.",
        )
    session.flush()
    return {
        "games_upserted": g_up,
        "games_skipped": g_skip,
        "players_upserted": p_up,
        "players_skipped": p_skip,
        "weekly_stats_upserted": weekly_up,
        "weekly_stats_skipped": weekly_skip,
        "injuries_upserted": inj_up,
        "plays_upserted": pbp_up,
        "seasons": list(seasons),
        "injuries_only": injuries_only,
    }


def ingest_schedules(
    session: Session,
    df: pd.DataFrame,
    *,
    seasons: tuple[int, ...] | list[int],
    observed_at: datetime,
) -> tuple[int, int]:
    run = start_run(session, PROVIDER, "schedules", "nfl", None)
    season_set = {int(y) for y in seasons}
    upserted = skipped = 0
    existing = set(session.query(Game.game_id).all())
    existing_ids = {row[0] for row in existing}
    for _, raw in df.iterrows():
        year = as_int(raw.get("season"), None)
        if year is None or year not in season_set:
            continue
        gid_raw = as_str(raw.get("game_id"))
        home = as_str(raw.get("home_team"))
        away = as_str(raw.get("away_team"))
        if not gid_raw or not home or not away:
            skipped += 1
            continue
        game_id = nfl_game_id(gid_raw)
        if game_id in existing_ids:
            skipped += 1
            continue
        kickoff = parse_kickoff(raw.get("gameday"), raw.get("gametime"))
        if kickoff is None:
            skipped += 1
            continue
        ensure_season(session, "nfl", year)
        home_team = ensure_nfl_team(session, home)
        away_team = ensure_nfl_team(session, away)
        stadium = as_str(raw.get("stadium")) or f"{home} home"
        site = lookup_stadium(as_str(raw.get("stadium_id")), stadium)
        venue = ensure_venue(
            session,
            nfl_venue_id(stadium),
            stadium,
            roof=as_str(raw.get("roof")) or "open",
            surface=as_str(raw.get("surface")) or "unknown",
            lat=site.lat if site else 0.0,
            lon=site.lon if site else 0.0,
        )
        if site and abs(venue.lat) < 0.01 and abs(venue.lon) < 0.01:
            venue.lat = site.lat
            venue.lon = site.lon
            venue.altitude_m = site.altitude_m
            venue.timezone = site.timezone
        home_score = as_int(raw.get("home_score"), None)
        away_score = as_int(raw.get("away_score"), None)
        status = "final" if home_score is not None and away_score is not None else "scheduled"
        game = Game(
            game_id=game_id,
            season_id=nfl_season_id(year),
            league_id="nfl",
            home_team_id=home_team.team_id,
            away_team_id=away_team.team_id,
            kickoff_utc=kickoff,
            venue_id=venue.venue_id,
            status=status,
            week=as_int(raw.get("week"), None),
            data_tier=board_tier(status, kickoff, observed_at),
            home_score=home_score,
            away_score=away_score,
        )
        session.add(game)
        map_provider(session, PROVIDER, "game", gid_raw, game_id)
        session.flush()
        existing_ids.add(game_id)
        upserted += 1
        _maybe_observed_weather(session, game, venue.venue_id, raw, observed_at)
        if upserted % 400 == 0:
            session.flush()
    finish_run(session, run, status="ok", upserted=upserted, skipped=skipped, notes="nflverse games.parquet")
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="schedules",
        league_id="nfl",
        season_year=0,
        row_count=upserted,
        status="available",
        notes=f"Seasons {min(season_set)}-{max(season_set)}. Closing lines on the file are consensus, not book ticks.",
    )
    session.flush()
    return upserted, skipped


def ingest_players(session: Session, df: pd.DataFrame, *, observed_at: datetime) -> tuple[int, int]:
    run = start_run(session, PROVIDER, "players", "nfl", None)
    upserted = skipped = 0
    for _, raw in df.iterrows():
        gsis = as_str(raw.get("gsis_id")) or as_str(raw.get("player_id"))
        name = as_str(raw.get("display_name")) or as_str(raw.get("player_name"))
        if not gsis or not name:
            skipped += 1
            continue
        pid = nfl_player_id(gsis)
        if session.get(Player, pid) is None:
            pos = as_str(raw.get("position")) or "UNK"
            session.add(
                Player(
                    player_id=pid,
                    name=name[:128],
                    birth_date=as_str(raw.get("birth_date")),
                    position_group=as_str(raw.get("position_group")) or position_group(pos),
                    position=pos[:16],
                    aliases=[],
                )
            )
            map_provider(session, PROVIDER, "player", gsis, pid)
            upserted += 1
        else:
            skipped += 1
        espn = as_str(raw.get("espn_id"))
        if espn:
            espn_key = str(int(float(espn))) if str(espn).replace(".", "", 1).isdigit() else espn
            from spe.identity.games import remap_provider

            remap_provider(session, "espn", "player", espn_key, pid, notes="nflverse_players_espn_id")
        if upserted and upserted % 1000 == 0:
            session.flush()
    finish_run(session, run, status="ok", upserted=upserted, skipped=skipped)
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="players",
        league_id="nfl",
        season_year=0,
        row_count=upserted,
        status="available",
        notes="Canonical player_id = nfl:{gsis_id}. ESPN ids mapped when present.",
    )
    session.flush()
    return upserted, skipped


def ingest_weekly_stats(session: Session, df: pd.DataFrame, year: int, *, observed_at: datetime) -> tuple[int, int]:
    run = start_run(session, PROVIDER, "player_week_stats", "nfl", year)
    index = _game_index(session, year)
    existing = {
        (g, p)
        for g, p in session.query(PlayerGameStat.game_id, PlayerGameStat.player_id).filter(
            PlayerGameStat.provider == PROVIDER
        )
    }
    upserted = skipped = 0
    for _, raw in df.iterrows():
        season = as_int(raw.get("season"), year) or year
        week = as_int(raw.get("week"), None)
        team = as_str(raw.get("recent_team")) or as_str(raw.get("team"))
        player_ext = as_str(raw.get("player_id")) or as_str(raw.get("gsis_id"))
        if week is None or not team or not player_ext:
            skipped += 1
            continue
        game_id = index.get((season, week, team.upper()))
        if not game_id:
            skipped += 1
            continue
        pid = nfl_player_id(player_ext)
        if session.get(Player, pid) is None:
            name = as_str(raw.get("player_display_name")) or as_str(raw.get("player_name")) or player_ext
            pos = as_str(raw.get("position")) or "UNK"
            session.add(
                Player(
                    player_id=pid,
                    name=name[:128],
                    position_group=as_str(raw.get("position_group")) or position_group(pos),
                    position=pos[:16],
                    aliases=[],
                )
            )
            map_provider(session, PROVIDER, "player", player_ext, pid)
            session.flush()
        if (game_id, pid) in existing:
            skipped += 1
            continue
        team_id = nfl_team_id(team)
        _ensure_roster(session, team_id, pid, year)
        rec_id = f"{game_id}:{pid}:{year}:{week}"
        session.add(
            PlayerGameStat(
                game_id=game_id,
                player_id=pid,
                attempts=as_int(raw.get("attempts"), 0) or 0,
                routes=0,
                targets=as_int(raw.get("targets"), 0) or 0,
                carries=as_int(raw.get("carries"), 0) or as_int(raw.get("rushing_attempts"), 0) or 0,
                receptions=as_int(raw.get("receptions"), 0) or 0,
                rush_yards=as_float(raw.get("rushing_yards")) or 0.0,
                rec_yards=as_float(raw.get("receiving_yards")) or 0.0,
                rush_td=as_int(raw.get("rushing_tds"), 0) or 0,
                rec_td=as_int(raw.get("receiving_tds"), 0) or 0,
                pass_td=as_int(raw.get("passing_tds"), 0) or 0,
                rz_targets=0,
                rz_carries=0,
                provider=PROVIDER,
                provider_record_id=rec_id[:160],
                observed_at=observed_at,
                effective_at=observed_at,
                quality_status="partial_no_charting",
            )
        )
        existing.add((game_id, pid))
        upserted += 1
        if upserted % 500 == 0:
            session.flush()
    finish_run(
        session,
        run,
        status="ok",
        upserted=upserted,
        skipped=skipped,
        notes="routes/rz_targets/rz_carries are schema placeholders, not observed charting.",
    )
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="player_week_stats",
        league_id="nfl",
        season_year=year,
        row_count=upserted,
        status="available",
        notes="targets, receptions, yards, TDs present. Route and RZ splits unavailable.",
    )
    session.flush()
    return upserted, skipped


def ingest_injuries(session: Session, df: pd.DataFrame, year: int, *, observed_at: datetime) -> int:
    run = start_run(session, PROVIDER, "injuries", "nfl", year)
    index = _game_index(session, year)
    seen = {
        rec_id
        for (rec_id,) in session.query(InjuryEvent.provider_record_id).filter(InjuryEvent.provider == PROVIDER)
    }
    upserted = 0
    for _, raw in df.iterrows():
        player_ext = as_str(raw.get("gsis_id")) or as_str(raw.get("player_id"))
        if not player_ext:
            continue
        pid = nfl_player_id(player_ext)
        if session.get(Player, pid) is None:
            continue
        week = as_int(raw.get("week"), None)
        team = as_str(raw.get("team"))
        game_id = index.get((year, week, team.upper())) if week and team else None
        date_raw = as_str(raw.get("date_modified")) or as_str(raw.get("report_date"))
        reported = observed_at
        if date_raw:
            try:
                reported = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
                if reported.tzinfo is None:
                    reported = reported.replace(tzinfo=UTC)
            except ValueError:
                reported = observed_at
        designation = as_str(raw.get("report_status")) or as_str(raw.get("practice_status")) or "unknown"
        rec_id = f"{pid}:{year}:{week}:{designation}"
        if rec_id[:160] in seen:
            continue
        seen.add(rec_id[:160])
        session.add(
            InjuryEvent(
                game_id=game_id,
                player_id=pid,
                designation=designation[:32],
                practice_status=as_str(raw.get("practice_status")),
                reported_at=reported,
                source="nflverse.injuries",
                confidence=0.7,
                coverage_complete=True,
                provider=PROVIDER,
                provider_record_id=rec_id[:160],
                observed_at=observed_at,
                effective_at=reported,
                quality_status="ok",
            )
        )
        upserted += 1
        if upserted % 400 == 0:
            session.flush()
    finish_run(session, run, status="ok", upserted=upserted, skipped=0)
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="injuries",
        league_id="nfl",
        season_year=year,
        row_count=upserted,
        status="available" if upserted else "empty",
        notes="A missing injury row is never treated as healthy.",
    )
    session.flush()
    return upserted


def ingest_pbp(session: Session, df: pd.DataFrame, year: int, *, observed_at: datetime) -> int:
    from uuid import uuid4

    from sqlalchemy import insert

    from spe.analytics.engine import derive_opportunity
    from spe.analytics.plays import nflverse_to_canonical

    run = start_run(session, PROVIDER, "pbp", "nfl", year)
    existing_games = {row[0] for row in session.query(Game.game_id).filter(Game.league_id == "nfl")}
    seen = {
        (g, p)
        for g, p in session.query(Play.game_id, Play.play_id).filter(Play.provider == PROVIDER)
    }
    canon = nflverse_to_canonical(df)
    if canon.empty:
        finish_run(session, run, status="empty", upserted=0, skipped=0)
        return 0
    canon = canon[canon["provider_game_id"].notna() & canon["play_id"].notna()]
    canon["game_id"] = canon["provider_game_id"].map(lambda x: nfl_game_id(str(x)))
    canon = canon[canon["game_id"].isin(existing_games)]
    if seen:
        keep = [(g, str(p)) not in seen for g, p in zip(canon["game_id"], canon["play_id"], strict=False)]
        canon = canon.loc[keep]
    skipped = int(len(df) - len(canon))
    abbrs = pd.concat([canon["offense_abbr"], canon["defense_abbr"]], ignore_index=True).dropna().unique()
    for abbr in abbrs:
        text = str(abbr).strip().upper()
        if text and text not in {"NAN", "NONE", ""}:
            ensure_nfl_team(session, text)
    session.flush()
    mappings: list[dict] = []
    for rec in canon.itertuples(index=False):
        off = str(rec.offense_abbr).strip().upper()
        deff = str(rec.defense_abbr).strip().upper()
        if not off or not deff or off in {"NAN", "NONE"}:
            skipped += 1
            continue
        pid = str(rec.play_id)[:64]
        mappings.append(
            {
                "play_pk": str(uuid4()),
                "game_id": rec.game_id,
                "play_id": pid,
                "period": int(rec.period) if pd.notna(rec.period) else 1,
                "clock_seconds": int(rec.game_seconds_remaining) % 3600 if pd.notna(rec.game_seconds_remaining) else 0,
                "down": int(rec.down) if pd.notna(rec.down) else None,
                "distance": float(rec.distance) if pd.notna(rec.distance) else None,
                "yardline": int(rec.yardline) if pd.notna(rec.yardline) else None,
                "offense_id": nfl_team_id(off),
                "defense_id": nfl_team_id(deff),
                "play_type": str(rec.play_type)[:64] if rec.play_type else "unknown",
                "yards": float(rec.yards) if pd.notna(rec.yards) else 0.0,
                "epa": float(rec.epa) if pd.notna(rec.epa) else None,
                "ppa": None,
                "wp": float(rec.wp) if pd.notna(rec.wp) else None,
                "success": bool(rec.success) if pd.notna(rec.success) else None,
                "is_pass": bool(rec.is_pass),
                "is_rush": bool(rec.is_rush),
                "is_td": bool(rec.is_td),
                "is_turnover": bool(rec.is_turnover),
                "is_red_zone": bool(rec.is_red_zone),
                "is_goal_line": bool(rec.is_goal_line),
                "is_scrimmage": bool(rec.is_scrimmage),
                "first_down": bool(rec.first_down),
                "game_seconds_remaining": int(rec.game_seconds_remaining) if pd.notna(rec.game_seconds_remaining) else None,
                "drive_id": str(rec.drive_id)[:32] if rec.drive_id else None,
                "passer_player_id": rec.passer_player_id,
                "rusher_player_id": rec.rusher_player_id,
                "receiver_player_id": rec.receiver_player_id,
                "score_before_off": int(rec.score_off) if pd.notna(rec.score_off) else 0,
                "score_before_def": int(rec.score_def) if pd.notna(rec.score_def) else 0,
                "provider": PROVIDER,
                "provider_record_id": f"{rec.provider_game_id}:{pid}"[:160],
                "observed_at": observed_at,
                "effective_at": observed_at,
                "ingested_at": observed_at,
                "revision": 1,
                "quality_status": "ok",
            }
        )
    upserted = 0
    stmt = insert(Play)
    for i in range(0, len(mappings), 1500):
        session.execute(stmt, mappings[i : i + 1500])
        upserted += len(mappings[i : i + 1500])
        session.flush()
    _apply_rz_from_canonical(session, canon)
    finish_run(session, run, status="ok", upserted=upserted, skipped=skipped, notes="FTN/CC-BY-SA columns dropped. EPA+WP from nflverse.")
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="pbp",
        league_id="nfl",
        season_year=year,
        row_count=upserted,
        status="available",
        notes="EPA/WP from nflverse PBP. FTN charting columns excluded. RZ/GL opportunities derived from identified ball-handlers.",
    )
    session.flush()
    return upserted


def _apply_rz_from_canonical(session: Session, canon: pd.DataFrame) -> None:
    from spe.analytics.engine import derive_opportunity

    if canon.empty or "game_id" not in canon.columns:
        return
    derived = derive_opportunity(canon)
    if derived.empty:
        return
    gids = set(derived["game_id"])
    stats = (
        session.query(PlayerGameStat)
        .filter(PlayerGameStat.game_id.in_(gids), PlayerGameStat.provider == PROVIDER)
        .all()
    )
    index = {(s.game_id, s.player_id): s for s in stats}
    for rec in derived.itertuples(index=False):
        row = index.get((rec.game_id, rec.player_id))
        if row is None:
            continue
        row.rz_targets = int(rec.rz_targets)
        row.rz_carries = int(rec.rz_carries)
        row.gl_carries = int(rec.gl_carries)
        if row.quality_status == "partial_no_charting":
            row.quality_status = "pbp_opportunities"


def _game_index(session: Session, year: int) -> dict[tuple[int, int, str], str]:
    season_id = nfl_season_id(year)
    index: dict[tuple[int, int, str], str] = {}
    games = session.query(Game).filter(Game.season_id == season_id).all()
    for game in games:
        if game.week is None:
            continue
        home = game.home_team_id.replace("nfl:", "")
        away = game.away_team_id.replace("nfl:", "")
        index[(year, game.week, home)] = game.game_id
        index[(year, game.week, away)] = game.game_id
    return index


def _ensure_roster(session: Session, team_id: str, player_id: str, year: int) -> None:
    if session.get(Team, team_id) is None:
        abbr = team_id.split(":", 1)[-1]
        ensure_nfl_team(session, abbr)
    existing = (
        session.query(Roster)
        .filter(Roster.team_id == team_id, Roster.player_id == player_id)
        .first()
    )
    if existing:
        return
    start = datetime(year, 9, 1, tzinfo=UTC)
    session.add(
        Roster(
            team_id=team_id,
            player_id=player_id,
            valid_from=start,
            status="active",
        )
    )


def _maybe_observed_weather(
    session: Session,
    game: Game,
    venue_id: str,
    raw: Any,
    observed_at: datetime,
) -> None:
    temp_f = as_float(raw.get("temp"))
    wind_mph = as_float(raw.get("wind"))
    if temp_f is None and wind_mph is None:
        return
    session.add(
        WeatherSnapshot(
            venue_id=venue_id,
            game_id=game.game_id,
            forecast_for=game.kickoff_utc,
            issued_at=game.kickoff_utc,
            temperature_c=f_to_c(temp_f),
            wind_mps=mph_to_mps(wind_mph),
            roof_status=as_str(raw.get("roof")) or "open",
            provider=PROVIDER,
            provider_record_id=f"obs-weather:{game.game_id}"[:160],
            observed_at=observed_at,
            effective_at=game.kickoff_utc,
            quality_status="observed_not_forecast",
        )
    )


def backfill_opportunity_from_plays(session: Session) -> dict[str, Any]:
    """Fill rz/gl counts from plays already stored. Does not download play-by-play."""
    from spe.analytics.engine import derive_opportunity

    game_ids = [row[0] for row in session.query(Play.game_id).distinct()]
    filled = {
        row[0]
        for row in session.query(PlayerGameStat.game_id)
        .filter(PlayerGameStat.provider == PROVIDER, PlayerGameStat.quality_status == "pbp_opportunities")
        .distinct()
    }
    game_ids = [game_id for game_id in game_ids if game_id not in filled]
    if not game_ids:
        return {"status": "already_filled" if filled else "no_plays", "games": 0, "players_updated": 0}
    updated = 0
    games = 0
    for start in range(0, len(game_ids), 40):
        chunk = game_ids[start : start + 40]
        plays = session.query(Play).filter(Play.game_id.in_(chunk)).all()
        if not plays:
            continue
        frame = pd.DataFrame(
            [
                {
                    "game_id": row.game_id,
                    "is_red_zone": bool(row.is_red_zone),
                    "is_goal_line": bool(row.is_goal_line),
                    "is_rush": bool(row.is_rush),
                    "pass_attempt": bool(row.is_pass),
                    "receiver_player_id": row.receiver_player_id,
                    "rusher_player_id": row.rusher_player_id,
                }
                for row in plays
            ]
        )
        derived = derive_opportunity(frame)
        if derived.empty:
            continue
        games += len({row.game_id for row in plays})
        stats = (
            session.query(PlayerGameStat)
            .filter(PlayerGameStat.game_id.in_(chunk), PlayerGameStat.provider == PROVIDER)
            .all()
        )
        index = {(row.game_id, row.player_id): row for row in stats}
        for rec in derived.itertuples(index=False):
            row = index.get((rec.game_id, rec.player_id))
            if row is None:
                continue
            row.rz_targets = int(rec.rz_targets)
            row.rz_carries = int(rec.rz_carries)
            row.gl_carries = int(rec.gl_carries)
            if row.quality_status == "partial_no_charting":
                row.quality_status = "pbp_opportunities"
            updated += 1
    session.flush()
    return {"status": "ok", "games_with_plays": games, "players_updated": updated}


def _clock_seconds(value: Any) -> int:
    if is_missing(value):
        return 0
    text = str(value)
    if ":" in text:
        parts = text.split(":")
        try:
            return int(parts[0]) * 60 + int(float(parts[1]))
        except (TypeError, ValueError):
            return 0
    return as_int(value, 0) or 0
