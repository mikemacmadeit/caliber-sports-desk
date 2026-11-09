"""nflverse participation, Next Gen Stats summaries, weekly rosters, and depth charts.

Play-level route and coverage labels stay on the play. They are not assigned to a player.
Weekly roster and depth-chart files have no publication timestamp. Their effective time
is the last kickoff of that week plus six hours, so the same week's chart cannot leak
into that week's pregame features.
Week 0 on Next Gen Stats is a season aggregate and is not a pregame feature.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pandas as pd
from sqlalchemy.orm import Session

from spe.db.base import as_utc, utcnow
from spe.db.models import (
    DepthChartWeek,
    Game,
    NgsWeek,
    ParticipationPlay,
    ParticipationPlayerGame,
    Player,
    RosterWeek,
    Team,
)
from spe.ingest.canonical import nfl_player_id, nfl_season_id, nfl_team_id, position_group
from spe.ingest.nflverse_historical import PROVIDER, load_asset
from spe.ingest.store import ensure_nfl_team, finish_run, map_provider, start_run, upsert_coverage
from spe.ingest.values import as_float, as_int, as_str

WEEK_LAG = timedelta(hours=6)
NGS_FILES = {
    "passing": "ngs_passing",
    "rushing": "ngs_rushing",
    "receiving": "ngs_receiving",
}
NGS_METRICS = {
    "passing": (
        "avg_time_to_throw",
        "avg_completed_air_yards",
        "avg_intended_air_yards",
        "avg_air_yards_differential",
        "aggressiveness",
        "avg_air_yards_to_sticks",
        "attempts",
        "pass_yards",
        "pass_touchdowns",
        "interceptions",
        "passer_rating",
        "completions",
        "completion_percentage",
        "expected_completion_percentage",
        "completion_percentage_above_expectation",
        "avg_air_distance",
        "max_air_distance",
    ),
    "rushing": (
        "efficiency",
        "percent_attempts_gte_eight_defenders",
        "avg_time_to_los",
        "rush_attempts",
        "rush_yards",
        "avg_rush_yards",
        "rush_touchdowns",
        "expected_rush_yards",
        "rush_yards_over_expected",
        "rush_yards_over_expected_per_att",
        "rush_pct_over_expected",
    ),
    "receiving": (
        "avg_cushion",
        "avg_separation",
        "avg_intended_air_yards",
        "percent_share_of_intended_air_yards",
        "receptions",
        "targets",
        "catch_percentage",
        "yards",
        "rec_touchdowns",
        "avg_yac",
        "avg_expected_yac",
        "avg_yac_above_expectation",
    ),
}


def week_available_at(last_kickoff: datetime) -> datetime:
    return as_utc(last_kickoff) + WEEK_LAG


def game_available_at(kickoff: datetime) -> datetime:
    return as_utc(kickoff) + timedelta(hours=4)


def nfl_week_ends(session: Session) -> dict[tuple[int, int], datetime]:
    ends: dict[tuple[int, int], datetime] = {}
    games = (
        session.query(Game)
        .filter(Game.league_id == "nfl", Game.data_tier == "historical", Game.week.isnot(None))
        .all()
    )
    for game in games:
        if not str(game.season_id).startswith("nfl-"):
            continue
        year = int(str(game.season_id).split("-", 1)[1])
        key = (year, int(game.week))
        kick = as_utc(game.kickoff_utc)
        if key not in ends or kick > ends[key]:
            ends[key] = kick
    return ends


def season_ends(session: Session) -> dict[int, datetime]:
    ends: dict[int, datetime] = {}
    for (year, _week), kick in nfl_week_ends(session).items():
        if year not in ends or kick > ends[year]:
            ends[year] = kick
    return ends


def _known_players(session: Session) -> set[str]:
    return {row[0] for row in session.query(Player.player_id)}


def _ensure_player(
    session: Session,
    known: set[str],
    gsis: str | None,
    name: str | None,
    position: str | None,
) -> str | None:
    if not gsis or not name:
        return None
    pid = nfl_player_id(gsis)
    if pid in known:
        return pid
    pos = (position or "UNK")[:16]
    session.add(
        Player(
            player_id=pid,
            name=name[:128],
            position_group=position_group(pos),
            position=pos,
            aliases=[],
        )
    )
    map_provider(session, PROVIDER, "player", gsis, pid)
    known.add(pid)
    return pid


def _blank(value: Any) -> str | None:
    text = as_str(value)
    return text or None


def _boolish(value: Any) -> bool | None:
    if value is None or (isinstance(value, float) and value != value):
        return None
    if isinstance(value, str) and value.strip().lower() in {"", "nan", "none"}:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "t", "yes"}:
        return True
    if text in {"false", "0", "f", "no"}:
        return False
    return None


def _insert_chunks(session: Session, model, rows: list[dict[str, Any]], size: int = 800) -> int:
    session.flush()
    if not rows:
        return 0
    written = 0
    for start in range(0, len(rows), size):
        session.bulk_insert_mappings(model, rows[start : start + size], render_nulls=True)
        written += len(rows[start : start + size])
        session.flush()
    return written


def _delete_games(session: Session, model, game_ids: list[str]) -> None:
    for start in range(0, len(game_ids), 400):
        chunk = game_ids[start : start + 400]
        session.query(model).filter(model.game_id.in_(chunk), model.provider == PROVIDER).delete(
            synchronize_session=False
        )
    session.flush()


def _split_ids(value: Any) -> list[str]:
    text = as_str(value) or ""
    return [part.strip() for part in text.split(";") if part.strip().startswith("00-")]


def ingest_participation_frame(
    session: Session,
    df: pd.DataFrame,
    *,
    year: int,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    observed = observed_at or datetime.now(UTC)
    season_games = {
        game.game_id: game
        for game in session.query(Game).filter(Game.season_id == nfl_season_id(year), Game.league_id == "nfl")
    }
    by_external = {game.game_id.split(":", 1)[-1]: game for game in season_games.values()}
    run = start_run(session, PROVIDER, "participation", "nfl", year)
    if df is None or df.empty or not by_external:
        finish_run(session, run, status="empty", notes="No participation rows or no games for this season.")
        upsert_coverage(
            session,
            provider=PROVIDER,
            dataset="participation",
            league_id="nfl",
            season_year=year,
            row_count=0,
            status="empty" if by_external else "no_games",
            notes="Participation file was empty or this season has no stored games.",
        )
        return {"plays": 0, "player_games": 0, "unresolved_players": 0}

    known = _known_players(session)
    play_rows: list[dict[str, Any]] = []
    snap_counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    offense_plays: dict[str, int] = defaultdict(int)
    defense_plays: dict[str, int] = defaultdict(int)
    unresolved = 0
    skipped = 0
    seen_plays: set[tuple[str, str]] = set()
    matched_game_ids: set[str] = set()

    for raw in df.itertuples(index=False):
        external = as_str(getattr(raw, "nflverse_game_id", None))
        game = by_external.get(external or "")
        play_raw = getattr(raw, "play_id", None)
        if game is None or play_raw is None or (isinstance(play_raw, float) and play_raw != play_raw):
            skipped += 1
            continue
        play_id = str(int(play_raw)) if isinstance(play_raw, float) else str(play_raw)
        key = (game.game_id, play_id)
        if key in seen_plays:
            skipped += 1
            continue
        seen_plays.add(key)
        matched_game_ids.add(game.game_id)
        formation = _blank(getattr(raw, "offense_formation", None))
        route = _blank(getattr(raw, "route", None))
        man_zone = _blank(getattr(raw, "defense_man_zone_type", None))
        coverage = _blank(getattr(raw, "defense_coverage_type", None))
        offense_ids = _split_ids(getattr(raw, "offense_players", None))
        defense_ids = _split_ids(getattr(raw, "defense_players", None))
        if offense_ids:
            offense_plays[game.game_id] += 1
        if defense_ids:
            defense_plays[game.game_id] += 1
        available = game_available_at(game.kickoff_utc)
        play_rows.append(
            {
                "id": str(uuid4()),
                "game_id": game.game_id,
                "play_id": play_id[:32],
                "possession_team": (_blank(getattr(raw, "possession_team", None)) or "")[:8] or None,
                "offense_formation": formation[:32] if formation else None,
                "offense_personnel": _blank(getattr(raw, "offense_personnel", None)),
                "defense_personnel": _blank(getattr(raw, "defense_personnel", None)),
                "defenders_in_box": as_float(getattr(raw, "defenders_in_box", None)),
                "number_of_pass_rushers": as_float(getattr(raw, "number_of_pass_rushers", None)),
                "n_offense": as_int(getattr(raw, "n_offense", None), None),
                "n_defense": as_int(getattr(raw, "n_defense", None), None),
                "route": route[:64] if route else None,
                "defense_man_zone_type": man_zone[:32] if man_zone else None,
                "defense_coverage_type": coverage[:32] if coverage else None,
                "time_to_throw": as_float(getattr(raw, "time_to_throw", None)),
                "was_pressure": _boolish(getattr(raw, "was_pressure", None)),
                "ngs_air_yards": as_float(getattr(raw, "ngs_air_yards", None)),
                "offense_players": _blank(getattr(raw, "offense_players", None)),
                "defense_players": _blank(getattr(raw, "defense_players", None)),
                "provider": PROVIDER,
                "provider_record_id": f"{game.game_id}:{play_id}"[:160],
                "observed_at": observed,
                "effective_at": available,
                "ingested_at": observed,
                "revision": 1,
                "quality_status": "postgame_play",
            }
        )
        for side, id_attr, name_attr, pos_attr, slot in (
            ("offense", "offense_players", "offense_names", "offense_positions", 0),
            ("defense", "defense_players", "defense_names", "defense_positions", 1),
        ):
            ids = offense_ids if side == "offense" else defense_ids
            names = [part.strip() for part in str(getattr(raw, name_attr, "") or "").split(";")]
            positions = [part.strip() for part in str(getattr(raw, pos_attr, "") or "").split(";")]
            aligned = len(names) == len(ids)
            for index, gsis in enumerate(ids):
                name = names[index] if aligned else None
                pos = positions[index] if aligned and index < len(positions) else None
                pid = _ensure_player(session, known, gsis, name, pos) if name else (
                    nfl_player_id(gsis) if nfl_player_id(gsis) in known else None
                )
                if pid is None:
                    unresolved += 1
                    continue
                snap_counts[(game.game_id, pid)][slot] += 1

    _delete_games(session, ParticipationPlay, list(matched_game_ids))
    _delete_games(session, ParticipationPlayerGame, list(matched_game_ids))
    plays = _insert_chunks(session, ParticipationPlay, play_rows)
    player_rows = []
    for (game_id, pid), (off_snaps, def_snaps) in snap_counts.items():
        player_rows.append(
            {
                "id": str(uuid4()),
                "game_id": game_id,
                "player_id": pid,
                "offense_snaps": off_snaps,
                "defense_snaps": def_snaps,
                "offense_plays": offense_plays.get(game_id, 0),
                "defense_plays": defense_plays.get(game_id, 0),
                "provider": PROVIDER,
                "provider_record_id": f"{game_id}:{pid}"[:160],
                "observed_at": observed,
                "effective_at": game_available_at(season_games[game_id].kickoff_utc),
                "ingested_at": observed,
                "revision": 1,
                "quality_status": "derived_from_player_lists",
            }
        )
    player_games = _insert_chunks(session, ParticipationPlayerGame, player_rows)
    finish_run(
        session,
        run,
        status="ok",
        upserted=plays,
        skipped=skipped,
        notes="Route and coverage stay on the play. Player rows are snap counts only.",
    )
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="participation",
        league_id="nfl",
        season_year=year,
        row_count=plays,
        status="available" if plays else "empty",
        notes=(
            f"plays={plays} player_games={player_games} unresolved_player_tokens={unresolved}. "
            "Play-level route is not a player route. effective_at is kickoff plus 4 hours."
        ),
    )
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="participation_player_games",
        league_id="nfl",
        season_year=year,
        row_count=player_games,
        status="available" if player_games else "empty",
        notes="Offense and defense snaps counted from semicolon-separated GSIS ids.",
    )
    session.flush()
    return {"plays": plays, "player_games": player_games, "unresolved_players": unresolved, "skipped": skipped}


def ingest_ngs_frame(
    session: Session,
    frames: dict[str, pd.DataFrame],
    *,
    seasons: list[int],
    observed_at: datetime | None = None,
) -> dict[str, int]:
    observed = observed_at or datetime.now(UTC)
    known = _known_players(session)
    ends = nfl_week_ends(session)
    seasons_end = season_ends(session)
    wanted = {int(year) for year in seasons}
    rows: list[dict[str, Any]] = []
    skipped = 0
    for stat_type, df in frames.items():
        if df is None or df.empty:
            continue
        metrics = NGS_METRICS.get(stat_type, ())
        for raw in df.itertuples(index=False):
            year = as_int(getattr(raw, "season", None), None)
            week = as_int(getattr(raw, "week", None), None)
            if year is None or week is None or year not in wanted:
                continue
            gsis = as_str(getattr(raw, "player_gsis_id", None))
            name = as_str(getattr(raw, "player_display_name", None))
            pos = as_str(getattr(raw, "player_position", None))
            pid = _ensure_player(session, known, gsis, name, pos)
            if pid is None:
                skipped += 1
                continue
            season_type = (as_str(getattr(raw, "season_type", None)) or "REG")[:8]
            if week == 0:
                end = seasons_end.get(year)
                quality = "season_aggregate_not_pregame"
            else:
                end = ends.get((year, week))
                quality = "postgame_week"
            if end is None:
                skipped += 1
                continue
            blob = {}
            for key in metrics:
                value = as_float(getattr(raw, key, None))
                if value is not None:
                    blob[key] = value
            team = as_str(getattr(raw, "team_abbr", None))
            rows.append(
                {
                    "id": str(uuid4()),
                    "season_year": year,
                    "week": week,
                    "season_type": season_type,
                    "player_id": pid,
                    "team_id": nfl_team_id(team) if team else None,
                    "stat_type": stat_type[:16],
                    "metrics": blob,
                    "provider": PROVIDER,
                    "provider_record_id": f"{stat_type}:{year}:{season_type}:{week}:{pid}"[:160],
                    "observed_at": observed,
                    "effective_at": week_available_at(end) if week else week_available_at(end),
                    "ingested_at": observed,
                    "revision": 1,
                    "quality_status": quality,
                }
            )
    for year in wanted:
        session.query(NgsWeek).filter(NgsWeek.season_year == year, NgsWeek.provider == PROVIDER).delete(
            synchronize_session=False
        )
    session.flush()
    written = _insert_chunks(session, NgsWeek, rows)
    by_year: dict[int, int] = defaultdict(int)
    for row in rows:
        by_year[int(row["season_year"])] += 1
    for year in wanted:
        upsert_coverage(
            session,
            provider=PROVIDER,
            dataset="ngs_summary",
            league_id="nfl",
            season_year=year,
            row_count=by_year.get(year, 0),
            status="available" if by_year.get(year) else "empty",
            notes="Weekly passing, rushing, and receiving summaries. Week 0 is a season aggregate and is not used pregame. Not tracking coordinates.",
        )
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="ngs_summary",
        league_id="fbs",
        season_year=0,
        row_count=0,
        status="not_in_source",
        notes="nflverse Next Gen Stats summaries are NFL-only.",
    )
    session.flush()
    return {"rows": written, "skipped": skipped}


def ingest_roster_frame(session: Session, df: pd.DataFrame, *, year: int, observed_at: datetime | None = None) -> dict[str, int]:
    return _ingest_week_table(session, df, year=year, kind="roster", observed_at=observed_at)


def ingest_depth_frame(session: Session, df: pd.DataFrame, *, year: int, observed_at: datetime | None = None) -> dict[str, int]:
    if df is not None and not df.empty and "dt" in df.columns and "week" not in df.columns:
        return _ingest_dated_depth(session, df, year=year, observed_at=observed_at)
    return _ingest_week_table(session, df, year=year, kind="depth", observed_at=observed_at)


def _parse_chart_time(value: Any) -> datetime | None:
    text = as_str(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _ingest_dated_depth(
    session: Session,
    df: pd.DataFrame,
    *,
    year: int,
    observed_at: datetime | None,
) -> dict[str, int]:
    """2025+ depth charts are timestamped snapshots, not week numbers."""
    observed = observed_at or datetime.now(UTC)
    known = _known_players(session)
    known_teams = {row[0] for row in session.query(Team.team_id)}
    run = start_run(session, PROVIDER, "depth", "nfl", year)
    session.query(DepthChartWeek).filter(DepthChartWeek.season_year == year, DepthChartWeek.provider == PROVIDER).delete(
        synchronize_session=False
    )
    session.flush()
    seen: set[tuple] = set()
    batch: list[dict[str, Any]] = []
    written = skipped = 0

    def flush_batch() -> None:
        nonlocal written
        if not batch:
            return
        written += _insert_chunks(session, DepthChartWeek, batch)
        batch.clear()

    for raw in df.itertuples(index=False):
        stamp = _parse_chart_time(getattr(raw, "dt", None))
        gsis = as_str(getattr(raw, "gsis_id", None))
        name = as_str(getattr(raw, "player_name", None))
        team = as_str(getattr(raw, "team", None))
        if stamp is None or not team:
            skipped += 1
            continue
        pid = _ensure_player(session, known, gsis, name, as_str(getattr(raw, "pos_abb", None)))
        if pid is None:
            skipped += 1
            continue
        team_id = nfl_team_id(team)
        if team_id not in known_teams:
            ensure_nfl_team(session, team)
            known_teams.add(team_id)
        formation = (_blank(getattr(raw, "pos_grp", None)) or "")[:32]
        abb = _blank(getattr(raw, "pos_abb", None)) or "UNK"
        slot = as_str(getattr(raw, "pos_slot", None)) or ""
        depth_position = f"{abb}:{slot}"[:16]
        identity = (stamp.strftime("%Y%m%d%H%M%S"), team_id, pid, formation, depth_position)
        if identity in seen:
            skipped += 1
            continue
        seen.add(identity)
        batch.append(
            {
                "id": str(uuid4()),
                "season_year": year,
                "week": int(stamp.strftime("%Y%m%d")),
                "game_type": stamp.strftime("%H%M%S"),
                "team_id": nfl_team_id(team),
                "player_id": pid,
                "formation": formation,
                "position": abb[:16],
                "depth_position": depth_position,
                "depth_rank": as_int(getattr(raw, "pos_rank", None), None),
                "provider": PROVIDER,
                "provider_record_id": f"depth:{identity[0]}:{team}:{pid}:{formation}:{depth_position}"[:160],
                "observed_at": observed,
                "effective_at": stamp,
                "ingested_at": observed,
                "revision": 1,
                "quality_status": "snapshot_timestamp",
            }
        )
        if len(batch) >= 5000:
            flush_batch()
    flush_batch()
    note = (
        "Rows use the file's dt timestamp as effective_at. "
        "week stores YYYYMMDD and game_type stores HHMMSS so snapshots do not collapse. "
        "This is the chart time, not a week-end estimate."
    )
    finish_run(session, run, status="ok" if written else "empty", upserted=written, skipped=skipped, notes=note)
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="depth_charts",
        league_id="nfl",
        season_year=year,
        row_count=written,
        status="available" if written else "empty",
        notes=note,
    )
    session.flush()
    return {"rows": written, "skipped": skipped}


def _ingest_week_table(
    session: Session,
    df: pd.DataFrame,
    *,
    year: int,
    kind: str,
    observed_at: datetime | None,
) -> dict[str, int]:
    observed = observed_at or datetime.now(UTC)
    ends = nfl_week_ends(session)
    known = _known_players(session)
    run = start_run(session, PROVIDER, kind, "nfl", year)
    rows: list[dict[str, Any]] = []
    skipped = 0
    seen: set[tuple] = set()
    if df is not None and not df.empty:
        for raw in df.itertuples(index=False):
            season = as_int(getattr(raw, "season", None), year) or year
            week = as_int(getattr(raw, "week", None), None)
            if season != year or week is None:
                skipped += 1
                continue
            end = ends.get((year, week))
            if end is None:
                skipped += 1
                continue
            if kind == "roster":
                gsis = as_str(getattr(raw, "gsis_id", None))
                name = as_str(getattr(raw, "full_name", None))
                team = as_str(getattr(raw, "team", None))
                pos = as_str(getattr(raw, "position", None))
            else:
                gsis = as_str(getattr(raw, "gsis_id", None))
                name = as_str(getattr(raw, "full_name", None))
                team = as_str(getattr(raw, "club_code", None))
                pos = as_str(getattr(raw, "position", None))
            if not team:
                skipped += 1
                continue
            pid = _ensure_player(session, known, gsis, name, pos)
            if pid is None:
                skipped += 1
                continue
            team_row = session.get(Team, nfl_team_id(team))
            if team_row is None:
                ensure_nfl_team(session, team)
            game_type = (as_str(getattr(raw, "game_type", None)) or "REG")[:8]
            available = week_available_at(end)
            if kind == "roster":
                identity = (year, week, game_type, nfl_team_id(team), pid)
                if identity in seen:
                    skipped += 1
                    continue
                seen.add(identity)
                jersey = as_str(getattr(raw, "jersey_number", None))
                rows.append(
                    {
                        "id": str(uuid4()),
                        "season_year": year,
                        "week": week,
                        "game_type": game_type,
                        "team_id": nfl_team_id(team),
                        "player_id": pid,
                        "position": pos[:16] if pos else None,
                        "depth_chart_position": (_blank(getattr(raw, "depth_chart_position", None)) or "")[:16] or None,
                        "status": (_blank(getattr(raw, "status", None)) or None),
                        "jersey": jersey[:8] if jersey else None,
                        "provider": PROVIDER,
                        "provider_record_id": f"roster:{year}:{week}:{game_type}:{team}:{pid}"[:160],
                        "observed_at": observed,
                        "effective_at": available,
                        "ingested_at": observed,
                        "revision": 1,
                        "quality_status": "available_after_week",
                    }
                )
            else:
                formation = _blank(getattr(raw, "formation", None)) or ""
                depth_position = _blank(getattr(raw, "depth_position", None)) or ""
                identity = (year, week, game_type, nfl_team_id(team), pid, formation, depth_position)
                if identity in seen:
                    skipped += 1
                    continue
                seen.add(identity)
                rows.append(
                    {
                        "id": str(uuid4()),
                        "season_year": year,
                        "week": week,
                        "game_type": game_type,
                        "team_id": nfl_team_id(team),
                        "player_id": pid,
                        "formation": formation[:32],
                        "position": pos[:16] if pos else None,
                        "depth_position": depth_position[:16],
                        "depth_rank": as_int(getattr(raw, "depth_team", None), None),
                        "provider": PROVIDER,
                        "provider_record_id": f"depth:{year}:{week}:{game_type}:{team}:{pid}:{formation}:{depth_position}"[:160],
                        "observed_at": observed,
                        "effective_at": available,
                        "ingested_at": observed,
                        "revision": 1,
                        "quality_status": "available_after_week",
                    }
                )
    model = RosterWeek if kind == "roster" else DepthChartWeek
    dataset = "roster_weekly" if kind == "roster" else "depth_charts"
    session.query(model).filter(model.season_year == year, model.provider == PROVIDER).delete(
        synchronize_session=False
    )
    session.flush()
    written = _insert_chunks(session, model, rows)
    note = (
        "Week grain only. effective_at is the last kickoff that week plus 6 hours because the file has no publication time. "
        "Same-week charts are excluded from that week's pregame features."
    )
    finish_run(session, run, status="ok" if written else "empty", upserted=written, skipped=skipped, notes=note)
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset=dataset,
        league_id="nfl",
        season_year=year,
        row_count=written,
        status="available" if written else "empty",
        notes=note,
    )
    session.flush()
    return {"rows": written, "skipped": skipped}


def ingest_nfl_extras(
    session: Session,
    *,
    seasons: list[int],
    refresh: bool = False,
) -> dict[str, Any]:
    summary: dict[str, Any] = {"participation": {}, "rosters": {}, "depth": {}, "ngs": {}}
    for year in seasons:
        try:
            roster = load_asset("roster_weekly", year, refresh=refresh)
            summary["rosters"][year] = ingest_roster_frame(session, roster, year=year)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            summary["rosters"][year] = {"error": str(exc)}
            upsert_coverage(
                session, provider=PROVIDER, dataset="roster_weekly", league_id="nfl",
                season_year=year, row_count=0, status="unavailable", notes=str(exc),
            )
        try:
            depth = load_asset("depth_charts", year, refresh=refresh)
            summary["depth"][year] = ingest_depth_frame(session, depth, year=year)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            summary["depth"][year] = {"error": str(exc)}
            upsert_coverage(
                session, provider=PROVIDER, dataset="depth_charts", league_id="nfl",
                season_year=year, row_count=0, status="unavailable", notes=str(exc),
            )
        try:
            part = load_asset("participation", year, refresh=refresh)
            summary["participation"][year] = ingest_participation_frame(session, part, year=year)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            summary["participation"][year] = {"error": str(exc)}
            upsert_coverage(
                session, provider=PROVIDER, dataset="participation", league_id="nfl",
                season_year=year, row_count=0, status="unavailable", notes=str(exc),
            )
        session.commit()
        print(f"nflverse extras {year}: roster={summary['rosters'].get(year)} depth={summary['depth'].get(year)} participation={summary['participation'].get(year)}", flush=True)
    frames: dict[str, pd.DataFrame] = {}
    for stat_type, key in NGS_FILES.items():
        try:
            frames[stat_type] = load_asset(key, refresh=refresh)
        except Exception as exc:  # noqa: BLE001
            frames[stat_type] = pd.DataFrame()
            summary.setdefault("ngs_errors", {})[stat_type] = str(exc)
    summary["ngs"] = ingest_ngs_frame(session, frames, seasons=seasons)
    for dataset in ("participation", "ngs_summary", "roster_weekly", "depth_charts"):
        upsert_coverage(
            session,
            provider=PROVIDER,
            dataset=dataset,
            league_id="fbs",
            season_year=0,
            row_count=0,
            status="not_in_source",
            notes="These nflverse releases are NFL-only. FBS is not backfilled from NFL rows.",
        )
    session.commit()
    summary["ingested_at"] = utcnow().isoformat()
    return summary
