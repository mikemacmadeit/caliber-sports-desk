"""Load frames from the DB, compute analytics, store rows. Never writes fabricated metrics."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.analytics.definitions import ANALYTICS_SOURCE, FEATURE_VERSION, TEAM_METRICS, UNAVAILABLE
from spe.analytics.engine import (
    add_usage_trends,
    apply_opponent_adjustments,
    apply_player_opponent_adjustments,
    attach_game_context,
    player_game_metrics,
    season_from_games,
    team_game_metrics,
    yards_allowed_by_position,
)
from spe.analytics.snapshots import build_pregame_snapshots
from spe.db.base import new_id
from spe.db.models import (
    FeatureRow,
    Game,
    Player,
    PlayerAnalytics,
    PlayerGameStat,
    Roster,
    TeamAnalytics,
    TeamGameStat,
)
from spe.ingest.store import upsert_coverage


def compute_and_store(session: Session, league_id: str, *, include_snapshots: bool = True) -> dict[str, Any]:
    games, plays, stats, players = load_frames(session, league_id)
    if games.empty:
        return {"league_id": league_id, "status": "no_games", "team_rows": 0, "player_rows": 0}
    plays_x = attach_game_context(plays, games) if not plays.empty else plays
    team_games = pd.DataFrame()
    if not plays_x.empty:
        team_games = apply_opponent_adjustments(team_game_metrics(plays_x), league_id, leave_one_out=True)

    _replace_analytics(session, TeamAnalytics, league_id)
    _replace_analytics(session, PlayerAnalytics, league_id)

    if not team_games.empty:
        pos_allowed = yards_allowed_by_position(plays_x, players) if not plays_x.empty else pd.DataFrame()
        if not pos_allowed.empty:
            pos_map: dict[tuple[str, str], dict[str, Any]] = {}
            for rec in pos_allowed.to_dict("records"):
                pos_map.setdefault((str(rec["game_id"]), str(rec["team_id"])), {})[str(rec["position_group"])] = {
                    "rec_yards_allowed": rec.get("rec_yards_allowed"),
                    "targets_identified": rec.get("targets_identified"),
                }
            team_games = team_games.copy()
            team_games["rec_yards_allowed_by_position"] = [
                pos_map.get((str(r.game_id), str(r.team_id))) for r in team_games.itertuples(index=False)
            ]
    else:
        pos_allowed = pd.DataFrame()
    team_n = _store_team_analytics(session, team_games, league_id)
    season_n = 0
    if not team_games.empty:
        for split in ("all", "home", "away"):
            for grain in ("season", "week"):
                season = season_from_games(team_games, split=split, grain=grain)
                season_n += _store_team_season(session, season, league_id)
        _update_team_game_stat(session, team_games)

    if not stats.empty:
        stats = player_game_metrics(stats, plays=plays_x if not plays_x.empty else None, players=players)
        stats = add_usage_trends(stats)
        if not team_games.empty:
            stats = apply_player_opponent_adjustments(stats, team_games, league_id)
    player_n = _store_player_analytics(session, stats, league_id) if not stats.empty else 0

    snap_team = snap_player = 0
    if include_snapshots:
        _delete_features(session, league_id, games["game_id"].tolist())
        trows, prows = build_pregame_snapshots(
            games=games, plays=plays_x, player_stats=stats, players=players, league_id=league_id
        )
        snap_team = _store_features(session, trows)
        snap_player = _store_features(session, prows)

    upsert_coverage(
        session,
        provider=ANALYTICS_SOURCE,
        dataset="team_analytics",
        league_id=league_id,
        season_year=0,
        row_count=team_n + season_n,
        status="available" if team_n else "empty",
        notes=f"game={team_n} season={season_n}. Premium charting excluded: {', '.join(UNAVAILABLE[:3])}.",
    )
    upsert_coverage(
        session,
        provider=ANALYTICS_SOURCE,
        dataset="player_analytics",
        league_id=league_id,
        season_year=0,
        row_count=player_n,
        status="available" if player_n else "empty",
        notes="RZ/GL from PBP when player IDs exist. Route participation not stored.",
    )
    upsert_coverage(
        session,
        provider=ANALYTICS_SOURCE,
        dataset="pregame_features",
        league_id=league_id,
        season_year=0,
        row_count=snap_team + snap_player,
        status="available" if snap_team else "empty",
        notes=f"{FEATURE_VERSION}. Kickoff-time snapshots only. Future games excluded.",
    )
    session.flush()
    usable_teams = int(team_games["team_id"].nunique()) if not team_games.empty else 0
    usable_players = int(stats["player_id"].nunique()) if not stats.empty else 0
    return {
        "league_id": league_id,
        "status": "ok",
        "games": int(len(games)),
        "plays": int(len(plays)),
        "team_game_rows": team_n,
        "team_season_rows": season_n,
        "player_game_rows": player_n,
        "pregame_team_snapshots": snap_team,
        "pregame_player_snapshots": snap_player,
        "teams_with_analytics": usable_teams,
        "players_with_analytics": usable_players,
        "position_defense_rows": int(len(pos_allowed)),
        "metrics": list(TEAM_METRICS),
        "unavailable": list(UNAVAILABLE),
    }


def load_frames(session: Session, league_id: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    # Temporal integrity: kickoff alone is insufficient. Only completed finals enter training frames.
    games = (
        session.query(Game)
        .filter(Game.league_id == league_id, Game.data_tier == "historical", Game.status == "final")
        .all()
    )
    gdf = pd.DataFrame(
        [
            {
                "game_id": g.game_id,
                "home_team_id": g.home_team_id,
                "away_team_id": g.away_team_id,
                "kickoff_utc": g.kickoff_utc,
                "week": g.week or 0,
                "season_year": int(g.season_id.rsplit("-", 1)[-1]) if g.season_id else 0,
                "league_id": g.league_id,
                "neutral_site": g.neutral_site,
                "status": g.status,
            }
            for g in games
        ]
    )
    if gdf.empty:
        return gdf, pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    conn = session.connection()
    pdf = pd.read_sql(
        text(
            """
            SELECT p.game_id, p.play_id, p.game_id AS provider_game_id,
                   p.offense_id, p.defense_id, p.offense_id AS offense_abbr, p.defense_id AS defense_abbr,
                   p.down, p.distance, p.yardline, p.play_type, p.yards,
                   COALESCE(p.epa, p.ppa) AS epa, p.ppa, p.wp, p.success,
                   p.is_pass, p.is_rush, p.is_td, p.is_turnover, p.is_red_zone, p.is_goal_line,
                   p.is_scrimmage, p.first_down, p.game_seconds_remaining, p.period,
                   p.score_before_off AS score_off, p.score_before_def AS score_def,
                   COALESCE(p.drive_id, '') AS drive_id,
                   p.passer_player_id, p.rusher_player_id, p.receiver_player_id,
                   p.is_pass AS pass_attempt, '' AS drive_result
            FROM plays p
            JOIN games g ON g.game_id = p.game_id
            WHERE g.league_id = :lg AND g.data_tier = 'historical' AND g.status = 'final'
            """
        ),
        conn,
        params={"lg": league_id},
    )
    if not pdf.empty:
        pdf["complete_pass"] = pdf["is_pass"].astype(bool) & pdf["receiver_player_id"].notna() & (pdf["yards"].fillna(0) > 0)
        for col in ("is_pass", "is_rush", "is_td", "is_turnover", "is_red_zone", "is_goal_line", "is_scrimmage", "first_down", "success"):
            if col in pdf.columns:
                pdf[col] = pdf[col].fillna(0).astype(bool)
    stats_rows = (
        session.query(PlayerGameStat)
        .join(Game, PlayerGameStat.game_id == Game.game_id)
        .filter(Game.league_id == league_id, Game.data_tier == "historical", Game.status == "final")
        .all()
    )
    rosters = session.query(Roster.player_id, Roster.team_id).all()
    roster_df = pd.DataFrame(rosters, columns=["player_id", "team_id"]) if rosters else pd.DataFrame(columns=["player_id", "team_id"])
    sdf = pd.DataFrame(
        [
            {
                "game_id": s.game_id,
                "player_id": s.player_id,
                "attempts": s.attempts,
                "targets": s.targets,
                "receptions": s.receptions,
                "rec_yards": s.rec_yards,
                "rush_yards": s.rush_yards,
                "carries": s.carries,
                "rush_td": s.rush_td,
                "rec_td": s.rec_td,
                "pass_td": s.pass_td,
                "rz_targets": s.rz_targets,
                "rz_carries": s.rz_carries,
                "gl_carries": s.gl_carries,
                "team_id": getattr(s, "team_id", None),
            }
            for s in stats_rows
        ]
    )
    if not sdf.empty and not gdf.empty:
        sdf = sdf.merge(gdf[["game_id", "kickoff_utc", "week", "season_year", "home_team_id", "away_team_id", "league_id"]], on="game_id", how="left")
        missing_team = sdf["team_id"].isna() if "team_id" in sdf.columns else pd.Series(True, index=sdf.index)
        if missing_team.any() and not roster_df.empty:
            extra = sdf.loc[missing_team].drop(columns=["team_id"], errors="ignore").merge(roster_df, on="player_id", how="left")
            home_match = extra["team_id"] == extra["home_team_id"]
            away_match = extra["team_id"] == extra["away_team_id"]
            extra = extra[home_match | away_match | extra["team_id"].isna()].drop_duplicates(["game_id", "player_id"])
            sdf = pd.concat([sdf.loc[~missing_team], extra], ignore_index=True)
        if "team_id" in sdf.columns:
            home_match = sdf["team_id"] == sdf["home_team_id"]
            away_match = sdf["team_id"] == sdf["away_team_id"]
            sdf.loc[~(home_match | away_match) & sdf["team_id"].notna(), "team_id"] = np.nan
        sdf = sdf.drop_duplicates(["game_id", "player_id"])
    players = session.query(Player).all()
    pldf = pd.DataFrame(
        [{"player_id": p.player_id, "name": p.name, "position_group": p.position_group, "position": p.position} for p in players]
    )
    return gdf, pdf, sdf, pldf


def _as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _store_team_analytics(session: Session, team_games: pd.DataFrame, league_id: str) -> int:
    if team_games.empty:
        return 0
    n = 0
    for rec in team_games.to_dict("records"):
        if rec.get("team_id") is None or (isinstance(rec.get("team_id"), float) and pd.isna(rec.get("team_id"))):
            continue
        metrics = _clean({k: rec.get(k) for k in TEAM_METRICS if k in rec or True})
        session.add(
            TeamAnalytics(
                id=new_id(),
                league_id=league_id,
                team_id=rec["team_id"],
                season_year=_as_int(rec.get("season_year")),
                week=_as_int(rec.get("week")),
                game_id=str(rec.get("game_id") or ""),
                grain="game",
                split=str(rec.get("split") or "all"),
                metrics=metrics,
                coverage={
                    "unavailable": list(UNAVAILABLE),
                    "n_plays": rec.get("off_plays"),
                    "rec_yards_allowed_by_position": rec.get("rec_yards_allowed_by_position"),
                },
                sample_size=_as_int(rec.get("off_plays") or rec.get("def_plays")),
                source=ANALYTICS_SOURCE,
            )
        )
        n += 1
        if n % 500 == 0:
            session.flush()
    session.flush()
    return n


def _store_team_season(session: Session, season: pd.DataFrame, league_id: str) -> int:
    if season.empty:
        return 0
    n = 0
    for rec in season.to_dict("records"):
        session.add(
            TeamAnalytics(
                id=new_id(),
                league_id=league_id,
                team_id=rec["team_id"],
                season_year=_as_int(rec.get("season_year")),
                week=_as_int(rec.get("week")),
                game_id="",
                grain=str(rec.get("grain") or "season"),
                split=str(rec.get("split") or "all"),
                metrics=_clean({k: rec.get(k) for k in TEAM_METRICS}),
                coverage={"unavailable": list(UNAVAILABLE)},
                sample_size=_as_int(rec.get("sample_size")),
                source=ANALYTICS_SOURCE,
            )
        )
        n += 1
    session.flush()
    return n


def _store_player_analytics(session: Session, stats: pd.DataFrame, league_id: str) -> int:
    n = 0
    skill = stats
    if "position_group" in stats.columns:
        skill = stats[stats["position_group"].isin(["WR", "TE", "RB", "QB"])]
    for rec in skill.to_dict("records"):
        session.add(
            PlayerAnalytics(
                id=new_id(),
                league_id=league_id,
                player_id=rec["player_id"],
                team_id=rec.get("team_id"),
                season_year=_as_int(rec.get("season_year")),
                week=_as_int(rec.get("week")),
                game_id=str(rec.get("game_id") or ""),
                grain="game",
                split="home" if rec.get("team_id") and rec.get("team_id") == rec.get("home_team_id") else "away",
                metrics=_clean(
                    {
                        "targets": rec.get("targets"),
                        "receptions": rec.get("receptions"),
                        "rec_yards": rec.get("rec_yards"),
                        "rush_yards": rec.get("rush_yards"),
                        "target_share": rec.get("target_share"),
                        "team_td_share": rec.get("team_td_share"),
                        "rz_targets": rec.get("rz_targets"),
                        "rz_carries": rec.get("rz_carries"),
                        "gl_carries": rec.get("gl_carries"),
                        "rec_yards_per_target": rec.get("rec_yards_per_target"),
                        "rush_yards_per_carry": rec.get("rush_yards_per_carry"),
                        "roll3_targets": rec.get("roll3_targets"),
                        "roll5_targets": rec.get("roll5_targets"),
                        "roll10_targets": rec.get("roll10_targets"),
                        "workload_delta_3v10": rec.get("workload_delta_3v10"),
                        "adj_rec_yards": rec.get("adj_rec_yards"),
                        "adj_rush_yards": rec.get("adj_rush_yards"),
                    }
                ),
                coverage={"rz_from_pbp": bool(rec.get("rz_from_pbp")), "unavailable": list(UNAVAILABLE)},
                sample_size=1,
                source=ANALYTICS_SOURCE,
            )
        )
        n += 1
        if n % 1000 == 0:
            session.flush()
    n += _store_player_season(session, skill, league_id)
    session.flush()
    return n


def _store_player_season(session: Session, skill: pd.DataFrame, league_id: str) -> int:
    if skill.empty:
        return 0
    df = skill.copy()
    if "home_team_id" in df.columns and "team_id" in df.columns:
        df["split_flag"] = np.where(df["team_id"] == df["home_team_id"], "home", "away")
    else:
        df["split_flag"] = "all"
    n = 0
    for split in ("all", "home", "away"):
        part = df if split == "all" else df[df["split_flag"] == split]
        if part.empty:
            continue
        grouped = part.groupby(["player_id", "season_year"], dropna=False)
        for (pid, year), g in grouped:
            games = max(len(g), 1)
            last_team = None
            if "team_id" in g.columns and g["team_id"].notna().any():
                last_team = g["team_id"].dropna().iloc[-1]
            session.add(
                PlayerAnalytics(
                    id=new_id(),
                    league_id=league_id,
                    player_id=pid,
                    team_id=last_team,
                    season_year=_as_int(year),
                    week=0,
                    game_id="",
                    grain="season",
                    split=split,
                    metrics=_clean(
                        {
                            "targets_per_game": float(g["targets"].sum()) / games if "targets" in g.columns else None,
                            "receptions_per_game": float(g["receptions"].sum()) / games if "receptions" in g.columns else None,
                            "rec_yards": float(g["rec_yards"].sum()) if "rec_yards" in g.columns else None,
                            "rush_yards": float(g["rush_yards"].sum()) if "rush_yards" in g.columns else None,
                            "target_share": float(np.nanmean(g["target_share"])) if "target_share" in g.columns else None,
                            "team_td_share": float(np.nanmean(g["team_td_share"])) if "team_td_share" in g.columns else None,
                            "rz_targets": float(g["rz_targets"].sum()) if "rz_targets" in g.columns else None,
                            "gl_carries": float(g["gl_carries"].sum()) if "gl_carries" in g.columns else None,
                            "adj_rec_yards_per_game": float(np.nanmean(g["adj_rec_yards"])) if "adj_rec_yards" in g.columns else None,
                            "adj_rush_yards_per_game": float(np.nanmean(g["adj_rush_yards"])) if "adj_rush_yards" in g.columns else None,
                        }
                    ),
                    coverage={"unavailable": list(UNAVAILABLE), "n_games": games},
                    sample_size=len(g),
                    source=ANALYTICS_SOURCE,
                )
            )
            n += 1
            if n % 500 == 0:
                session.flush()
    return n


def _store_features(session: Session, rows: list[dict[str, Any]]) -> int:
    n = 0
    for row in rows:
        session.add(
            FeatureRow(
                game_id=row["game_id"],
                player_id=row["player_id"],
                feature_version=row["feature_version"],
                as_of=row["as_of"],
                feature_blob=_clean(row["feature_blob"]),
                coverage_mask=row["coverage_mask"] or {},
                sample_size=row["sample_size"],
                missingness=row["missingness"],
                source=row["source"],
            )
        )
        n += 1
        if n % 1000 == 0:
            session.flush()
    session.flush()
    return n


def _update_team_game_stat(session: Session, team_games: pd.DataFrame) -> None:
    existing = {(r.game_id, r.team_id) for r in session.query(TeamGameStat.game_id, TeamGameStat.team_id).all()}
    now = datetime.now(UTC)
    for rec in team_games.to_dict("records"):
        key = (rec["game_id"], rec["team_id"])
        if key in existing:
            continue
        session.add(
            TeamGameStat(
                game_id=rec["game_id"],
                team_id=rec["team_id"],
                epa_per_play=rec.get("off_epa_per_play"),
                success_rate=rec.get("off_success_rate"),
                pace_sec_per_play=rec.get("pace_sec_per_play"),
                explosiveness=rec.get("explosive_play_rate"),
                red_zone_td_rate=rec.get("red_zone_td_rate"),
                provider=ANALYTICS_SOURCE,
                provider_record_id=f"{rec['game_id']}:{rec['team_id']}"[:160],
                observed_at=now,
                effective_at=now,
                quality_status="ok",
            )
        )
        existing.add(key)


def _replace_analytics(session: Session, model, league_id: str) -> None:
    session.query(model).filter(
        model.league_id == league_id,
        model.source == ANALYTICS_SOURCE,
    ).delete(synchronize_session=False)
    session.flush()


def _delete_features(session: Session, league_id: str, game_ids: list[str]) -> None:
    if not game_ids:
        return
    session.query(FeatureRow).filter(FeatureRow.game_id.in_(game_ids), FeatureRow.feature_version == FEATURE_VERSION).delete(
        synchronize_session=False
    )
    session.flush()


def _clean(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        if np.isnan(obj) or np.isinf(obj):
            return None
        return float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if obj is pd.NA:
        return None
    return obj
