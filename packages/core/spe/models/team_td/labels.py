"""Team-game offensive TD and possession labels from play-by-play.

Offensive touchdowns are scrimmage pass/rush TDs. Defensive and special-teams
touchdowns are counted separately and never mixed into offensive totals.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def team_game_labels(plays: pd.DataFrame) -> pd.DataFrame:
    """One row per (game_id, team_id) with offensive TD outcomes and drive counts."""
    if plays.empty:
        return pd.DataFrame()
    df = plays.copy()
    for col in ("is_scrimmage", "is_td", "is_pass", "is_rush", "is_red_zone", "is_turnover"):
        if col not in df.columns:
            df[col] = False
        df[col] = df[col].fillna(False).astype(bool)

    scrim = df.loc[df["is_scrimmage"]].copy()
    if scrim.empty:
        return pd.DataFrame()

    off_td = scrim.loc[scrim["is_td"]]
    pass_td = off_td.loc[off_td["is_pass"]].groupby(["game_id", "offense_id"]).size()
    rush_td = off_td.loc[off_td["is_rush"]].groupby(["game_id", "offense_id"]).size()
    total_td = off_td.groupby(["game_id", "offense_id"]).size()

    plays_n = scrim.groupby(["game_id", "offense_id"]).size()
    rz_plays = scrim.loc[scrim["is_red_zone"]].groupby(["game_id", "offense_id"]).size()
    rz_td = scrim.loc[scrim["is_red_zone"] & scrim["is_td"]].groupby(["game_id", "offense_id"]).size()

    possessions = _possession_counts(scrim)
    scoring_drives = _scoring_drive_counts(scrim)

    # Defensive / ST TDs credited to the defense (non-scrimmage TDs).
    non_scrim = df.loc[~df["is_scrimmage"] & df["is_td"]]
    def_st_td = (
        non_scrim.groupby(["game_id", "defense_id"]).size()
        if not non_scrim.empty and "defense_id" in non_scrim.columns
        else pd.Series(dtype=float)
    )

    idx = plays_n.index
    out = pd.DataFrame(index=idx).reset_index()
    out = out.rename(columns={"offense_id": "team_id"})
    out["off_plays"] = plays_n.reindex(idx).fillna(0).astype(int).to_numpy()
    out["off_td"] = total_td.reindex(idx).fillna(0).astype(int).to_numpy()
    out["pass_td"] = pass_td.reindex(idx).fillna(0).astype(int).to_numpy()
    out["rush_td"] = rush_td.reindex(idx).fillna(0).astype(int).to_numpy()
    # Guard: pass+rush should equal offensive TDs when both flags are set; keep observed total.
    out["pass_td"] = np.minimum(out["pass_td"], out["off_td"])
    out["rush_td"] = np.minimum(out["rush_td"], out["off_td"] - out["pass_td"])
    out["rz_plays"] = rz_plays.reindex(idx).fillna(0).astype(int).to_numpy()
    out["rz_td"] = rz_td.reindex(idx).fillna(0).astype(int).to_numpy()
    out["possessions"] = possessions.reindex(idx).fillna(0).astype(int).to_numpy()
    out["scoring_drives"] = scoring_drives.reindex(idx).fillna(0).astype(int).to_numpy()
    out["def_st_td_scored"] = def_st_td.reindex(idx).fillna(0).astype(int).to_numpy() if len(def_st_td) else 0
    out["off_td_bucket"] = np.where(out["off_td"] >= 5, 5, out["off_td"]).astype(int)
    return out


def _possession_counts(scrim: pd.DataFrame) -> pd.Series:
    if "drive_id" in scrim.columns and not scrim["drive_id"].fillna("").eq("").all():
        drives = scrim.loc[scrim["drive_id"].fillna("").ne("")]
        return drives.groupby(["game_id", "offense_id"])["drive_id"].nunique()
    # Fallback: estimate possessions from scrimmage plays (~6–7 plays per drive).
    plays = scrim.groupby(["game_id", "offense_id"]).size()
    return np.maximum(np.round(plays / 6.5), 1).astype(int)


def _scoring_drive_counts(scrim: pd.DataFrame) -> pd.Series:
    if "drive_id" not in scrim.columns or scrim["drive_id"].fillna("").eq("").all():
        return scrim.loc[scrim["is_td"]].groupby(["game_id", "offense_id"]).size()
    drives = (
        scrim.groupby(["game_id", "offense_id", "drive_id"], dropna=False)
        .agg(scored=("is_td", "max"), reached_rz=("is_red_zone", "max"))
        .reset_index()
    )
    scored = drives.loc[drives["scored"].fillna(False)]
    if scored.empty:
        return pd.Series(dtype=float)
    return scored.groupby(["game_id", "offense_id"]).size()
