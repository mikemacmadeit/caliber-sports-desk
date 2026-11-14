"""Chronological partitions that never split a game's players between periods."""
from __future__ import annotations

import pandas as pd


def split_games(frame: pd.DataFrame, fraction: float = 0.8):
    if not 0 < fraction < 1:
        raise ValueError("fraction must be between zero and one")
    if frame.empty:
        return frame.copy(), frame.copy()
    kick = pd.to_datetime(frame["kickoff_utc"], utc=True, errors="raise")
    if kick.isna().any():
        raise ValueError("Every row needs a kickoff")
    # A malformed game with two kickoff values is unsafe to partition.
    if "game_id" in frame and kick.groupby(frame["game_id"]).nunique().max() > 1:
        raise ValueError("A game has inconsistent kickoffs")
    times = sorted(kick.unique())
    if len(times) < 2:
        return frame.copy(), frame.iloc[:0].copy()
    boundary = times[max(1, min(int(len(times) * fraction), len(times) - 1))]
    return frame.loc[kick < boundary].copy(), frame.loc[kick >= boundary].copy()
