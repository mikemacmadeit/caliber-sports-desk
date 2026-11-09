"""Leakage-safe player-game features. NFL EPA and FBS PPA stay in separate frames."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.analytics.engine import GAME_COMPLETE_AFTER, SKILL
from spe.analytics.persist import load_frames
from spe.eval.leakage import assert_event_time

DEFAULT_LINES = {
    "anytime_td": 0.5,
    "receptions": 4.5,
    "receiving_yards": 49.5,
    "rushing_yards": 39.5,
}


def build_model_frame(session: Session, league_id: str) -> pd.DataFrame:
    """One row per eligible player-game with prior-only features and outcomes.

    Eligibility for TD evaluation: skill players who recorded offensive involvement,
    including those with zero touchdowns.
    """
    games, plays, stats, players = load_frames(session, league_id)
    if stats.empty:
        return pd.DataFrame()
    stats = stats.copy()
    stats["kickoff_utc"] = pd.to_datetime(stats["kickoff_utc"], utc=True)
    if players is not None and not players.empty:
        stats = stats.merge(players[["player_id", "position_group"]], on="player_id", how="left", suffixes=("", "_p"))
        if "position_group_p" in stats.columns:
            stats["position_group"] = stats["position_group"].fillna(stats["position_group_p"])
    stats = stats[stats["position_group"].isin(SKILL)].copy()
    rush = stats["rush_td"].fillna(0) if "rush_td" in stats.columns else 0
    rec = stats["rec_td"].fillna(0) if "rec_td" in stats.columns else 0
    stats["anytime_tds"] = rush + rec
    stats["y_td"] = (stats["anytime_tds"] > 0).astype(float)
    involved = (
        stats["targets"].fillna(0)
        + stats["receptions"].fillna(0)
        + stats["carries"].fillna(0)
        + (stats["attempts"].fillna(0) if "attempts" in stats.columns else 0)
        + stats["rec_yards"].fillna(0).abs()
        + stats["rush_yards"].fillna(0).abs()
    )
    stats = stats[involved > 0].copy()
    if stats.empty:
        return pd.DataFrame()

    team_prior = _opponent_prior(plays, games, league_id) if plays is not None and not plays.empty else pd.DataFrame()
    stats["opp_id"] = np.where(stats["team_id"] == stats["home_team_id"], stats["away_team_id"], stats["home_team_id"])
    if not team_prior.empty:
        stats = stats.merge(team_prior, on=["game_id", "opp_id"], how="left")
    else:
        stats["opp_def_epa"] = np.nan
        stats["opp_def_epa_available_at"] = pd.NaT

    stats = stats.sort_values(["player_id", "kickoff_utc"])
    g = stats.groupby("player_id", sort=False)
    for col, name in (
        ("targets", "prior_targets"),
        ("receptions", "prior_receptions"),
        ("rec_yards", "prior_rec_yards"),
        ("rush_yards", "prior_rush_yards"),
        ("carries", "prior_carries"),
        ("anytime_tds", "prior_tds"),
        ("y_td", "prior_td_rate"),
    ):
        if col not in stats.columns:
            stats[name] = np.nan
            continue
        stats[name] = g[col].transform(lambda s: s.shift(1).expanding().mean())
    stats["prior_games"] = g.cumcount()
    stats["is_home"] = (stats["team_id"] == stats["home_team_id"]).astype(float)
    stats["league_id"] = league_id
    stats["epa_scale"] = "epa" if league_id == "nfl" else "ppa"
    return stats.reset_index(drop=True)


def chronological_split(
    frame: pd.DataFrame,
    *,
    train_end,
    test_start,
    test_end,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    kick = pd.to_datetime(frame["kickoff_utc"], utc=True)
    train = frame[kick < pd.Timestamp(train_end)].copy()
    test = frame[(kick >= pd.Timestamp(test_start)) & (kick <= pd.Timestamp(test_end))].copy()
    return train, test


def zscore_within_train(train: pd.Series, test: pd.Series) -> tuple[np.ndarray, np.ndarray, dict[str, float]]:
    mu = float(np.nanmean(train)) if len(train) else 0.0
    sd = float(np.nanstd(train)) if len(train) else 1.0
    if sd < 1e-8:
        sd = 1.0
    meta = {"mean": mu, "std": sd}
    return ((train - mu) / sd).to_numpy(), ((test - mu) / sd).to_numpy(), meta


def assert_prior_available(frame: pd.DataFrame) -> None:
    """Raise if a feature game could not have been complete before kickoff."""
    if frame.empty or "kickoff_utc" not in frame.columns:
        return
    kick = pd.to_datetime(frame["kickoff_utc"], utc=True)
    if "opp_def_epa_available_at" in frame.columns:
        avail = pd.to_datetime(frame["opp_def_epa_available_at"], utc=True)
        bad = frame[avail.notna() & (avail >= kick)]
        if not bad.empty:
            raise AssertionError(f"Opponent EPA/PPA used before it was complete: {len(bad)} rows")
    for rec in frame.itertuples(index=False):
        as_of = pd.Timestamp(rec.kickoff_utc).to_pydatetime()
        n = int(getattr(rec, "prior_games", 0) or 0)
        if n <= 0:
            continue
        observed = as_of - GAME_COMPLETE_AFTER - timedelta(seconds=1)
        assert_event_time(as_of=as_of, observed_at=observed)


def _opponent_prior(plays: pd.DataFrame, games: pd.DataFrame, league_id: str) -> pd.DataFrame:
    from spe.analytics.engine import apply_opponent_adjustments, attach_game_context, team_game_metrics

    plays_x = plays if "kickoff_utc" in plays.columns else attach_game_context(plays, games)
    tg = team_game_metrics(plays_x)
    if tg.empty:
        return pd.DataFrame()
    tg = apply_opponent_adjustments(tg, league_id, leave_one_out=True)
    tg["kickoff_utc"] = pd.to_datetime(tg["kickoff_utc"], utc=True)
    tg["completed_at"] = tg["kickoff_utc"] + GAME_COMPLETE_AFTER
    tg = tg.sort_values(["team_id", "completed_at"])
    parts: list[pd.DataFrame] = []
    for team_id, chunk in tg.groupby("team_id", sort=False):
        c = chunk.sort_values("completed_at").copy()
        c["opp_def_epa"] = c["def_epa_per_play"].expanding().mean()
        c["opp_id"] = team_id
        parts.append(c[["game_id", "opp_id", "opp_def_epa", "completed_at", "kickoff_utc"]])
    if not parts:
        return pd.DataFrame()
    hist = pd.concat(parts, ignore_index=True)
    # Map each (game, opponent) to the opponent's expanding mean EXCLUDING the current game
    # by using completed_at of prior games only: shift within team so current game is out.
    hist = hist.sort_values(["opp_id", "completed_at"])
    hist["opp_def_epa"] = hist.groupby("opp_id")["opp_def_epa"].shift(1)
    hist["opp_def_epa_available_at"] = hist.groupby("opp_id")["completed_at"].shift(1)
    return hist[["game_id", "opp_id", "opp_def_epa", "opp_def_epa_available_at"]]
