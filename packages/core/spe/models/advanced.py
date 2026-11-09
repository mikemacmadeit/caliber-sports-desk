"""Research models on features already stored in Caliber.

M3 artifacts are not replaced. Missing measurements stay missing.
A feature is kept only when it helps on every selection season.
The last completed season is scored after that choice is frozen.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.analytics.definitions import SHRINKAGE_K
from spe.config import settings
from spe.models.metrics import apply_platt, brier_score, expected_calibration_error, log_loss, mean_absolute_error, platt_calibrate

SELECTION_YEARS = (2022, 2023)
REPORT_YEARS = (2024, 2025)
MARKETS = ("opportunity", "anytime_td", "receptions", "receiving_yards", "rushing_yards")
MIN_GAIN = {
    "anytime_td": 0.001,
    "opportunity": 0.02,
    "receptions": 0.02,
    "receiving_yards": 0.15,
    "rushing_yards": 0.15,
}
MIN_COVERAGE = 0.30


def feature_catalog() -> dict[str, Any]:
    """What each league can actually support. Zeros are not treated as measurements."""
    return {
        "nfl": {
            "snap_participation": "usage_pregame offense_snap_share_roll3, prior games only",
            "target_share": "prior mean of targets divided by team targets",
            "carry_share": "prior mean of carries divided by team carries",
            "red_zone_targets": "prior mean from player game stats",
            "goal_line_carries": "prior mean from player game stats",
            "recent_workload": "prior 3-game mean minus prior 10-game mean",
            "offensive_efficiency": "prior team offensive EPA per play",
            "defensive_efficiency": "prior opponent defensive EPA per play",
            "opponent_adjusted": "prior opponent-adjusted defensive EPA",
            "pace": "prior team seconds per play",
            "home_field": "team is the home team; the box score has no team, so only a unique roster-week team that is in the game is used",
            "pregame_weather": "rejected: Open-Meteo issued_at is inferred as the valid hour minus 24 hours",
            "availability": "not used: box-score rows are players who played, so an out label cannot be scored",
            "depth_chart": "usage_pregame depth rank, starter flag, and roster status from an earlier chart",
            "separation": "prior-week NGS average separation, cushion, and yards after catch above expectation",
            "rush_yards_over_expected": "prior-week NGS rush yards over expected",
            "formation": "prior-game shotgun share and zone share",
        },
        "fbs": {
            "snap_participation": "not in source",
            "target_share": "not separate from receptions; CFBD box sets targets equal to receptions on every row",
            "carry_share": "prior mean of carries divided by team carries",
            "red_zone_targets": "not in source; stored zeros are the column default",
            "goal_line_carries": "not in source; stored zeros are the column default",
            "recent_workload": "prior 3-game mean minus prior 10-game mean of the market count",
            "offensive_efficiency": "prior team offensive PPA stored in the EPA-named column",
            "defensive_efficiency": "prior opponent defensive PPA",
            "opponent_adjusted": "prior opponent-adjusted defensive PPA",
            "pace": "prior team seconds per play",
            "home_field": "team is the home team",
            "pregame_weather": "rejected: no historical forecast has a source issue timestamp",
            "availability": "not used: one college injury row",
            "depth_chart": "not in source",
        },
        "excluded_everywhere": [
            "Open-Meteo previous-day values have no API issue timestamp; stored issued_at is inferred",
            "NWS forecasts are live only and are not a historical backtest",
            "nflverse schedule weather is observed_not_forecast",
            "NOAA daily observations are issued after the local observation day",
            "GDELT headlines are not verified and are not linked to players",
            "NGS week-zero totals are season aggregates",
            "same-game EPA, snap counts, and target shares",
        ],
    }


def add_player_priors(frame: pd.DataFrame, league_id: str) -> pd.DataFrame:
    """Shift every player measurement by one game. The current game is not a feature."""
    out = frame.sort_values(["player_id", "kickoff_utc"]).copy()
    if league_id == "fbs":
        for col in ("rz_targets", "rz_carries", "gl_carries"):
            if col in out.columns:
                out[col] = np.nan
    group = out.groupby("player_id", sort=False)
    for src, dest in (
        ("targets", "prior_targets"),
        ("receptions", "prior_receptions"),
        ("carries", "prior_carries"),
        ("rec_yards", "prior_rec_yards"),
        ("rush_yards", "prior_rush_yards"),
        ("rz_targets", "prior_rz_targets"),
        ("gl_carries", "prior_gl_carries"),
        ("target_share", "prior_target_share"),
        ("carry_share", "prior_carry_share"),
        ("anytime_tds", "prior_td_rate"),
    ):
        if src not in out.columns:
            out[dest] = np.nan
            continue
        out[dest] = group[src].transform(lambda s: s.shift(1).expanding(min_periods=1).mean())
    volume = out["targets"].fillna(0) + out["carries"].fillna(0)
    out["_volume"] = volume
    vol_group = out.groupby("player_id", sort=False)["_volume"]
    roll3 = vol_group.transform(lambda s: s.shift(1).rolling(3, min_periods=1).mean())
    roll10 = vol_group.transform(lambda s: s.shift(1).rolling(10, min_periods=3).mean())
    out["workload_delta"] = roll3 - roll10
    out.loc[roll10.isna(), "workload_delta"] = np.nan
    return out.drop(columns=["_volume"])


def market_spec(market_id: str) -> dict[str, Any]:
    specs = {
        "opportunity": {"count": "targets", "positions": {"WR", "TE", "RB"}, "line": 4.5, "binary": False, "yards": False},
        "anytime_td": {"count": "anytime_tds", "positions": {"WR", "TE", "RB", "QB"}, "line": 0.5, "binary": True, "yards": False},
        "receptions": {"count": "receptions", "positions": {"WR", "TE", "RB"}, "line": 4.5, "binary": False, "yards": False},
        "receiving_yards": {"count": "rec_yards", "positions": {"WR", "TE", "RB"}, "line": 49.5, "binary": False, "yards": True},
        "rushing_yards": {"count": "rush_yards", "positions": {"RB", "QB"}, "line": 39.5, "binary": False, "yards": True},
    }
    return specs[market_id]


def candidate_features(league_id: str, market_id: str) -> list[str]:
    common = ["is_home", "own_off_epa", "opp_adj_def", "pace", "workload_delta", "team_rz_td", "team_points_per_opp"]
    if market_id in {"opportunity", "receptions", "receiving_yards", "anytime_td"}:
        common.append("prior_target_share")
    if market_id in {"opportunity", "rushing_yards", "anytime_td"}:
        common.append("prior_carry_share")
    common.append("usage_x_defense")
    if league_id == "nfl":
        common.extend(["snap_roll3", "snap_delta", "depth_rank", "starter", "roster_active", "shotgun_share", "zone_share"])
        if market_id in {"opportunity", "receptions", "receiving_yards", "anytime_td"}:
            common.extend(["prior_rz_targets", "ngs_separation", "ngs_cushion", "ngs_yac_oe"])
        if market_id in {"rushing_yards", "anytime_td", "opportunity"}:
            common.append("ngs_ryoe")
        if market_id in {"rushing_yards", "anytime_td"}:
            common.append("prior_gl_carries")
    return common


def keep_feature(gains: list[float], minimum: float) -> bool:
    """A feature has to help on every selection season by at least the minimum."""
    return bool(gains) and all(gain >= minimum for gain in gains)


def _z_fit(train: pd.Series) -> tuple[float, float]:
    observed = train[np.isfinite(train.to_numpy(dtype=float))]
    if observed.empty:
        return 0.0, 1.0
    mu = float(observed.mean())
    sd = float(observed.std(ddof=0))
    if sd < 1e-8:
        sd = 1.0
    return mu, sd


def _z_apply(values: pd.Series, mu: float, sd: float) -> np.ndarray:
    raw = values.to_numpy(dtype=float)
    z = (raw - mu) / sd
    return np.where(np.isfinite(z), z, 0.0)


def coverage(values: pd.Series) -> float:
    raw = values.to_numpy(dtype=float)
    if len(raw) == 0:
        return 0.0
    return float(np.isfinite(raw).mean())


def fit_log_beta(base: np.ndarray, feature: np.ndarray, y: np.ndarray) -> float:
    """One train-only multiplier. Missing feature rows are not given a fake value."""
    mask = np.isfinite(base) & np.isfinite(feature) & np.isfinite(y) & (base > 0)
    if int(mask.sum()) < 80:
        return 0.0
    resid = np.log(np.clip(y[mask], 0, None) + 0.1) - np.log(base[mask] + 0.1)
    denom = float(np.dot(feature[mask], feature[mask])) + 1.0
    beta = float(np.dot(feature[mask], resid) / denom)
    return float(np.clip(beta, -0.5, 0.5))


def apply_beta(base: np.ndarray, feature: np.ndarray, beta: float) -> np.ndarray:
    adjusted = np.asarray(base, dtype=float) * np.exp(np.clip(beta * feature, -0.35, 0.35))
    return np.clip(adjusted, 1e-6, None)


def shrinkage_mean(train: pd.DataFrame, test: pd.DataFrame, count_col: str, league_id: str) -> tuple[np.ndarray, np.ndarray]:
    """Same shrinkage the served M3 path uses. Rates are frozen at the train cutoff."""
    k = SHRINKAGE_K[league_id]
    y = train[count_col].fillna(0)
    league = float(y.mean()) if len(train) else 0.0
    pos = y.groupby(train["position_group"]).mean().to_dict()
    team = y.groupby(train["team_id"].fillna("")).mean().to_dict()
    player = y.groupby(train["player_id"]).agg(["mean", "count"])
    rates = player["mean"].to_dict()
    counts = player["count"].to_dict()

    def _row_mean(frame: pd.DataFrame) -> np.ndarray:
        prior = frame["position_group"].map(pos).fillna(league).to_numpy(dtype=float)
        team_prior = frame["team_id"].fillna("").map(team).to_numpy(dtype=float)
        team_prior = np.where(np.isfinite(team_prior), team_prior, prior)
        n = frame["player_id"].map(counts).fillna(0).to_numpy(dtype=float)
        raw = frame["player_id"].map(rates).to_numpy(dtype=float)
        raw = np.where(np.isfinite(raw), raw, team_prior)
        shrink = np.where(n > 0, n / (n + k), 0.0)
        return np.clip(shrink * raw + (1.0 - shrink) * team_prior, 1e-6, None)

    return _row_mean(train), _row_mean(test)


def opponent_adjustment(train: pd.DataFrame, test: pd.DataFrame, train_mean: np.ndarray, test_mean: np.ndarray, count_col: str) -> tuple[np.ndarray, np.ndarray, float]:
    z_train = train["opp_def_z"].to_numpy(dtype=float)
    y = train[count_col].fillna(0).to_numpy(dtype=float)
    beta = fit_log_beta(train_mean, np.where(np.isfinite(z_train), z_train, 0.0), y)
    return apply_beta(train_mean, np.where(np.isfinite(z_train), z_train, 0.0), beta), apply_beta(
        test_mean, np.where(np.isfinite(test["opp_def_z"].to_numpy(dtype=float)), test["opp_def_z"].to_numpy(dtype=float), 0.0), beta
    ), beta


def poisson_over_array(lam: np.ndarray, line: float) -> np.ndarray:
    k = int(np.floor(line))
    lam = np.clip(np.asarray(lam, dtype=float), 1e-6, None)
    term = np.exp(-lam)
    cdf = term.copy()
    for i in range(k):
        term = term * lam / (i + 1)
        cdf = cdf + term
    return np.clip(1.0 - cdf, 1e-6, 1.0 - 1e-6)


def _norm_sf(line: float, mean: np.ndarray, sigma: float) -> np.ndarray:
    import math

    mean = np.asarray(mean, dtype=float)
    z = (line - mean) / (sigma * math.sqrt(2))
    cdf = 0.5 * (1.0 + np.array([math.erf(float(v)) for v in z]))
    return np.clip(1.0 - cdf, 1e-6, 1.0 - 1e-6)


def _lognormal_sf(line: float, mean: np.ndarray, sigma: float) -> np.ndarray:
    import math

    mean = np.clip(np.asarray(mean, dtype=float), 1e-6, None)
    mu = np.log(mean + 1.0) - 0.5 * sigma * sigma
    z = (np.log(line + 1.0) - mu) / (sigma * math.sqrt(2))
    cdf = 0.5 * (1.0 + np.array([math.erf(float(v)) for v in z]))
    return np.clip(1.0 - cdf, 1e-6, 1.0 - 1e-6)


def over_probability(mean: np.ndarray, y_train: np.ndarray, line: float, *, yards: bool, family: str) -> np.ndarray:
    if family == "lognormal":
        resid = np.log(np.clip(y_train, 0, None) + 1.0) - np.log(np.clip(mean[: len(y_train)], 1e-6, None) + 1.0)
        sigma = float(np.nanstd(resid)) if len(resid) else 0.4
        return _lognormal_sf(line, mean, max(sigma, 0.15))
    if yards or family == "gaussian":
        sigma = float(np.nanstd(y_train - mean[: len(y_train)])) if len(y_train) else 1.0
        return _norm_sf(line, mean, max(sigma, 1.0))
    return poisson_over_array(mean, line)


def binary_probability(mean: np.ndarray) -> np.ndarray:
    return np.clip(1.0 - np.exp(-np.clip(mean, 1e-6, None)), 1e-6, 1.0 - 1e-6)


def score_block(y: np.ndarray, mean: np.ndarray, p: np.ndarray, *, binary: bool, line: float) -> dict[str, float]:
    if binary:
        y_bin = (y > 0).astype(float)
        return {
            "n": float(len(y)),
            "brier": brier_score(y_bin, p),
            "log_loss": log_loss(y_bin, p),
            "ece": expected_calibration_error(y_bin, p),
            "mae": mean_absolute_error(y, mean),
        }
    y_over = (y > line).astype(float)
    return {
        "n": float(len(y)),
        "brier": brier_score(y_over, p),
        "log_loss": log_loss(y_over, p),
        "ece": expected_calibration_error(y_over, p),
        "mae": mean_absolute_error(y, mean),
    }


def by_position(frame: pd.DataFrame, y: np.ndarray, mean: np.ndarray, p: np.ndarray, *, binary: bool, line: float) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    positions = frame["position_group"].to_numpy()
    for pos in sorted(set(positions)):
        mask = positions == pos
        if int(mask.sum()) < 20:
            continue
        out[str(pos)] = score_block(y[mask], mean[mask], p[mask], binary=binary, line=line)
    return out


def _prepare_matrix(train: pd.DataFrame, test: pd.DataFrame, columns: list[str]) -> tuple[np.ndarray, np.ndarray, list[str]]:
    kept: list[str] = []
    train_cols: list[np.ndarray] = []
    test_cols: list[np.ndarray] = []
    for col in columns:
        if col not in train.columns or coverage(train[col]) < MIN_COVERAGE:
            continue
        mu, sd = _z_fit(train[col])
        train_cols.append(_z_apply(train[col], mu, sd))
        test_cols.append(_z_apply(test[col], mu, sd))
        kept.append(col)
    if not kept:
        return np.zeros((len(train), 0)), np.zeros((len(test), 0)), []
    return np.column_stack(train_cols), np.column_stack(test_cols), kept


def primary_loss(metrics: dict[str, float], market_id: str) -> float:
    if market_id == "anytime_td":
        return metrics["brier"]
    return metrics["mae"]


def season_mask(frame: pd.DataFrame, year: int, *, train: bool) -> pd.Series:
    kick = pd.to_datetime(frame["kickoff_utc"], utc=True)
    start = pd.Timestamp(f"{year}-08-01", tz="UTC")
    end = pd.Timestamp(f"{year + 1}-03-01", tz="UTC")
    if train:
        return kick < start
    return (kick >= start) & (kick < end)


def assign_roster_team(stats: pd.DataFrame, roster: pd.DataFrame) -> pd.DataFrame:
    """Fill a missing team only when one roster-week team is actually in that game."""
    out = stats.copy()
    roster = roster.copy()
    if roster.empty or "week" not in out.columns:
        return out
    out["week"] = pd.to_numeric(out["week"], errors="coerce")
    roster["week"] = pd.to_numeric(roster["week"], errors="coerce")
    out["season_year"] = pd.to_numeric(out["season_year"], errors="coerce")
    roster["season_year"] = pd.to_numeric(roster["season_year"], errors="coerce")
    counts = roster.groupby(["player_id", "season_year", "week"])["team_id"].nunique()
    unique_keys = counts[counts == 1].reset_index()[["player_id", "season_year", "week"]]
    one = roster.merge(unique_keys, on=["player_id", "season_year", "week"]).drop_duplicates(["player_id", "season_year", "week"])
    merged = out.merge(
        one[["player_id", "season_year", "week", "team_id"]],
        on=["player_id", "season_year", "week"],
        how="left",
        suffixes=("", "_roster"),
    )
    roster_team = merged["team_id_roster"]
    on_game = roster_team.eq(merged["home_team_id"]) | roster_team.eq(merged["away_team_id"])
    fill = merged["team_id"].isna() & on_game
    merged.loc[fill, "team_id"] = roster_team[fill]
    return merged.drop(columns=["team_id_roster"])


def _with_pregame_universe(
    session: Session, stats: pd.DataFrame, league_id: str, year_max: int
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Add recorded zeros and pregame-expected absences. Same-week roster status and snaps stay out."""
    from spe.models.eligibility import SKILL, absence_rows, include_in_corrected, label_stat_rows, summarize_eligibility

    roster = pd.DataFrame(columns=["player_id", "season_year", "week", "team_id", "status"])
    if league_id == "nfl":
        roster = pd.read_sql(
            text(
                """
                SELECT player_id, season_year, week, team_id, status
                FROM roster_weeks
                WHERE season_year <= :year_max
                """
            ),
            session.connection(),
            params={"year_max": int(year_max)},
        )
    injuries = pd.read_sql(
        text(
            """
            SELECT i.game_id, i.player_id, i.designation, i.reported_at
            FROM injury_events i
            JOIN games g ON g.game_id = i.game_id
            WHERE g.league_id = :lg AND i.reported_at < g.kickoff_utc
            """
        ),
        session.connection(),
        params={"lg": league_id},
    )
    games = pd.read_sql(
        text(
            """
            SELECT g.game_id, s.year AS season_year, g.week, g.home_team_id, g.away_team_id, g.kickoff_utc
            FROM games g
            JOIN seasons s ON s.season_id = g.season_id
            WHERE g.league_id = :lg AND g.data_tier = 'historical' AND g.status = 'final'
              AND s.year <= :year_max
            """
        ),
        session.connection(),
        params={"lg": league_id, "year_max": int(year_max)},
    )
    players = pd.read_sql(text("SELECT player_id, position_group FROM players"), session.connection())
    labeled = label_stat_rows(stats, roster, injuries)
    added = absence_rows(games, roster, injuries, stats[["game_id", "player_id"]].drop_duplicates())
    if not added.empty:
        added = added.merge(players, on="player_id", how="left")
    combined = pd.concat([labeled, added], ignore_index=True) if not added.empty else labeled
    summary = summarize_eligibility(combined)
    summary["roster_source"] = "prior_week_only" if league_id == "nfl" else "not_in_source"
    summary["same_week_roster_status_used"] = False
    summary["snap_counts_used"] = False
    summary["confirmed_active_pregame"] = 0
    if league_id != "nfl":
        summary["note"] = "FBS has no weekly roster, so players missing from the box score stay unknown and are not added."
    participation = session.execute(
        text(
            """
            SELECT count(*)
            FROM participation_player_games p
            JOIN games g ON g.game_id = p.game_id
            WHERE g.league_id = :lg
            """
        ),
        {"lg": league_id},
    ).scalar()
    summary["postgame_participation_rows_not_used"] = int(participation or 0)
    keep = [
        str(pos) in SKILL and include_in_corrected(bool(has_stat), str(label))
        for pos, has_stat, label in zip(combined["position_group"], combined["has_stat_row"], combined["availability"], strict=False)
    ]
    kept = combined.loc[keep].copy()
    kept["anytime_tds"] = kept["rush_td"].fillna(0) + kept["rec_td"].fillna(0)
    return kept, summary


def load_model_frame(session: Session, league_id: str, *, year_max: int = 2025, universe: str = "involved") -> pd.DataFrame:
    stats = pd.read_sql(
        text(
            """
            SELECT p.player_id, p.game_id, p.team_id, p.targets, p.receptions, p.rec_yards,
                   p.rush_yards, p.carries, p.rz_targets, p.rz_carries, p.gl_carries,
                   p.rush_td, p.rec_td, g.kickoff_utc, g.week, g.home_team_id, g.away_team_id,
                   s.year AS season_year, pl.position_group
            FROM player_game_stats p
            JOIN games g ON g.game_id = p.game_id
            JOIN seasons s ON s.season_id = g.season_id
            LEFT JOIN players pl ON pl.player_id = p.player_id
            WHERE g.league_id = :lg AND g.data_tier = 'historical' AND g.status = 'final'
              AND s.year <= :year_max
            """
        ),
        session.connection(),
        params={"lg": league_id, "year_max": int(year_max)},
    )
    if stats.empty:
        return stats
    stats["kickoff_utc"] = pd.to_datetime(stats["kickoff_utc"], utc=True)
    stats["anytime_tds"] = stats["rush_td"].fillna(0) + stats["rec_td"].fillna(0)
    stats["position_group"] = stats["position_group"].fillna("UNK")
    if league_id == "nfl":
        roster = pd.read_sql(
            text("SELECT player_id, season_year, week, team_id FROM roster_weeks WHERE season_year <= :year_max"),
            session.connection(),
            params={"year_max": int(year_max)},
        )
        stats = assign_roster_team(stats, roster)
    eligibility: dict[str, Any] | None = None
    if universe == "corrected":
        stats, eligibility = _with_pregame_universe(session, stats, league_id, year_max)
    team_targets = stats.groupby(["game_id", "team_id"], dropna=True)["targets"].transform("sum")
    team_carries = stats.groupby(["game_id", "team_id"], dropna=True)["carries"].transform("sum")
    stats["target_share"] = np.where(team_targets > 0, stats["targets"] / team_targets, np.nan)
    stats["carry_share"] = np.where(team_carries > 0, stats["carries"] / team_carries, np.nan)
    if league_id == "fbs":
        stats["target_share"] = np.nan
    # Shares use full game box scores (team denominators). Priors must be computed on the
    # same row universe the model sees — otherwise expanding means include games that are
    # later dropped and diverge from live scoring over the filtered hist frame.
    if universe != "corrected":
        involved = (
            stats["targets"].fillna(0)
            + stats["receptions"].fillna(0)
            + stats["carries"].fillna(0)
            + stats["rec_yards"].fillna(0).abs()
            + stats["rush_yards"].fillna(0).abs()
        )
        stats = stats.loc[involved > 0].copy()
    stats = add_player_priors(stats, league_id)
    stats = _attach_team_environment(session, stats, league_id)
    stats = _attach_usage(session, stats, league_id)
    stats = _attach_weather(session, stats)
    stats["is_home"] = np.where(stats["team_id"].notna() & (stats["team_id"] == stats["home_team_id"]), 1.0, np.where(stats["team_id"].notna(), 0.0, np.nan))
    share = stats["prior_target_share"].fillna(stats["prior_carry_share"])
    stats["usage_x_defense"] = share * stats["opp_def_z"]
    stats.loc[share.isna() | stats["opp_def_z"].isna(), "usage_x_defense"] = np.nan
    if universe == "corrected":
        out = stats.reset_index(drop=True)
        if eligibility is not None:
            out.attrs["eligibility"] = eligibility
        return out
    return stats.reset_index(drop=True)


def _attach_team_environment(session: Session, stats: pd.DataFrame, league_id: str) -> pd.DataFrame:
    team = pd.read_sql(
        text(
            """
            SELECT t.team_id, t.game_id,
                   json_extract(t.metrics, '$.off_epa_per_play') AS off_epa,
                   json_extract(t.metrics, '$.def_epa_per_play') AS def_epa,
                   json_extract(t.metrics, '$.adj_def_epa') AS adj_def,
                   json_extract(t.metrics, '$.pace_sec_per_play') AS pace,
                   json_extract(t.metrics, '$.red_zone_td_rate') AS rz_td,
                   json_extract(t.metrics, '$.points_per_scoring_opp') AS points_per_opp,
                   g.kickoff_utc
            FROM team_analytics t
            JOIN games g ON g.game_id = t.game_id
            WHERE t.league_id = :lg AND t.grain = 'game'
            """
        ),
        session.connection(),
        params={"lg": league_id},
    )
    if team.empty:
        for col in ("own_off_epa", "opp_def_z", "opp_adj_def", "pace", "team_rz_td", "team_points_per_opp"):
            stats[col] = np.nan
        return stats
    team["kickoff_utc"] = pd.to_datetime(team["kickoff_utc"], utc=True)
    team = team.sort_values(["team_id", "kickoff_utc"])
    group = team.groupby("team_id", sort=False)
    for src, dest in (
        ("off_epa", "prior_off"),
        ("def_epa", "prior_def"),
        ("adj_def", "prior_adj_def"),
        ("pace", "prior_pace"),
        ("rz_td", "prior_rz_td"),
        ("points_per_opp", "prior_points_per_opp"),
    ):
        if src not in team.columns:
            team[dest] = np.nan
            continue
        team[dest] = group[src].transform(lambda s: pd.to_numeric(s, errors="coerce").shift(1).expanding(min_periods=1).mean())
    own = team[["game_id", "team_id", "prior_off", "prior_pace", "prior_rz_td", "prior_points_per_opp"]].rename(
        columns={"prior_off": "own_off_epa", "prior_pace": "pace", "prior_rz_td": "team_rz_td", "prior_points_per_opp": "team_points_per_opp"}
    ).drop_duplicates(["game_id", "team_id"])
    opp = team[["game_id", "team_id", "prior_def", "prior_adj_def"]].rename(
        columns={"team_id": "opp_id", "prior_def": "opp_def", "prior_adj_def": "opp_adj_def"}
    ).drop_duplicates(["game_id", "opp_id"])
    stats = stats.merge(own, on=["game_id", "team_id"], how="left")
    stats["opp_id"] = np.where(stats["team_id"] == stats["home_team_id"], stats["away_team_id"], stats["home_team_id"])
    stats = stats.merge(opp, on=["game_id", "opp_id"], how="left")
    for col in ("own_off_epa", "pace", "opp_def", "opp_adj_def", "team_rz_td", "team_points_per_opp"):
        if col in stats.columns:
            stats[col] = pd.to_numeric(stats[col], errors="coerce")
    mu = float(np.nanmean(stats["opp_def"])) if stats["opp_def"].notna().any() else 0.0
    sd = float(np.nanstd(stats["opp_def"])) if stats["opp_def"].notna().any() else 1.0
    if sd < 1e-8:
        sd = 1.0
    stats["opp_def_z"] = (stats["opp_def"] - mu) / sd
    return stats


def _attach_usage(session: Session, stats: pd.DataFrame, league_id: str) -> pd.DataFrame:
    for col in ("snap_roll3", "snap_delta", "depth_rank", "starter", "roster_active", "ngs_separation", "ngs_cushion", "ngs_yac_oe", "ngs_ryoe", "shotgun_share", "zone_share"):
        stats[col] = np.nan
    if league_id != "nfl":
        return stats
    usage = pd.read_sql(
        text(
            """
            SELECT player_id, game_id,
                   json_extract(metrics, '$.offense_snap_share_roll3') AS snap_roll3,
                   json_extract(metrics, '$.offense_snap_share_delta_3v10') AS snap_delta,
                   json_extract(metrics, '$.depth_rank_prior_week') AS depth_rank,
                   json_extract(metrics, '$.starter_prior_week') AS starter,
                   json_extract(metrics, '$.roster_status_prior_week') AS roster_status,
                   json_extract(metrics, '$.ngs_avg_separation') AS ngs_separation,
                   json_extract(metrics, '$.ngs_avg_cushion') AS ngs_cushion,
                   json_extract(metrics, '$.ngs_avg_yac_above_expectation') AS ngs_yac_oe,
                   json_extract(metrics, '$.ngs_rush_yards_over_expected') AS ngs_ryoe
            FROM player_analytics
            WHERE league_id = 'nfl' AND grain = 'usage_pregame'
            """
        ),
        session.connection(),
    )
    if not usage.empty:
        for col in ("snap_roll3", "snap_delta", "depth_rank", "starter", "ngs_separation", "ngs_cushion", "ngs_yac_oe", "ngs_ryoe"):
            usage[col] = pd.to_numeric(usage[col], errors="coerce")
        usage["roster_active"] = [
            np.nan if value is None or str(value).strip().lower() in {"", "none", "nan", "<na>"} else (1.0 if str(value).strip().upper() == "ACT" else 0.0)
            for value in usage["roster_status"]
        ]
        usage = usage.drop(columns=["roster_status"]).drop_duplicates(["player_id", "game_id"])
        stats = stats.drop(columns=[col for col in usage.columns if col in stats.columns and col not in {"player_id", "game_id"}]).merge(
            usage, on=["player_id", "game_id"], how="left"
        )
    formation = pd.read_sql(
        text(
            """
            SELECT team_id, game_id,
                   json_extract(metrics, '$.shotgun_share') AS shotgun_share,
                   json_extract(metrics, '$.zone_share') AS zone_share
            FROM team_analytics
            WHERE league_id = 'nfl' AND grain = 'usage_pregame' AND source = 'caliber_usage_v1'
            """
        ),
        session.connection(),
    )
    if formation.empty:
        return stats
    for col in ("shotgun_share", "zone_share"):
        formation[col] = pd.to_numeric(formation[col], errors="coerce")
    formation = formation.drop_duplicates(["team_id", "game_id"])
    stats = stats.drop(columns=["shotgun_share", "zone_share"], errors="ignore").merge(formation, on=["team_id", "game_id"], how="left")
    return stats


def _attach_weather(session: Session, stats: pd.DataFrame) -> pd.DataFrame:
    """Historical Open-Meteo rows are not used. Their issue time was computed, not supplied."""
    del session
    stats["wind_mps"] = np.nan
    return stats


def _zscore_defense(train: pd.DataFrame, test: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Standardize opponent defense on the training seasons only."""
    train = train.copy()
    test = test.copy()
    if "opp_def" not in train.columns:
        train["opp_def_z"] = 0.0
        test["opp_def_z"] = 0.0
        return train, test
    mu, sd = _z_fit(train["opp_def"])
    train["opp_def_z"] = _z_apply(train["opp_def"], mu, sd)
    test["opp_def_z"] = _z_apply(test["opp_def"], mu, sd)
    for frame in (train, test):
        share = frame["prior_target_share"].fillna(frame["prior_carry_share"]) if "prior_target_share" in frame.columns else frame.get("prior_carry_share")
        if share is None:
            frame["usage_x_defense"] = np.nan
            continue
        frame["usage_x_defense"] = share.to_numpy(dtype=float) * frame["opp_def_z"].to_numpy(dtype=float)
        frame.loc[share.isna(), "usage_x_defense"] = np.nan
    return train, test


def _calibrated_binary(train_p: np.ndarray, y_train: np.ndarray, test_p: np.ndarray) -> np.ndarray:
    y_bin = (y_train > 0).astype(float)
    a, b = platt_calibrate(train_p, y_bin)
    return apply_platt(test_p, a, b)


def evaluate_split(
    train: pd.DataFrame,
    test: pd.DataFrame,
    league_id: str,
    market_id: str,
    extra: list[str],
    family: str,
    positions: set[str] | None = None,
) -> dict[str, Any] | None:
    spec = market_spec(market_id)
    count_col = "receptions" if league_id == "fbs" and market_id == "opportunity" else spec["count"]
    positions = positions or spec["positions"]
    train = train[train["position_group"].isin(positions)].copy()
    test = test[test["position_group"].isin(positions)].copy()
    if len(train) < 200 or len(test) < 50:
        return None
    train, test = _zscore_defense(train, test)
    base_tr, base_te = shrinkage_mean(train, test, count_col, league_id)
    m3_tr, m3_te, _beta = opponent_adjustment(train, test, base_tr, base_te, count_col)
    x_tr, x_te, used = _prepare_matrix(train, test, extra)
    new_tr, new_te = m3_tr.copy(), m3_te.copy()
    betas: dict[str, float] = {}
    y_tr = train[count_col].fillna(0).to_numpy(dtype=float)
    for i, name in enumerate(used):
        beta = fit_log_beta(new_tr, x_tr[:, i], y_tr)
        betas[name] = beta
        if beta == 0.0:
            continue
        new_tr = apply_beta(new_tr, x_tr[:, i], beta)
        new_te = apply_beta(new_te, x_te[:, i], beta)
    y_te = test[count_col].fillna(0).to_numpy(dtype=float)
    line = float(spec["line"])
    if spec["binary"]:
        p_m3 = _calibrated_binary(binary_probability(m3_tr), y_tr, binary_probability(m3_te))
        p_new = _calibrated_binary(binary_probability(new_tr), y_tr, binary_probability(new_te))
    else:
        p_m3 = over_probability(np.concatenate([m3_tr, m3_te]), y_tr, line, yards=spec["yards"], family="gaussian" if spec["yards"] else "poisson")
        p_m3 = p_m3[len(m3_tr) :]
        chosen = family if spec["yards"] else "poisson"
        p_new = over_probability(np.concatenate([new_tr, new_te]), y_tr, line, yards=spec["yards"], family=chosen)
        p_new = p_new[len(new_tr) :]
        if spec["yards"]:
            p_m3_train = over_probability(m3_tr, y_tr, line, yards=True, family="gaussian")
            # Calibration of the over probability is fit on train labels only.
            p_m3 = apply_platt(p_m3, *platt_calibrate(p_m3_train, (y_tr > line).astype(float)))
            p_new_train = over_probability(new_tr, y_tr, line, yards=True, family=chosen)
            p_new = apply_platt(p_new, *platt_calibrate(p_new_train, (y_tr > line).astype(float)))
        else:
            p_train = poisson_over_array(m3_tr, line)
            p_m3 = apply_platt(p_m3, *platt_calibrate(p_train, (y_tr > line).astype(float)))
            p_new_train = poisson_over_array(new_tr, line)
            p_new = apply_platt(p_new, *platt_calibrate(p_new_train, (y_tr > line).astype(float)))
    return {
        "features": [name for name, beta in betas.items() if beta != 0.0],
        "m3": score_block(y_te, m3_te, p_m3, binary=spec["binary"], line=line),
        "new": score_block(y_te, new_te, p_new, binary=spec["binary"], line=line),
        "m3_by_position": by_position(test, y_te, m3_te, p_m3, binary=spec["binary"], line=line),
        "new_by_position": by_position(test, y_te, new_te, p_new, binary=spec["binary"], line=line),
    }


def select_features(frame: pd.DataFrame, league_id: str, market_id: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Greedy ablation. A feature is kept only when both selection seasons clear the minimum."""
    chosen: list[str] = []
    decisions: list[dict[str, Any]] = []
    minimum = MIN_GAIN[market_id]
    for feature in candidate_features(league_id, market_id):
        gains: list[float] = []
        coverages: dict[str, float] = {}
        for year in SELECTION_YEARS:
            train = frame[season_mask(frame, year, train=True)]
            coverages[str(year)] = coverage(train[feature]) if feature in train.columns else 0.0
        if max(coverages.values(), default=0.0) < MIN_COVERAGE:
            decisions.append(
                {
                    "feature": feature,
                    "kept": False,
                    "minimum_gain": minimum,
                    "gains": {},
                    "train_coverage": coverages,
                    "reason": "below 30% coverage on the selection seasons; missing values were not filled",
                }
            )
            continue
        for year in SELECTION_YEARS:
            train = frame[season_mask(frame, year, train=True)]
            test = frame[season_mask(frame, year, train=False)]
            base = evaluate_split(train, test, league_id, market_id, chosen, "gaussian")
            trial = evaluate_split(train, test, league_id, market_id, [*chosen, feature], "gaussian")
            if base is None or trial is None:
                gains = []
                break
            gains.append(primary_loss(base["new"], market_id) - primary_loss(trial["new"], market_id))
        kept = keep_feature(gains, minimum)
        if kept:
            reason = "improved the primary metric on both 2022 and 2023"
        elif not gains:
            reason = "selection window did not have enough rows"
        elif max(coverages.values(), default=0.0) < MIN_COVERAGE:
            reason = "below 30% coverage on the selection seasons; missing values were not filled"
        else:
            reason = "did not clear the minimum gain on both selection seasons"
        decisions.append(
            {
                "feature": feature,
                "kept": kept,
                "minimum_gain": minimum,
                "gains": {str(year): gain for year, gain in zip(SELECTION_YEARS, gains, strict=False)},
                "train_coverage": coverages,
                "reason": reason,
            }
        )
        if kept:
            chosen.append(feature)
    return chosen, decisions


def choose_yards_family(frame: pd.DataFrame, league_id: str, market_id: str, features: list[str]) -> tuple[str, dict[str, float]]:
    spec = market_spec(market_id)
    if not spec["yards"]:
        return "poisson", {}
    votes = {"gaussian": 0.0, "lognormal": 0.0}
    for year in SELECTION_YEARS:
        train = frame[season_mask(frame, year, train=True)]
        test = frame[season_mask(frame, year, train=False)]
        for family in votes:
            scored = evaluate_split(train, test, league_id, market_id, features, family)
            if scored is None:
                continue
            votes[family] += scored["new"]["brier"]
    if votes["lognormal"] == 0.0 and votes["gaussian"] == 0.0:
        return "gaussian", votes
    family = "lognormal" if votes["lognormal"] < votes["gaussian"] else "gaussian"
    return family, votes


def compare_league(session: Session, league_id: str) -> dict[str, Any]:
    frame = load_model_frame(session, league_id)
    print(f"{league_id} rows {len(frame)}", flush=True)
    markets: dict[str, Any] = {}
    for market_id in MARKETS:
        features, ablation = select_features(frame, league_id, market_id)
        family, distribution_votes = choose_yards_family(frame, league_id, market_id, features)
        windows = []
        for year in (*SELECTION_YEARS, *REPORT_YEARS):
            train = frame[season_mask(frame, year, train=True)]
            test = frame[season_mask(frame, year, train=False)]
            scored = evaluate_split(train, test, league_id, market_id, features, family)
            if scored is None:
                continue
            windows.append({"season": year, "held_out": year in REPORT_YEARS, **scored})
        markets[market_id] = {
            "features": features,
            "distribution": family,
            "distribution_selection_brier": distribution_votes,
            "ablation": ablation,
            "windows": windows,
        }
        print(f"{league_id} {market_id} kept {features}", flush=True)
    return {"league_id": league_id, "rows": int(len(frame)), "markets": markets}


def recommend(report: dict[str, Any]) -> str:
    """Replace M3 only if every market is no worse on the untouched 2025 season."""
    for league in report["leagues"]:
        for market in league["markets"].values():
            final = next((row for row in market["windows"] if row["season"] == 2025), None)
            if final is None:
                return "keep_m3"
            if market["distribution"] == "poisson" and market_id_is_count(market):
                pass
            new_loss = primary_loss(final["new"], _market_of(league, market))
            m3_loss = primary_loss(final["m3"], _market_of(league, market))
            if new_loss > m3_loss + 1e-9:
                return "keep_m3"
    return "candidate_beats_or_ties_m3_do_not_auto_promote"


def _market_of(league: dict[str, Any], market: dict[str, Any]) -> str:
    for name, body in league["markets"].items():
        if body is market:
            return name
    return "receptions"


def market_id_is_count(market: dict[str, Any]) -> bool:
    return market.get("distribution") == "poisson"


def comparison_path() -> Path:
    return Path(settings.parquet_dir) / "models" / "m4_comparison.json"


def write_comparison(report: dict[str, Any], path: Path | None = None) -> Path:
    target = path or comparison_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return target


def run_comparison(session: Session) -> dict[str, Any]:
    """Fit and score research models. Does not write forecasts, odds, or M3 artifacts."""
    from spe.models.feature_audit import audit_sources

    m3_paths = [Path(settings.parquet_dir) / "models" / f"{league}_m3.json" for league in ("nfl", "fbs")]
    before = {path: path.read_bytes() if path.exists() else None for path in m3_paths}
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "source_audit": audit_sources(session),
        "feature_audit": feature_catalog(),
        "selection_seasons": list(SELECTION_YEARS),
        "report_seasons": list(REPORT_YEARS),
        "baseline": "M3 feature set re-fit before each test season: player shrinkage plus opponent adjustment",
        "leagues": [compare_league(session, league) for league in ("nfl", "fbs")],
    }
    report["recommendation"] = recommend(report)
    write_comparison(report)
    for path, payload in before.items():
        after = path.read_bytes() if path.exists() else None
        if after != payload:
            raise RuntimeError(f"M3 artifact changed: {path}")
    return report
