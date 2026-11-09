"""Pregame team-TD feature matrix. Every feature uses only prior kickoffs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.analytics.definitions import SHRINKAGE_K
from spe.analytics.engine import apply_opponent_adjustments, attach_game_context, team_game_metrics
from spe.db.models import Game
from spe.eval.leakage import assert_event_time
from spe.models.team_td.labels import team_game_labels

FEATURE_COLS = (
    "is_home",
    "prior_off_td",
    "prior_pass_td",
    "prior_rush_td",
    "prior_possessions",
    "prior_off_plays",
    "prior_rz_plays",
    "prior_scoring_drives",
    "prior_off_epa",
    "prior_pace",
    "prior_explosive",
    "prior_rz_td_rate",
    "prior_pass_share",
    "opp_prior_def_epa",
    "opp_prior_off_td_allowed",
    "opp_prior_pass_td_allowed",
    "opp_prior_rush_td_allowed",
    "opp_prior_explosive_allowed",
    "opp_prior_rz_td_allowed",
    "exp_possessions",
    "epa_edge",
    "home_field",
    "weather_temp_c",
    "weather_wind_mps",
    "weather_available",
    "injury_out_skill",
    "injury_report_complete",
    "sample_size",
)

LABEL_COLS = (
    "off_td",
    "pass_td",
    "rush_td",
    "off_td_bucket",
    "possessions",
    "off_plays",
    "rz_plays",
    "scoring_drives",
    "def_st_td_scored",
)


def load_plays_and_games(session: Session, league_id: str) -> tuple[pd.DataFrame, pd.DataFrame]:
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
                "season_year": int(g.season_id.rsplit("-", 1)[-1]) if g.season_id and "-" in g.season_id else 0,
                "league_id": g.league_id,
                "neutral_site": bool(g.neutral_site),
                "venue_id": g.venue_id,
            }
            for g in games
        ]
    )
    if gdf.empty:
        return gdf, pd.DataFrame()
    conn = session.connection()
    pdf = pd.read_sql(
        text(
            """
            SELECT p.game_id, p.play_id, p.offense_id, p.defense_id,
                   p.down, p.distance, p.yardline, p.yards,
                   COALESCE(p.epa, p.ppa) AS epa, p.success, p.wp,
                   p.is_pass, p.is_rush, p.is_td, p.is_turnover, p.is_red_zone, p.is_goal_line,
                   p.is_scrimmage, p.first_down, p.period, p.game_seconds_remaining,
                   p.score_before_off AS score_off, p.score_before_def AS score_def,
                   COALESCE(p.drive_id, '') AS drive_id, '' AS drive_result
            FROM plays p
            JOIN games g ON g.game_id = p.game_id
            WHERE g.league_id = :lg AND g.data_tier = 'historical' AND g.status = 'final'
            """
        ),
        conn,
        params={"lg": league_id},
    )
    if pdf.empty:
        return gdf, pdf
    for col in (
        "is_pass",
        "is_rush",
        "is_td",
        "is_turnover",
        "is_red_zone",
        "is_goal_line",
        "is_scrimmage",
        "first_down",
        "success",
    ):
        if col in pdf.columns:
            pdf[col] = pdf[col].fillna(0).astype(bool)
    return gdf, pdf


def build_team_td_frame(session: Session, league_id: str) -> pd.DataFrame:
    """Labeled team-game rows with strictly prior features (vectorized expanding means)."""
    games, plays = load_plays_and_games(session, league_id)
    if games.empty or plays.empty:
        return pd.DataFrame()
    plays_x = attach_game_context(plays, games)
    team_metrics = apply_opponent_adjustments(team_game_metrics(plays_x), league_id, leave_one_out=True)
    labels = team_game_labels(plays_x)
    if labels.empty or team_metrics.empty:
        return pd.DataFrame()

    meta = games[
        [
            "game_id",
            "home_team_id",
            "away_team_id",
            "kickoff_utc",
            "week",
            "season_year",
            "league_id",
            "neutral_site",
            "venue_id",
        ]
    ].copy()
    meta["kickoff_utc"] = pd.to_datetime(meta["kickoff_utc"], utc=True)
    labels = labels.merge(meta, on="game_id", how="left")
    labels["is_home"] = labels["team_id"] == labels["home_team_id"]
    labels["opp_id"] = np.where(labels["is_home"], labels["away_team_id"], labels["home_team_id"])

    metric_cols = [
        c
        for c in (
            "game_id",
            "team_id",
            "off_epa_per_play",
            "def_epa_per_play",
            "adj_off_epa",
            "adj_def_epa",
            "pace_sec_per_play",
            "explosive_play_rate",
            "explosive_play_rate_allowed",
            "red_zone_td_rate",
            "plays_per_game",
            "rec_td_allowed",
            "rush_td_allowed",
            "rz_td_allowed",
        )
        if c in team_metrics.columns
    ]
    labeled = labels.merge(team_metrics[metric_cols], on=["game_id", "team_id"], how="left")
    labeled = labeled.sort_values(["kickoff_utc", "game_id", "team_id"]).reset_index(drop=True)

    own_cols = {
        "off_td": "prior_off_td",
        "pass_td": "prior_pass_td",
        "rush_td": "prior_rush_td",
        "possessions": "prior_possessions",
        "off_plays": "prior_off_plays",
        "rz_plays": "prior_rz_plays",
        "scoring_drives": "prior_scoring_drives",
        "off_epa_per_play": "prior_off_epa",
        "pace_sec_per_play": "prior_pace",
        "explosive_play_rate": "prior_explosive",
        "red_zone_td_rate": "prior_rz_td_rate",
        "def_epa_per_play": "prior_def_epa",
        "explosive_play_rate_allowed": "prior_explosive_allowed",
        "rz_td_allowed": "prior_rz_td_allowed_own",
    }
    for src, dest in own_cols.items():
        if src not in labeled.columns:
            labeled[dest] = np.nan
            continue
        labeled[dest] = (
            labeled.groupby("team_id", sort=False)[src]
            .transform(lambda s: s.shift(1).expanding(min_periods=1).mean())
        )
    labeled["sample_size"] = labeled.groupby("team_id", sort=False).cumcount().astype(float)

    # Opponent priors: map opponent's own prior defensive / allowed columns.
    opp_map = labeled[
        [
            "game_id",
            "team_id",
            "prior_def_epa",
            "prior_explosive_allowed",
            "prior_rz_td_allowed_own",
            "prior_pace",
            "prior_off_td",
            "prior_pass_td",
            "prior_rush_td",
        ]
    ].rename(
        columns={
            "team_id": "opp_id",
            "prior_def_epa": "opp_prior_def_epa",
            "prior_explosive_allowed": "opp_prior_explosive_allowed",
            "prior_rz_td_allowed_own": "opp_prior_rz_td_allowed",
            "prior_pace": "opp_prior_pace",
            "prior_off_td": "opp_prior_off_td_scored",
            "prior_pass_td": "opp_prior_pass_td_scored",
            "prior_rush_td": "opp_prior_rush_td_scored",
        }
    )
    labeled = labeled.merge(opp_map, on=["game_id", "opp_id"], how="left")

    # TDs allowed = mean of opponents' offensive TDs against this team (shifted).
    allowed = labeled[["game_id", "kickoff_utc", "opp_id", "off_td", "pass_td", "rush_td"]].copy()
    allowed = allowed.rename(columns={"opp_id": "team_id", "off_td": "allowed_off_td", "pass_td": "allowed_pass_td", "rush_td": "allowed_rush_td"})
    allowed = allowed.sort_values(["team_id", "kickoff_utc", "game_id"])
    for src, dest in (
        ("allowed_off_td", "prior_off_td_allowed"),
        ("allowed_pass_td", "prior_pass_td_allowed"),
        ("allowed_rush_td", "prior_rush_td_allowed"),
    ):
        allowed[dest] = (
            allowed.groupby("team_id", sort=False)[src]
            .transform(lambda s: s.shift(1).expanding(min_periods=1).mean())
        )
    allowed_feats = allowed[
        ["game_id", "team_id", "prior_off_td_allowed", "prior_pass_td_allowed", "prior_rush_td_allowed"]
    ].rename(columns={"team_id": "opp_id"})
    labeled = labeled.merge(allowed_feats, on=["game_id", "opp_id"], how="left")
    labeled["opp_prior_off_td_allowed"] = labeled["prior_off_td_allowed"]
    labeled["opp_prior_pass_td_allowed"] = labeled["prior_pass_td_allowed"]
    labeled["opp_prior_rush_td_allowed"] = labeled["prior_rush_td_allowed"]

    league_td = 2.3 if league_id == "nfl" else 3.0
    league_poss = 11.5 if league_id == "nfl" else 12.5
    league_plays = 65.0 if league_id == "nfl" else 72.0
    league_pace = 28.0 if league_id == "nfl" else 24.0
    k = SHRINKAGE_K.get(league_id, 10.0)
    n = labeled["sample_size"].to_numpy(dtype=float)
    w = n / (n + k)

    def shrink_col(col: str, default: float) -> np.ndarray:
        raw = pd.to_numeric(labeled[col], errors="coerce").to_numpy(dtype=float) if col in labeled.columns else np.full(len(labeled), np.nan)
        return np.where(np.isfinite(raw), w * raw + (1.0 - w) * default, default)

    labeled["prior_off_td"] = shrink_col("prior_off_td", league_td)
    labeled["prior_pass_td"] = shrink_col("prior_pass_td", league_td * 0.6)
    labeled["prior_rush_td"] = shrink_col("prior_rush_td", league_td * 0.4)
    labeled["prior_possessions"] = shrink_col("prior_possessions", league_poss)
    labeled["prior_off_plays"] = shrink_col("prior_off_plays", league_plays)
    labeled["prior_rz_plays"] = shrink_col("prior_rz_plays", 8.0)
    labeled["prior_scoring_drives"] = shrink_col("prior_scoring_drives", 3.5)
    labeled["prior_off_epa"] = shrink_col("prior_off_epa", 0.0)
    labeled["prior_pace"] = shrink_col("prior_pace", league_pace)
    labeled["prior_explosive"] = shrink_col("prior_explosive", 0.12)
    labeled["prior_rz_td_rate"] = shrink_col("prior_rz_td_rate", 0.2)
    pass_share_raw = np.where(
        (labeled["prior_pass_td"] + labeled["prior_rush_td"]) > 0,
        labeled["prior_pass_td"] / (labeled["prior_pass_td"] + labeled["prior_rush_td"]),
        0.6,
    )
    labeled["prior_pass_share"] = pass_share_raw
    labeled["opp_prior_def_epa"] = shrink_col("opp_prior_def_epa", 0.0)
    labeled["opp_prior_off_td_allowed"] = shrink_col("opp_prior_off_td_allowed", league_td)
    labeled["opp_prior_pass_td_allowed"] = shrink_col("opp_prior_pass_td_allowed", league_td * 0.6)
    labeled["opp_prior_rush_td_allowed"] = shrink_col("opp_prior_rush_td_allowed", league_td * 0.4)
    labeled["opp_prior_explosive_allowed"] = shrink_col("opp_prior_explosive_allowed", 0.12)
    labeled["opp_prior_rz_td_allowed"] = shrink_col("opp_prior_rz_td_allowed", 1.5)

    own_pace = labeled["prior_pace"].to_numpy(dtype=float)
    opp_pace = pd.to_numeric(labeled.get("opp_prior_pace"), errors="coerce").to_numpy(dtype=float)
    base_poss = labeled["prior_possessions"].to_numpy(dtype=float)
    pace_mid = (own_pace + np.where(np.isfinite(opp_pace), opp_pace, own_pace)) / 2.0
    pace_factor = np.where(pace_mid > 0, own_pace / np.maximum(pace_mid, 1.0), 1.0)
    labeled["exp_possessions"] = np.clip(base_poss * pace_factor, 8.0, 16.0)
    labeled["epa_edge"] = labeled["prior_off_epa"] - labeled["opp_prior_def_epa"]
    labeled["home_field"] = np.where(
        labeled["neutral_site"].fillna(False),
        0.0,
        np.where(labeled["is_home"], 1.0, -1.0),
    ).astype(float)
    labeled["is_home"] = labeled["is_home"].astype(float)

    weather = _weather_by_game(session, league_id)
    injuries = _injury_skill_out(session, league_id)
    temps: list[float] = []
    winds: list[float] = []
    wx_ok: list[float] = []
    inj_out: list[float] = []
    inj_ok: list[float] = []
    as_ofs: list[str] = []
    for rec in labeled.itertuples(index=False):
        kick = pd.Timestamp(rec.kickoff_utc)
        if kick.tzinfo is None:
            kick = kick.tz_localize("UTC")
        as_of = (kick - pd.Timedelta(seconds=1)).to_pydatetime()
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=UTC)
        as_ofs.append(as_of.isoformat())
        wx = weather.get(rec.game_id)
        ok = False
        temp = wind = 0.0
        if wx is not None and wx.get("issued_at") is not None:
            try:
                assert_event_time(as_of=as_of, observed_at=wx["issued_at"])
                ok = True
                temp = float(wx["temperature_c"]) if wx.get("temperature_c") is not None else 0.0
                wind = float(wx["wind_mps"]) if wx.get("wind_mps") is not None else 0.0
            except Exception:
                ok = False
        wx_ok.append(1.0 if ok else 0.0)
        temps.append(temp if ok else 0.0)
        winds.append(wind if ok else 0.0)
        inj = injuries.get(rec.game_id, {}).get(rec.team_id, {})
        complete = bool(inj.get("complete"))
        inj_ok.append(1.0 if complete else 0.0)
        inj_out.append(float(inj.get("out_skill") or 0) if complete else 0.0)

    labeled["weather_temp_c"] = temps
    labeled["weather_wind_mps"] = winds
    labeled["weather_available"] = wx_ok
    labeled["injury_out_skill"] = inj_out
    labeled["injury_report_complete"] = inj_ok
    labeled["as_of"] = as_ofs
    labeled["league_id"] = league_id

    # Keep raw same-game metrics so upcoming feature rebuild can average priors.
    raw_keep = (
        "off_epa_per_play",
        "def_epa_per_play",
        "pace_sec_per_play",
        "explosive_play_rate",
        "explosive_play_rate_allowed",
        "red_zone_td_rate",
        "rz_td_allowed",
    )
    keep = [
        "game_id",
        "team_id",
        "opp_id",
        "league_id",
        "season_year",
        "week",
        "kickoff_utc",
        "neutral_site",
        "as_of",
        *LABEL_COLS,
        *FEATURE_COLS,
        *raw_keep,
    ]
    out = labeled[[c for c in keep if c in labeled.columns]].copy()
    return out


def features_for_upcoming_game(
    hist: pd.DataFrame,
    *,
    game_id: str,
    team_id: str,
    opp_id: str,
    kickoff_utc: datetime,
    is_home: bool,
    neutral_site: bool,
    league_id: str,
    weather: dict[str, Any] | None = None,
    injury: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one pregame feature row from completed history only."""
    kick = pd.Timestamp(kickoff_utc)
    if kick.tzinfo is None:
        kick = kick.tz_localize("UTC")
    as_of = kick - pd.Timedelta(seconds=1)
    own = hist.loc[(hist["team_id"] == team_id) & (pd.to_datetime(hist["kickoff_utc"], utc=True) < as_of)]
    opp = hist.loc[(hist["team_id"] == opp_id) & (pd.to_datetime(hist["kickoff_utc"], utc=True) < as_of)]
    allowed = hist.loc[(hist["opp_id"] == opp_id) & (pd.to_datetime(hist["kickoff_utc"], utc=True) < as_of)]

    league_td = 2.3 if league_id == "nfl" else 3.0
    league_poss = 11.5 if league_id == "nfl" else 12.5
    league_plays = 65.0 if league_id == "nfl" else 72.0
    league_pace = 28.0 if league_id == "nfl" else 24.0
    k = SHRINKAGE_K.get(league_id, 10.0)
    n = int(len(own))

    def shrink(val: float | None, default: float, n_obs: int) -> float:
        if val is None or (isinstance(val, float) and np.isnan(val)):
            return default
        ww = n_obs / (n_obs + k) if n_obs else 0.0
        return float(ww * val + (1.0 - ww) * default)

    def raw_mean(df: pd.DataFrame, col: str) -> float | None:
        if df.empty or col not in df.columns:
            return None
        val = pd.to_numeric(df[col], errors="coerce").mean()
        return None if pd.isna(val) else float(val)

    prior_off_td = shrink(raw_mean(own, "off_td"), league_td, n)
    prior_pass_td = shrink(raw_mean(own, "pass_td"), league_td * 0.6, n)
    prior_rush_td = shrink(raw_mean(own, "rush_td"), league_td * 0.4, n)
    prior_poss = shrink(raw_mean(own, "possessions"), league_poss, n)
    prior_plays = shrink(raw_mean(own, "off_plays"), league_plays, n)
    prior_rz = shrink(raw_mean(own, "rz_plays"), 8.0, n)
    prior_scoring = shrink(raw_mean(own, "scoring_drives"), 3.5, n)
    prior_off_epa = shrink(raw_mean(own, "off_epa_per_play"), 0.0, n)
    prior_pace = shrink(raw_mean(own, "pace_sec_per_play"), league_pace, n)
    prior_explosive = shrink(raw_mean(own, "explosive_play_rate"), 0.12, n)
    prior_rz_rate = shrink(raw_mean(own, "red_zone_td_rate"), 0.2, n)
    prior_pass_share = (
        prior_pass_td / (prior_pass_td + prior_rush_td) if (prior_pass_td + prior_rush_td) > 0 else 0.6
    )
    n_opp = int(len(opp))
    opp_def = shrink(raw_mean(opp, "def_epa_per_play"), 0.0, n_opp)
    opp_pace = shrink(raw_mean(opp, "pace_sec_per_play"), league_pace, n_opp)
    opp_explosive_allowed = shrink(raw_mean(opp, "explosive_play_rate_allowed"), 0.12, n_opp)
    opp_rz_allowed = shrink(raw_mean(opp, "rz_td_allowed"), 1.5, n_opp)
    n_allowed = int(len(allowed))
    opp_allowed_td = shrink(raw_mean(allowed, "off_td"), league_td, n_allowed)
    opp_pass_allowed = shrink(raw_mean(allowed, "pass_td"), league_td * 0.6, n_allowed)
    opp_rush_allowed = shrink(raw_mean(allowed, "rush_td"), league_td * 0.4, n_allowed)

    pace_mid = (prior_pace + opp_pace) / 2.0
    pace_factor = prior_pace / max(pace_mid, 1.0) if pace_mid > 0 else 1.0
    exp_poss = float(np.clip(prior_poss * pace_factor, 8.0, 16.0))

    as_of_dt = as_of.to_pydatetime()
    if as_of_dt.tzinfo is None:
        as_of_dt = as_of_dt.replace(tzinfo=UTC)
    weather_ok = False
    temp = wind = 0.0
    if weather and weather.get("issued_at") is not None:
        try:
            assert_event_time(as_of=as_of_dt, observed_at=weather["issued_at"])
            weather_ok = True
            temp = float(weather["temperature_c"] or 0.0)
            wind = float(weather["wind_mps"] or 0.0)
        except Exception:
            weather_ok = False
    inj_complete = bool(injury.get("complete")) if injury else False
    inj_out = float(injury.get("out_skill") or 0) if inj_complete else 0.0

    return {
        "game_id": game_id,
        "team_id": team_id,
        "opp_id": opp_id,
        "league_id": league_id,
        "kickoff_utc": kick.to_pydatetime(),
        "is_home": 1.0 if is_home else 0.0,
        "neutral_site": bool(neutral_site),
        "prior_off_td": prior_off_td,
        "prior_pass_td": prior_pass_td,
        "prior_rush_td": prior_rush_td,
        "prior_possessions": prior_poss,
        "prior_off_plays": prior_plays,
        "prior_rz_plays": prior_rz,
        "prior_scoring_drives": prior_scoring,
        "prior_off_epa": prior_off_epa,
        "prior_pace": prior_pace,
        "prior_explosive": prior_explosive,
        "prior_rz_td_rate": prior_rz_rate,
        "prior_pass_share": float(prior_pass_share),
        "opp_prior_def_epa": opp_def,
        "opp_prior_off_td_allowed": opp_allowed_td,
        "opp_prior_pass_td_allowed": opp_pass_allowed,
        "opp_prior_rush_td_allowed": opp_rush_allowed,
        "opp_prior_explosive_allowed": opp_explosive_allowed,
        "opp_prior_rz_td_allowed": opp_rz_allowed,
        "exp_possessions": exp_poss,
        "epa_edge": prior_off_epa - opp_def,
        "home_field": 0.0 if neutral_site else (1.0 if is_home else -1.0),
        "weather_temp_c": temp if weather_ok else 0.0,
        "weather_wind_mps": wind if weather_ok else 0.0,
        "weather_available": 1.0 if weather_ok else 0.0,
        "injury_out_skill": inj_out,
        "injury_report_complete": 1.0 if inj_complete else 0.0,
        "sample_size": float(n),
        "as_of": as_of_dt.isoformat(),
    }


def _weather_by_game(session: Session, league_id: str) -> dict[str, dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT w.game_id, w.issued_at, w.temperature_c, w.wind_mps, w.quality_status, w.provider
            FROM weather_snapshots w
            JOIN games g ON g.game_id = w.game_id
            WHERE g.league_id = :lg
              AND w.quality_status NOT IN ('observed_not_forecast')
              AND w.provider IN ('nws', 'nws_live')
            ORDER BY w.issued_at DESC
            """
        ),
        {"lg": league_id},
    ).mappings().all()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        gid = row["game_id"]
        if gid in out:
            continue
        issued = row["issued_at"]
        if isinstance(issued, str):
            try:
                issued = datetime.fromisoformat(issued.replace("Z", "+00:00"))
            except ValueError:
                issued = None
        if issued is not None and getattr(issued, "tzinfo", None) is None:
            issued = issued.replace(tzinfo=UTC)
        out[gid] = {
            "issued_at": issued,
            "temperature_c": row["temperature_c"],
            "wind_mps": row["wind_mps"],
            "provider": row["provider"],
        }
    return out


def _injury_skill_out(session: Session, league_id: str) -> dict[str, dict[str, dict[str, Any]]]:
    rows = session.execute(
        text(
            """
            SELECT i.game_id, r.team_id, i.designation, i.coverage_complete, p.position_group, i.reported_at, g.kickoff_utc
            FROM injury_events i
            JOIN games g ON g.game_id = i.game_id
            JOIN players p ON p.player_id = i.player_id
            LEFT JOIN rosters r ON r.player_id = i.player_id AND r.valid_to IS NULL
            WHERE g.league_id = :lg AND i.reported_at < g.kickoff_utc
            """
        ),
        {"lg": league_id},
    ).mappings().all()
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        gid = row["game_id"]
        team = row["team_id"]
        if not gid or not team:
            continue
        slot = out.setdefault(gid, {}).setdefault(team, {"complete": False, "out_skill": 0})
        if row["coverage_complete"]:
            slot["complete"] = True
        des = (row["designation"] or "").lower()
        pos = row["position_group"] or ""
        if pos in {"QB", "RB", "WR", "TE"} and any(tok in des for tok in ("out", "ir", "pup", "suspended")):
            slot["out_skill"] += 1
    return out


def chronological_split_frame(
    frame: pd.DataFrame,
    *,
    train_end: datetime,
    test_start: datetime,
    test_end: datetime,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    kick = pd.to_datetime(frame["kickoff_utc"], utc=True)
    train_end_ts = pd.Timestamp(train_end)
    test_start_ts = pd.Timestamp(test_start)
    test_end_ts = pd.Timestamp(test_end)
    if train_end_ts.tzinfo is None:
        train_end_ts = train_end_ts.tz_localize("UTC")
    if test_start_ts.tzinfo is None:
        test_start_ts = test_start_ts.tz_localize("UTC")
    if test_end_ts.tzinfo is None:
        test_end_ts = test_end_ts.tz_localize("UTC")
    train = frame.loc[kick < train_end_ts].copy()
    test = frame.loc[(kick >= test_start_ts) & (kick <= test_end_ts)].copy()
    return train, test


# Re-export helpers used by score.py
weather_by_game = _weather_by_game
injury_skill_out = _injury_skill_out
