"""League-specific team strength ratings from prior-only EPA/PPA and scoring.

Ratings are computed chronologically: each game only sees completed prior games.
NFL and FBS never share calibrated parameters.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.analytics.definitions import SHRINKAGE_K
from spe.analytics.engine import (
    attach_game_context,
    prior_only,
    shrink,
    team_game_metrics,
)
from spe.db.models import Team
from spe.integrity.assignments import NFL_CONTINUITY, canonical_team_map
from spe.models.ats.labels import attach_margins

FEATURE_COLS = (
    "home_off_epa",
    "home_def_epa",
    "away_off_epa",
    "away_def_epa",
    "home_off_success",
    "home_def_success",
    "away_off_success",
    "away_def_success",
    "home_explosive",
    "away_explosive",
    "home_explosive_allowed",
    "away_explosive_allowed",
    "home_pace",
    "away_pace",
    "home_ppg",
    "away_ppg",
    "home_papg",
    "away_papg",
    "home_margin_prior",
    "away_margin_prior",
    "epa_edge",
    "success_edge",
    "ppg_edge",
    "home_field",
    "home_rest_days",
    "away_rest_days",
    "rest_edge",
    "weather_temp_c",
    "weather_wind_mps",
    "weather_available",
    "sample_size",
    "home_games_prior",
    "away_games_prior",
)


def load_ats_games(session: Session, league_id: str) -> pd.DataFrame:
    rows = session.execute(
        text(
            """
            SELECT g.game_id, g.league_id, g.home_team_id, g.away_team_id,
                   g.kickoff_utc, g.week, g.neutral_site, g.venue_id, g.status,
                   g.home_score, g.away_score, g.data_tier, g.season_id
            FROM games g
            WHERE g.league_id = :lg
              AND g.data_tier IN ('historical', 'upcoming')
            """
        ),
        {"lg": league_id},
    ).mappings().all()
    if not rows:
        return pd.DataFrame()
    gdf = pd.DataFrame([dict(r) for r in rows])
    gdf["kickoff_utc"] = pd.to_datetime(gdf["kickoff_utc"], utc=True)
    gdf["season_year"] = gdf["season_id"].astype(str).str.rsplit("-", n=1).str[-1].astype(int)
    gdf = attach_margins(gdf)
    return gdf


def load_plays(session: Session, league_id: str) -> pd.DataFrame:
    return pd.read_sql(
        text(
            """
            SELECT p.game_id, p.play_id, p.offense_id, p.defense_id,
                   p.down, p.distance, p.yardline, p.yards,
                   COALESCE(p.epa, p.ppa) AS epa, p.success,
                   p.is_pass, p.is_rush, p.is_td, p.is_turnover, p.is_red_zone, p.is_goal_line,
                   p.is_scrimmage, p.first_down, p.period, p.game_seconds_remaining,
                   p.score_before_off AS score_off, p.score_before_def AS score_def,
                   COALESCE(p.drive_id, '') AS drive_id,
                   '' AS drive_result
            FROM plays p
            JOIN games g ON g.game_id = p.game_id
            WHERE g.league_id = :lg AND g.data_tier = 'historical'
            """
        ),
        session.connection(),
        params={"lg": league_id},
    )


def load_closing_spreads(session: Session, league_id: str) -> pd.DataFrame:
    # NFL stores nflverse L (positive = home favored). FBS stores the CFBD
    # home handicap (negative = home favored). Callers must honor `source`.
    source = "nflverse_consensus" if league_id == "nfl" else "cfbd"
    return pd.read_sql(
        text(
            """
            SELECT c.game_id, c.home_line AS spread_line,
                   c.home_price_american, c.away_price_american, c.source
            FROM closing_lines c
            JOIN games g ON g.game_id = c.game_id
            WHERE g.league_id = :lg
              AND c.market_id = 'point_spread'
              AND c.source = :src
            """
        ),
        session.connection(),
        params={"lg": league_id, "src": source},
    )


def load_weather_priors(session: Session, league_id: str) -> pd.DataFrame:
    return pd.read_sql(
        text(
            """
            SELECT w.game_id,
                   AVG(w.temperature_c) AS weather_temp_c,
                   AVG(w.wind_mps) AS weather_wind_mps
            FROM weather_snapshots w
            JOIN games g ON g.game_id = w.game_id
            WHERE g.league_id = :lg
            GROUP BY w.game_id
            """
        ),
        session.connection(),
        params={"lg": league_id},
    )


def team_id_canon(session: Session, league_id: str) -> dict[str, str]:
    """One id per club, so a move or a second scoreboard id keeps its seasons.

    NFL: Oakland, San Diego, and St. Louis seasons fold into the current club.
    FBS: a short scoreboard id folds into the historical ``fbs:{espn}`` id.
    """
    if league_id == "nfl":
        return dict(NFL_CONTINUITY)
    if league_id != "fbs":
        return {}
    teams = session.query(Team).filter(Team.league_id == league_id).all()
    return canonical_team_map(teams)


def _apply_canon(frame: pd.DataFrame, columns: list[str], canon: dict[str, str]) -> None:
    if not canon:
        return
    for column in columns:
        if column not in frame.columns:
            continue
        frame[column] = frame[column].map(lambda value: canon.get(str(value), str(value)) if pd.notna(value) else value)


def build_team_game_table(session: Session, league_id: str) -> pd.DataFrame:
    games = load_ats_games(session, league_id)
    plays = load_plays(session, league_id)
    if games.empty or plays.empty:
        return pd.DataFrame()
    canon = team_id_canon(session, league_id)
    _apply_canon(games, ["home_team_id", "away_team_id"], canon)
    _apply_canon(plays, ["offense_id", "defense_id"], canon)
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
        if col in plays.columns:
            plays[col] = plays[col].fillna(0).astype(bool)
    ctx = attach_game_context(plays, games)
    metrics = team_game_metrics(ctx)
    if metrics.empty:
        return pd.DataFrame()
    home = games[
        ["game_id", "home_team_id", "away_team_id", "home_score", "away_score", "kickoff_utc", "neutral_site"]
    ].copy()
    home["team_id"] = home["home_team_id"]
    home["opp_id"] = home["away_team_id"]
    home["points_for"] = home["home_score"]
    home["points_against"] = home["away_score"]
    home["is_home"] = 1
    away = games[
        ["game_id", "home_team_id", "away_team_id", "home_score", "away_score", "kickoff_utc", "neutral_site"]
    ].copy()
    away["team_id"] = away["away_team_id"]
    away["opp_id"] = away["home_team_id"]
    away["points_for"] = away["away_score"]
    away["points_against"] = away["home_score"]
    away["is_home"] = 0
    sides = pd.concat([home, away], ignore_index=True)
    out = metrics.merge(
        sides[
            [
                "game_id",
                "team_id",
                "opp_id",
                "points_for",
                "points_against",
                "is_home",
                "kickoff_utc",
                "neutral_site",
            ]
        ],
        on=["game_id", "team_id"],
        how="inner",
        suffixes=("", "_side"),
    )
    if "kickoff_utc_side" in out.columns:
        out["kickoff_utc"] = out["kickoff_utc"].fillna(out["kickoff_utc_side"])
        out = out.drop(columns=["kickoff_utc_side"])
    out["kickoff_utc"] = pd.to_datetime(out["kickoff_utc"], utc=True)
    out["margin"] = out["points_for"] - out["points_against"]
    return out.sort_values("kickoff_utc")


class _RunningTeam:
    __slots__ = (
        "n",
        "off_epa",
        "def_epa",
        "off_success",
        "def_success",
        "explosive",
        "explosive_allowed",
        "pace",
        "ppg",
        "papg",
        "margin",
        "recent_off",
        "recent_def",
        "kickoffs",
    )

    def __init__(self) -> None:
        self.n = 0
        self.off_epa = 0.0
        self.def_epa = 0.0
        self.off_success = 0.0
        self.def_success = 0.0
        self.explosive = 0.0
        self.explosive_allowed = 0.0
        self.pace = 0.0
        self.ppg = 0.0
        self.papg = 0.0
        self.margin = 0.0
        self.recent_off: list[float] = []
        self.recent_def: list[float] = []
        self.kickoffs: list[pd.Timestamp] = []

    def snapshot(self, k: float, means: dict[str, float]) -> dict[str, float]:
        n = float(self.n)
        if self.n == 0:
            return {**{key: means[key] for key in means}, "n": 0.0, "margin": 0.0}

        def avg(total: float, prior: float) -> float:
            raw = total / n
            return float(shrink(raw, prior, n, k))

        season_off = avg(self.off_epa, means["off_epa"])
        season_def = avg(self.def_epa, means["def_epa"])
        recent_off = float(np.mean(self.recent_off)) if self.recent_off else season_off
        recent_def = float(np.mean(self.recent_def)) if self.recent_def else season_def
        w = min(0.45, 0.08 * len(self.recent_off))
        return {
            "off_epa": (1 - w) * season_off + w * recent_off,
            "def_epa": (1 - w) * season_def + w * recent_def,
            "off_success": avg(self.off_success, means["off_success"]),
            "def_success": avg(self.def_success, means["def_success"]),
            "explosive": avg(self.explosive, means["explosive"]),
            "explosive_allowed": avg(self.explosive_allowed, means["explosive_allowed"]),
            "pace": avg(self.pace, means["pace"]),
            "ppg": avg(self.ppg, means["ppg"]),
            "papg": avg(self.papg, means["papg"]),
            "margin": avg(self.margin, 0.0),
            "n": n,
        }

    def update(self, row: pd.Series) -> None:
        self.n += 1
        self.off_epa += _num(row.get("off_epa_per_play"), 0.0)
        self.def_epa += _num(row.get("def_epa_per_play"), 0.0)
        self.off_success += _num(row.get("off_success_rate"), 0.45)
        self.def_success += _num(row.get("def_success_rate"), 0.45)
        self.explosive += _num(row.get("explosive_play_rate"), 0.1)
        self.explosive_allowed += _num(row.get("explosive_play_rate_allowed"), 0.1)
        self.pace += _num(row.get("pace_sec_per_play"), 28.0)
        self.ppg += _num(row.get("points_for"), 22.0)
        self.papg += _num(row.get("points_against"), 22.0)
        self.margin += _num(row.get("margin"), 0.0)
        self.recent_off.append(_num(row.get("off_epa_per_play"), 0.0))
        self.recent_def.append(_num(row.get("def_epa_per_play"), 0.0))
        self.recent_off = self.recent_off[-5:]
        self.recent_def = self.recent_def[-5:]
        kick = pd.Timestamp(row["kickoff_utc"])
        self.kickoffs.append(kick)


def build_team_strength_frame(session: Session, league_id: str) -> pd.DataFrame:
    """One pregame feature row per game. Expanding prior-only team stats — O(n)."""
    games = load_ats_games(session, league_id)
    team_games = build_team_game_table(session, league_id)
    if games.empty or team_games.empty:
        return pd.DataFrame()

    spreads = load_closing_spreads(session, league_id)
    weather = load_weather_priors(session, league_id)
    k = float(SHRINKAGE_K.get(league_id, 10.0))
    canon = team_id_canon(session, league_id)

    def cid(value: object) -> str:
        text = str(value)
        return canon.get(text, text)

    league_hist = team_games.dropna(subset=["points_for"])
    means = {
        "off_epa": float(pd.to_numeric(league_hist.get("off_epa_per_play"), errors="coerce").mean() or 0.0),
        "def_epa": float(pd.to_numeric(league_hist.get("def_epa_per_play"), errors="coerce").mean() or 0.0),
        "off_success": float(pd.to_numeric(league_hist.get("off_success_rate"), errors="coerce").mean() or 0.45),
        "def_success": float(pd.to_numeric(league_hist.get("def_success_rate"), errors="coerce").mean() or 0.45),
        "explosive": float(pd.to_numeric(league_hist.get("explosive_play_rate"), errors="coerce").mean() or 0.1),
        "explosive_allowed": float(
            pd.to_numeric(league_hist.get("explosive_play_rate_allowed"), errors="coerce").mean() or 0.1
        ),
        "pace": float(pd.to_numeric(league_hist.get("pace_sec_per_play"), errors="coerce").mean() or 28.0),
        "ppg": float(pd.to_numeric(league_hist.get("points_for"), errors="coerce").mean() or 22.0),
        "papg": float(pd.to_numeric(league_hist.get("points_against"), errors="coerce").mean() or 22.0),
    }

    by_game: dict[str, list[pd.Series]] = defaultdict(list)
    for _, row in team_games.iterrows():
        by_game[str(row["game_id"])].append(row)

    finals = games[(games["status"] == "final") & games["margin"].notna()].sort_values("kickoff_utc")
    # Complete-after buffer matches analytics.prior_only default.
    complete_after = pd.Timedelta(hours=4, minutes=30)

    teams: dict[str, _RunningTeam] = defaultdict(_RunningTeam)
    pending_updates: list[tuple[pd.Timestamp, list[pd.Series]]] = []
    rows: list[dict[str, Any]] = []

    ordered = games.sort_values("kickoff_utc")
    final_idx = 0
    final_list = list(finals.itertuples(index=False))

    for rec in ordered.itertuples(index=False):
        as_of = pd.Timestamp(rec.kickoff_utc)
        # Apply completed games whose results would be known before this kickoff.
        while final_idx < len(final_list):
            done = final_list[final_idx]
            done_kick = pd.Timestamp(done.kickoff_utc)
            if done_kick + complete_after >= as_of:
                break
            for side_row in by_game.get(str(done.game_id), []):
                teams[cid(side_row["team_id"])].update(side_row)
            final_idx += 1

        home_key = cid(rec.home_team_id)
        away_key = cid(rec.away_team_id)
        home_state = teams[home_key].snapshot(k, means)
        away_state = teams[away_key].snapshot(k, means)
        if home_state["n"] < 1 or away_state["n"] < 1:
            continue

        home_rest = _rest_days(teams[home_key].kickoffs, as_of)
        away_rest = _rest_days(teams[away_key].kickoffs, as_of)
        home_field = 0.0 if bool(rec.neutral_site) else 1.0
        epa_edge = (home_state["off_epa"] - away_state["def_epa"]) - (
            away_state["off_epa"] - home_state["def_epa"]
        )
        success_edge = (home_state["off_success"] - away_state["def_success"]) - (
            away_state["off_success"] - home_state["def_success"]
        )
        ppg_edge = (home_state["ppg"] - away_state["papg"]) - (away_state["ppg"] - home_state["papg"])

        rows.append(
            {
                "game_id": rec.game_id,
                "league_id": league_id,
                "home_team_id": rec.home_team_id,
                "away_team_id": rec.away_team_id,
                "kickoff_utc": as_of.to_pydatetime(),
                "week": int(rec.week or 0),
                "season_year": int(rec.season_year),
                "status": rec.status,
                "home_score": rec.home_score,
                "away_score": rec.away_score,
                "margin": rec.margin if pd.notna(rec.margin) else np.nan,
                "home_off_epa": home_state["off_epa"],
                "home_def_epa": home_state["def_epa"],
                "away_off_epa": away_state["off_epa"],
                "away_def_epa": away_state["def_epa"],
                "home_off_success": home_state["off_success"],
                "home_def_success": home_state["def_success"],
                "away_off_success": away_state["off_success"],
                "away_def_success": away_state["def_success"],
                "home_explosive": home_state["explosive"],
                "away_explosive": away_state["explosive"],
                "home_explosive_allowed": home_state["explosive_allowed"],
                "away_explosive_allowed": away_state["explosive_allowed"],
                "home_pace": home_state["pace"],
                "away_pace": away_state["pace"],
                "home_ppg": home_state["ppg"],
                "away_ppg": away_state["ppg"],
                "home_papg": home_state["papg"],
                "away_papg": away_state["papg"],
                "home_margin_prior": home_state["margin"],
                "away_margin_prior": away_state["margin"],
                "epa_edge": epa_edge,
                "success_edge": success_edge,
                "ppg_edge": ppg_edge,
                "home_field": home_field,
                "home_rest_days": home_rest,
                "away_rest_days": away_rest,
                "rest_edge": home_rest - away_rest,
                "weather_temp_c": np.nan,
                "weather_wind_mps": np.nan,
                "weather_available": 0.0,
                "sample_size": min(home_state["n"], away_state["n"]),
                "home_games_prior": home_state["n"],
                "away_games_prior": away_state["n"],
                "spread_line": np.nan,
                "home_spread_price": np.nan,
                "away_spread_price": np.nan,
            }
        )

    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    if not spreads.empty:
        frame = frame.drop(columns=["spread_line", "home_spread_price", "away_spread_price"], errors="ignore")
        frame = frame.merge(spreads, on="game_id", how="left")
        frame = frame.rename(
            columns={
                "home_price_american": "home_spread_price",
                "away_price_american": "away_spread_price",
            }
        )
    if not weather.empty:
        frame = frame.drop(columns=["weather_temp_c", "weather_wind_mps"], errors="ignore")
        frame = frame.merge(weather, on="game_id", how="left")
        frame["weather_available"] = frame["weather_temp_c"].notna().astype(float)
        med_temp = float(frame["weather_temp_c"].median()) if frame["weather_temp_c"].notna().any() else 18.0
        frame["weather_temp_c"] = frame["weather_temp_c"].fillna(med_temp)
        frame["weather_wind_mps"] = frame["weather_wind_mps"].fillna(0.0)
    if league_id == "nfl":
        from spe.models.ats.injuries import INJURY_FEATURES, load_nfl_injury_features

        injuries = load_nfl_injury_features(session)
        frame = frame.merge(injuries, on="game_id", how="left")
        for col in INJURY_FEATURES:
            if col not in frame.columns:
                frame[col] = 0.0
            frame[col] = frame[col].fillna(0.0)
    # silence unused
    _ = pending_updates
    _ = prior_only
    return frame


def _rest_days(kickoffs: list[pd.Timestamp], as_of: pd.Timestamp) -> float:
    prior = [k for k in kickoffs if k < as_of]
    if not prior:
        return 7.0
    delta = (as_of - prior[-1]).total_seconds() / 86400.0
    return float(np.clip(delta, 3.0, 21.0))


def _num(value: Any, default: float) -> float:
    try:
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
