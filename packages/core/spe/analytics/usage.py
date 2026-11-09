"""Pregame usage features from participation, depth charts, rosters, and NGS weeks.

Every value is taken from a row whose effective time is already in the past.
Same-game snaps and same-week charts are excluded. Week 0 NGS aggregates are excluded.
Missing inputs are omitted. They are not stored as zeros.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pandas as pd
from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.db.models import Game, PlayerAnalytics, TeamAnalytics
from spe.ingest.canonical import nfl_team_id
from spe.ingest.store import upsert_coverage

USAGE_SOURCE = "caliber_usage_v1"
_MAN = "MAN_COVERAGE"
_ZONE = "ZONE_COVERAGE"
_FORMATIONS = ("SHOTGUN", "UNDER CENTER", "PISTOL")


def _games(session: Session) -> pd.DataFrame:
    rows = []
    for game in session.query(Game).filter(Game.league_id == "nfl", Game.data_tier == "historical"):
        year = int(str(game.season_id).split("-", 1)[1]) if str(game.season_id).startswith("nfl-") else None
        rows.append(
            {
                "game_id": game.game_id,
                "season_year": year,
                "week": game.week,
                "kickoff_utc": as_utc(game.kickoff_utc),
                "home_team_id": game.home_team_id,
                "away_team_id": game.away_team_id,
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["kickoff_utc"] = pd.to_datetime(frame["kickoff_utc"], utc=True)
    return frame


def _prior_share(snaps: pd.DataFrame) -> pd.DataFrame:
    if snaps.empty:
        return snaps
    frame = snaps.sort_values(["player_id", "kickoff_utc"]).copy()
    grouped = frame.groupby("player_id", sort=False)["offense_snap_share"]
    frame["offense_snap_share_last"] = grouped.shift(1)
    frame["offense_snap_share_roll3"] = grouped.transform(lambda s: s.shift(1).rolling(3, min_periods=1).mean())
    frame["offense_snap_share_roll10"] = grouped.transform(lambda s: s.shift(1).rolling(10, min_periods=3).mean())
    prior_n = frame.groupby("player_id", sort=False).cumcount()
    frame.loc[prior_n < 1, ["offense_snap_share_last", "offense_snap_share_roll3"]] = pd.NA
    frame.loc[prior_n < 3, "offense_snap_share_roll10"] = pd.NA
    frame["offense_snap_share_delta_3v10"] = frame["offense_snap_share_roll3"] - frame["offense_snap_share_roll10"]
    return frame


def compute_usage(session: Session) -> dict[str, Any]:
    games = _games(session)
    if games.empty:
        return {"player_rows": 0, "team_rows": 0, "status": "no_games"}
    kickoff = games.set_index("game_id")["kickoff_utc"]

    snaps = _read(
        session,
        """
        SELECT game_id, player_id, offense_snaps, defense_snaps, offense_plays
        FROM participation_player_games
        WHERE provider = 'nflverse'
        """,
    )
    if not snaps.empty:
        snaps = snaps[snaps["game_id"].isin(set(kickoff.index))].copy()
        denom = snaps["offense_plays"].where(snaps["offense_plays"] > 0)
        snaps["offense_snap_share"] = snaps["offense_snaps"] / denom
        snaps["kickoff_utc"] = snaps["game_id"].map(kickoff)
    snaps = _prior_share(snaps)

    depth = _read(
        session,
        """
        SELECT player_id, effective_at, team_id AS depth_team_id,
               depth_rank AS depth_rank_prior_week,
               depth_position AS depth_position_prior_week,
               formation AS depth_formation_prior_week
        FROM depth_chart_weeks
        WHERE provider = 'nflverse'
        """,
    )
    if not depth.empty:
        depth["starter_prior_week"] = [
            None if pd.isna(value) else bool(int(value) == 1) for value in depth["depth_rank_prior_week"]
        ]
    roster = _read(
        session,
        """
        SELECT player_id, effective_at, status AS roster_status_prior_week,
               position AS roster_position_prior_week, team_id AS roster_team_id
        FROM roster_weeks
        WHERE provider = 'nflverse'
        """,
    )
    ngs_raw = _read(
        session,
        """
        SELECT player_id, effective_at, stat_type, metrics
        FROM ngs_weeks
        WHERE provider = 'nflverse' AND week > 0
          AND quality_status != 'season_aggregate_not_pregame'
        """,
    )
    ngs = _expand_metrics(ngs_raw)

    player_ids = set()
    if not snaps.empty:
        player_ids.update(snaps["player_id"])
    base = games.loc[games["game_id"].isin(set(snaps["game_id"]) if not snaps.empty else set())].copy()
    if snaps.empty:
        base = pd.DataFrame(columns=["game_id", "player_id", "kickoff_utc", "season_year", "week"])
    else:
        base = snaps.merge(games[["game_id", "season_year", "week"]], on="game_id", how="left")
    if not roster.empty:
        roster["effective_at"] = pd.to_datetime(roster["effective_at"], utc=True)
    if not depth.empty:
        depth["effective_at"] = pd.to_datetime(depth["effective_at"], utc=True)
    listed = _players_without_participation(games, snaps, roster, depth)
    if not listed.empty:
        listed = _completed_snap_history(listed, snaps)
        base = pd.concat([base, listed], ignore_index=True)

    def _asof(left: pd.DataFrame, right: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
        if left.empty or right.empty:
            for col in cols:
                left[col] = pd.NA
            return left
        right = right.sort_values("effective_at")
        left = left.sort_values("kickoff_utc")
        keep = ["player_id", "effective_at", *cols]
        merged = pd.merge_asof(
            left,
            right[keep],
            left_on="kickoff_utc",
            right_on="effective_at",
            by="player_id",
            direction="backward",
        )
        return merged.drop(columns=["effective_at"], errors="ignore")

    if not base.empty:
        if not depth.empty:
            depth["effective_at"] = pd.to_datetime(depth["effective_at"], utc=True)
            depth = depth.sort_values("depth_rank_prior_week", na_position="last").groupby(
                ["player_id", "effective_at"], as_index=False
            ).head(1)
        if not roster.empty:
            roster["effective_at"] = pd.to_datetime(roster["effective_at"], utc=True)
            roster = roster.sort_values("effective_at").groupby(["player_id", "effective_at"], as_index=False).tail(1)
        base = _asof(
            base,
            depth,
            ["depth_rank_prior_week", "depth_position_prior_week", "depth_formation_prior_week", "starter_prior_week"],
        )
        base = _asof(
            base,
            roster,
            ["roster_status_prior_week", "roster_position_prior_week", "roster_team_id"],
        )
        if not ngs.empty:
            ngs["effective_at"] = pd.to_datetime(ngs["effective_at"], utc=True)
            for stat_type, fields in (
                ("receiving", ["avg_separation", "avg_yac_above_expectation", "avg_cushion"]),
                ("passing", ["completion_percentage_above_expectation", "avg_time_to_throw", "aggressiveness"]),
                ("rushing", ["rush_yards_over_expected", "rush_yards_over_expected_per_att", "efficiency"]),
            ):
                part = ngs[ngs["stat_type"] == stat_type]
                present = [field for field in fields if field in part.columns]
                if part.empty or not present:
                    continue
                renamed = part.rename(columns={field: f"ngs_{field}" for field in present})
                base = _asof(base, renamed, [f"ngs_{field}" for field in present])

    session.query(PlayerAnalytics).filter(
        PlayerAnalytics.league_id == "nfl",
        PlayerAnalytics.source == USAGE_SOURCE,
    ).delete(synchronize_session=False)
    session.query(TeamAnalytics).filter(
        TeamAnalytics.league_id == "nfl",
        TeamAnalytics.source == USAGE_SOURCE,
    ).delete(synchronize_session=False)
    session.flush()

    player_rows = _player_rows(base)
    team_rows = _team_rows(session, games)
    if player_rows:
        session.execute(PlayerAnalytics.__table__.insert(), player_rows)
    if team_rows:
        session.execute(TeamAnalytics.__table__.insert(), team_rows)
    session.flush()
    upsert_coverage(
        session,
        provider=USAGE_SOURCE,
        dataset="usage_pregame",
        league_id="nfl",
        season_year=0,
        row_count=len(player_rows),
        status="available" if player_rows else "empty",
        notes=(
            f"player_rows={len(player_rows)} team_rows={len(team_rows)}. "
            "Prior games and prior weeks only. Week 0 NGS excluded. News is not an input."
        ),
    )
    upsert_coverage(
        session,
        provider=USAGE_SOURCE,
        dataset="usage_pregame",
        league_id="fbs",
        season_year=0,
        row_count=0,
        status="not_in_source",
        notes="Usage features are built from NFL-only participation, rosters, depth charts, and NGS.",
    )
    return {"player_rows": len(player_rows), "team_rows": len(team_rows), "status": "ok"}


def _clean_metric(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    if isinstance(value, float):
        return float(value)
    if hasattr(value, "item"):
        return value.item()
    return value


def _player_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    metric_cols = [
        col
        for col in frame.columns
        if col.startswith("offense_snap_share_")
        or col.startswith("depth_")
        or col.startswith("roster_")
        or col.startswith("starter_")
        or col.startswith("ngs_")
    ]
    now = datetime.now(UTC)
    rows = []
    for rec in frame.itertuples(index=False):
        metrics: dict[str, Any] = {}
        for col in metric_cols:
            value = _clean_metric(getattr(rec, col, None))
            if value is None or value == "":
                continue
            if col == "starter_prior_week":
                metrics[col] = bool(value)
            else:
                metrics[col] = value
        if "offense_snap_share_roll3" in metrics and "offense_snap_share_roll10" in metrics:
            metrics["offense_snap_share_delta_3v10"] = (
                metrics["offense_snap_share_roll3"] - metrics["offense_snap_share_roll10"]
            )
        stored = {key: value for key, value in metrics.items() if key != "roster_team_id"}
        if not stored:
            continue
        rows.append(
            {
                "id": str(uuid4()),
                "league_id": "nfl",
                "player_id": rec.player_id,
                "team_id": metrics.get("roster_team_id"),
                "season_year": int(rec.season_year or 0),
                "week": int(rec.week or 0),
                "game_id": rec.game_id,
                "grain": "usage_pregame",
                "split": "all",
                "metrics": stored,
                "coverage": {
                    "sources": sorted({_source_for(k) for k in metrics}),
                    "as_of": pd.Timestamp(rec.kickoff_utc).isoformat(),
                    "rule": "prior participation games and prior-week charts only",
                },
                "sample_size": 1,
                "source": USAGE_SOURCE,
            }
        )
    _ = now
    return rows


def _source_for(key: str) -> str:
    if key.startswith("ngs_"):
        return "ngs"
    if key.startswith("depth_") or key.startswith("starter_"):
        return "depth_chart"
    if key.startswith("roster_"):
        return "roster_weekly"
    return "participation"


def _players_without_participation(
    games: pd.DataFrame,
    snaps: pd.DataFrame,
    roster: pd.DataFrame,
    depth: pd.DataFrame,
) -> pd.DataFrame:
    """Players listed before kickoff for games that have no participation file yet."""
    columns = ["game_id", "player_id", "kickoff_utc", "season_year", "week"]
    played = set(snaps["game_id"]) if not snaps.empty else set()
    upcoming = games.loc[~games["game_id"].isin(played)]
    if upcoming.empty:
        return pd.DataFrame(columns=columns)
    sides = pd.concat(
        [
            upcoming.rename(columns={"home_team_id": "team_id"})[["game_id", "season_year", "week", "kickoff_utc", "team_id"]],
            upcoming.rename(columns={"away_team_id": "team_id"})[["game_id", "season_year", "week", "kickoff_utc", "team_id"]],
        ],
        ignore_index=True,
    )
    frames = [
        _names_on_latest_list(sides, roster, "roster_team_id"),
        _names_on_latest_list(sides, depth, "depth_team_id"),
    ]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True).drop_duplicates(["game_id", "player_id"])


def _names_on_latest_list(sides: pd.DataFrame, chart: pd.DataFrame, team_col: str) -> pd.DataFrame:
    columns = ["game_id", "player_id", "kickoff_utc", "season_year", "week"]
    if chart.empty or team_col not in chart.columns:
        return pd.DataFrame(columns=columns)
    times = chart[[team_col, "effective_at"]].drop_duplicates().rename(columns={team_col: "team_id", "effective_at": "listed_at"})
    picked = pd.merge_asof(
        sides.sort_values("kickoff_utc"),
        times.sort_values("listed_at"),
        left_on="kickoff_utc",
        right_on="listed_at",
        by="team_id",
        direction="backward",
        allow_exact_matches=False,
    ).dropna(subset=["listed_at"])
    if picked.empty:
        return pd.DataFrame(columns=columns)
    names = picked.merge(chart, left_on=["team_id", "listed_at"], right_on=[team_col, "effective_at"], how="inner")
    return names[columns].drop_duplicates(["game_id", "player_id"])


def _completed_snap_history(listed: pd.DataFrame, snaps: pd.DataFrame) -> pd.DataFrame:
    """Last completed game's snap share. It is known four hours after that kickoff."""
    if snaps.empty or listed.empty:
        return listed
    history = snaps.sort_values(["player_id", "kickoff_utc"]).copy()
    grouped = history.groupby("player_id", sort=False)["offense_snap_share"]
    history["offense_snap_share_last"] = history["offense_snap_share"]
    history["offense_snap_share_roll3"] = grouped.transform(lambda s: s.rolling(3, min_periods=1).mean())
    history["offense_snap_share_roll10"] = grouped.transform(lambda s: s.rolling(10, min_periods=3).mean())
    history["offense_snap_share_delta_3v10"] = history["offense_snap_share_roll3"] - history["offense_snap_share_roll10"]
    history["known_at"] = history["kickoff_utc"] + pd.Timedelta(hours=4)
    merged = pd.merge_asof(
        listed.sort_values("kickoff_utc"),
        history[
            [
                "player_id",
                "known_at",
                "offense_snap_share_last",
                "offense_snap_share_roll3",
                "offense_snap_share_roll10",
                "offense_snap_share_delta_3v10",
            ]
        ].sort_values("known_at"),
        left_on="kickoff_utc",
        right_on="known_at",
        by="player_id",
        direction="backward",
        allow_exact_matches=False,
    )
    return merged.drop(columns=["known_at"], errors="ignore")


def _read(session: Session, sql: str) -> pd.DataFrame:
    return pd.read_sql(sql, session.connection())


def _expand_metrics(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    parsed = []
    for rec in frame.itertuples(index=False):
        raw = rec.metrics
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        item = {"player_id": rec.player_id, "effective_at": rec.effective_at, "stat_type": rec.stat_type}
        if isinstance(raw, dict):
            item.update(raw)
        parsed.append(item)
    return pd.DataFrame(parsed)


def _team_rows(session: Session, games: pd.DataFrame) -> list[dict[str, Any]]:
    kickoff = games.set_index("game_id")["kickoff_utc"]
    meta = games.set_index("game_id")[["season_year", "week"]]
    source = _read(
        session,
        """
        SELECT game_id, possession_team, offense_formation AS formation, defense_man_zone_type AS man_zone
        FROM participation_plays
        WHERE provider = 'nflverse' AND possession_team IS NOT NULL AND possession_team != ''
        """,
    )
    plays = []
    if not source.empty:
        source = source[source["game_id"].isin(set(kickoff.index))]
        for rec in source.itertuples(index=False):
            plays.append(
                {
                    "game_id": rec.game_id,
                    "team_id": nfl_team_id(rec.possession_team),
                    "kickoff_utc": kickoff.loc[rec.game_id],
                    "formation": rec.formation,
                    "man_zone": rec.man_zone,
                }
            )
    frame = pd.DataFrame(plays)
    if frame.empty:
        return []
    grouped = []
    for (game_id, team_id), part in frame.groupby(["game_id", "team_id"]):
        formations = part["formation"].dropna()
        shells = part["man_zone"].dropna()
        shells = shells[shells.isin([_MAN, _ZONE])]
        item: dict[str, Any] = {"game_id": game_id, "team_id": team_id, "kickoff_utc": part["kickoff_utc"].iloc[0]}
        if len(formations):
            item["shotgun_share"] = float((formations == "SHOTGUN").mean())
            item["under_center_share"] = float((formations == "UNDER CENTER").mean())
            item["pistol_share"] = float((formations == "PISTOL").mean())
            item["formation_plays"] = int(len(formations))
        if len(shells):
            item["zone_share"] = float((shells == _ZONE).mean())
            item["man_share"] = float((shells == _MAN).mean())
            item["coverage_plays"] = int(len(shells))
        if len(item) > 3:
            grouped.append(item)
    team = pd.DataFrame(grouped)
    if team.empty:
        return []
    team = team.sort_values(["team_id", "kickoff_utc"])
    value_cols = [col for col in team.columns if col.endswith("_share")]
    for col in value_cols:
        team[f"prior_{col}"] = team.groupby("team_id")[col].shift(1)
    rows = []
    for rec in team.itertuples(index=False):
        metrics = {}
        for col in value_cols:
            value = _clean_metric(getattr(rec, f"prior_{col}", None))
            if value is not None:
                metrics[col] = value
        if not metrics:
            continue
        info = meta.loc[rec.game_id]
        rows.append(
            {
                "id": str(uuid4()),
                "league_id": "nfl",
                "team_id": rec.team_id,
                "season_year": int(info.season_year or 0),
                "week": int(info.week or 0),
                "game_id": rec.game_id,
                "grain": "usage_pregame",
                "split": "all",
                "metrics": metrics,
                "coverage": {
                    "sources": ["participation"],
                    "as_of": pd.Timestamp(rec.kickoff_utc).isoformat(),
                    "rule": "previous game only; blank formation and coverage plays are left out of the rate",
                },
                "sample_size": 1,
                "source": USAGE_SOURCE,
            }
        )
    _ = _FORMATIONS
    return rows
