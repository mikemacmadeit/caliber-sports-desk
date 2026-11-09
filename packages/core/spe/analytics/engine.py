"""League-agnostic football analytics. NFL uses EPA; FBS uses PPA mapped into the same epa column.

Descriptive game/season rows include the current game. Pregame snapshots must call
`prior_only()` first so kickoff-time features never see the game being evaluated.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import numpy as np
import pandas as pd

from spe.analytics.definitions import (
    EXPLOSIVE_PASS_YARDS,
    EXPLOSIVE_RUSH_YARDS,
    SHRINKAGE_K,
    UNAVAILABLE,
)

SKILL = {"WR", "TE", "RB", "QB"}


def shrink(value: float, prior: float, n: float, k: float) -> float:
    denom = n + k
    if denom <= 0:
        return prior
    return (n * value + k * prior) / denom


GAME_COMPLETE_AFTER = timedelta(hours=4, minutes=30)


def prior_only(
    games: pd.DataFrame,
    as_of: pd.Timestamp,
    *,
    complete_after: timedelta | None = GAME_COMPLETE_AFTER,
) -> pd.DataFrame:
    """Games whose results were available before as_of.

    Kickoff-before-as_of is not enough: a 1pm box score is not known at a 4pm
    kickoff. Require kickoff + complete_after < as_of so same-day earlier games
    only enter after they would have ended.
    """
    kick = pd.to_datetime(games["kickoff_utc"], utc=True)
    as_of_ts = pd.Timestamp(as_of)
    if as_of_ts.tzinfo is None:
        as_of_ts = as_of_ts.tz_localize("UTC")
    elif kick.dt.tz is not None:
        as_of_ts = as_of_ts.tz_convert("UTC")
    if complete_after is None:
        return games.loc[kick < as_of_ts].copy()
    return games.loc[(kick + complete_after) < as_of_ts].copy()


def attach_game_context(plays: pd.DataFrame, games: pd.DataFrame, *, id_map: dict[str, str] | None = None) -> pd.DataFrame:
    """Join canonical plays to games. id_map maps offense_abbr -> team_id when abbrs are not already IDs."""
    g = games.copy()
    if "game_id" not in plays.columns:
        plays = plays.copy()
        if id_map:
            plays["game_id"] = plays["provider_game_id"].map(lambda x: id_map.get(str(x), str(x)))
        else:
            plays["game_id"] = plays["provider_game_id"]
    cols = ["game_id", "home_team_id", "away_team_id", "kickoff_utc", "week", "season_year", "league_id", "neutral_site"]
    have = [c for c in cols if c in g.columns]
    merged = plays.merge(g[have], on="game_id", how="inner", suffixes=("", "_g"))
    if "offense_id" not in merged.columns:
        if id_map:
            merged["offense_id"] = merged["offense_abbr"].map(lambda a: id_map.get(str(a), str(a)))
            merged["defense_id"] = merged["defense_abbr"].map(lambda a: id_map.get(str(a), str(a)))
        else:
            merged["offense_id"] = merged["offense_abbr"]
            merged["defense_id"] = merged["defense_abbr"]
    merged["is_home_off"] = merged["offense_id"] == merged["home_team_id"]
    return merged


def team_game_metrics(plays: pd.DataFrame) -> pd.DataFrame:
    """One row per team per game with offense, defense, and split flags."""
    scrim = plays.loc[plays["is_scrimmage"].fillna(False)].copy()
    if scrim.empty:
        return pd.DataFrame()
    scrim["epa_num"] = pd.to_numeric(scrim["epa"], errors="coerce")
    scrim["explosive"] = (
        (scrim["is_pass"] & (scrim["yards"] >= EXPLOSIVE_PASS_YARDS))
        | (scrim["is_rush"] & (scrim["yards"] >= EXPLOSIVE_RUSH_YARDS))
    )
    scrim["success_num"] = scrim["success"].map(lambda x: np.nan if x is None or (isinstance(x, float) and np.isnan(x)) else float(bool(x)))
    scrim["third"] = pd.to_numeric(scrim["down"], errors="coerce").eq(3)
    scrim["third_ok"] = scrim["third"] & scrim["first_down"].fillna(False)
    scrim["neutral"] = _neutral_mask(scrim)

    off = _agg_offense(scrim)
    deff = _agg_defense(scrim)
    out = off.merge(deff, on=["game_id", "team_id"], how="outer")
    meta = (
        scrim.groupby("game_id", dropna=False)
        .agg(
            league_id=("league_id", "first"),
            season_year=("season_year", "first"),
            week=("week", "first"),
            kickoff_utc=("kickoff_utc", "first"),
            home_team_id=("home_team_id", "first"),
            away_team_id=("away_team_id", "first"),
        )
        .reset_index()
    )
    out = out.merge(meta, on="game_id", how="left")
    out["is_home"] = out["team_id"] == out["home_team_id"]
    out["split"] = np.where(out["is_home"].fillna(False), "home", "away")
    scoring = _points_per_scoring_opp(scrim)
    if not scoring.empty:
        out = out.merge(scoring, on=["game_id", "team_id"], how="left")
    else:
        out["points_per_scoring_opp"] = np.nan
        out["scoring_opps"] = 0
    if "pace_sec_per_play" in out.columns:
        out = out.drop(columns=["pace_sec_per_play"])
    pace = _pace_by_team_game(scrim)
    if not pace.empty:
        out = out.merge(pace, on=["game_id", "team_id"], how="left")
    else:
        out["pace_sec_per_play"] = np.nan
    return out


def apply_opponent_adjustments(team_games: pd.DataFrame, league_id: str, *, leave_one_out: bool = True) -> pd.DataFrame:
    """Shrink opponent-adjusted EPA. Leave-one-out for descriptive ratings; disable for pregame (already prior-only)."""
    if team_games.empty:
        return team_games
    k = SHRINKAGE_K.get(league_id, 10.0)
    df = team_games.copy()
    league_off = float(np.nanmean(df["off_epa_per_play"]))
    league_def = float(np.nanmean(df["def_epa_per_play"]))
    opp_map = _opponent_map(df)
    adj_off = []
    adj_def = []
    for row in df.itertuples(index=False):
        opp = opp_map.get((row.game_id, row.team_id))
        opp_rows = df[df["team_id"] == opp] if opp else df.iloc[0:0]
        if leave_one_out and opp and not opp_rows.empty:
            loo = opp_rows[opp_rows["game_id"] != row.game_id]
            # Empty LOO (opponent has only this game): keep n=1 and shrink hard instead of ignoring the opponent.
            opp_rows = loo if not loo.empty else opp_rows
        n_games = float(len(opp_rows)) if not opp_rows.empty else 0.0
        raw_def = float(np.nanmean(pd.to_numeric(opp_rows["def_epa_per_play"], errors="coerce"))) if not opp_rows.empty else league_def
        raw_off = float(np.nanmean(pd.to_numeric(opp_rows["off_epa_per_play"], errors="coerce"))) if not opp_rows.empty else league_off
        if np.isnan(raw_def):
            raw_def = league_def
        if np.isnan(raw_off):
            raw_off = league_off
        opp_def = shrink(raw_def, league_def, n_games, k)
        opp_off = shrink(raw_off, league_off, n_games, k)
        off = row.off_epa_per_play if pd.notna(row.off_epa_per_play) else league_off
        de = row.def_epa_per_play if pd.notna(row.def_epa_per_play) else league_def
        adj_off.append(off - (opp_def - league_def))
        adj_def.append(de - (opp_off - league_off))
    df["adj_off_epa"] = adj_off
    df["adj_def_epa"] = adj_def
    df["league_off_epa"] = league_off
    df["league_def_epa"] = league_def
    return df


def apply_player_opponent_adjustments(player_games: pd.DataFrame, team_games: pd.DataFrame, league_id: str) -> pd.DataFrame:
    """Shrink player yardage vs opponent defensive EPA. Small samples regress hard to the observed line."""
    if player_games.empty or team_games.empty:
        df = player_games.copy()
        df["adj_rec_yards"] = df["rec_yards"] if "rec_yards" in df.columns else np.nan
        df["adj_rush_yards"] = df["rush_yards"] if "rush_yards" in df.columns else np.nan
        return df
    k = SHRINKAGE_K.get(league_id, 10.0)
    df = player_games.copy()
    tg = team_games.copy()
    league_def = float(np.nanmean(tg["def_epa_per_play"]))
    opp_map = _opponent_map(tg)
    def_by_team = tg.groupby("team_id")["def_epa_per_play"].mean()
    n_by_team = tg.groupby("team_id").size()
    adj_rec = []
    adj_rush = []
    for row in df.itertuples(index=False):
        team_id = getattr(row, "team_id", None)
        opp = opp_map.get((row.game_id, team_id))
        n_opp = float(n_by_team.get(opp, 0)) if opp else 0.0
        raw_def = float(def_by_team.get(opp, league_def)) if opp else league_def
        # Leave this game out of the opponent's mean when they have other games.
        if opp and n_opp > 1:
            other = tg[(tg["team_id"] == opp) & (tg["game_id"] != row.game_id)]["def_epa_per_play"]
            if not other.empty:
                raw_def = float(np.nanmean(other))
                n_opp = float(len(other))
        opp_def = shrink(raw_def, league_def, n_opp, k)
        rec = float(row.rec_yards) if pd.notna(getattr(row, "rec_yards", np.nan)) else 0.0
        rush = float(row.rush_yards) if pd.notna(getattr(row, "rush_yards", np.nan)) else 0.0
        rec_adj = rec - 40.0 * (opp_def - league_def)
        rush_adj = rush - 25.0 * (opp_def - league_def)
        adj_rec.append(shrink(rec_adj, rec, n_opp, k))
        adj_rush.append(shrink(rush_adj, rush, n_opp, k))
    df["adj_rec_yards"] = adj_rec
    df["adj_rush_yards"] = adj_rush
    return df


def season_from_games(team_games: pd.DataFrame, split: str = "all", grain: str = "season") -> pd.DataFrame:
    src = team_games if split == "all" else team_games[team_games["split"] == split]
    if src.empty:
        return pd.DataFrame()
    keys = ["league_id", "season_year", "team_id"] if grain == "season" else ["league_id", "season_year", "week", "team_id"]
    grouped = src.groupby(keys, dropna=False)
    rows = []
    for key, part in grouped:
        if grain == "season":
            league, year, team = key
            week = 0
        else:
            league, year, week, team = key
            week = int(week) if pd.notna(week) else 0
        n = len(part)
        rows.append(
            {
                "league_id": league,
                "season_year": int(year) if pd.notna(year) else 0,
                "team_id": team,
                "week": week,
                "game_id": "",
                "grain": grain,
                "split": split,
                "sample_size": n,
                "off_epa_per_play": _wmean(part, "off_epa_per_play", "off_plays"),
                "def_epa_per_play": _wmean(part, "def_epa_per_play", "def_plays"),
                "pass_epa_per_play": _wmean(part, "pass_epa_per_play", "pass_plays"),
                "rush_epa_per_play": _wmean(part, "rush_epa_per_play", "rush_plays"),
                "off_success_rate": _wmean(part, "off_success_rate", "off_plays"),
                "def_success_rate": _wmean(part, "def_success_rate", "def_plays"),
                "explosive_play_rate": _wmean(part, "explosive_play_rate", "off_plays"),
                "explosive_play_rate_allowed": _wmean(part, "explosive_play_rate_allowed", "def_plays"),
                "third_down_rate": _wmean(part, "third_down_rate", "third_down_att"),
                "red_zone_td_rate": _wmean(part, "red_zone_td_rate", "rz_trips"),
                "plays_per_game": float(np.nanmean(part["plays_per_game"])) if "plays_per_game" in part else n,
                "pace_sec_per_play": float(np.nanmean(part["pace_sec_per_play"])) if "pace_sec_per_play" in part else np.nan,
                "neutral_pass_rate": _wmean(part, "neutral_pass_rate", "neutral_plays"),
                "points_per_scoring_opp": _wmean(part, "points_per_scoring_opp", "scoring_opps"),
                "adj_off_epa": float(np.nanmean(part["adj_off_epa"])) if "adj_off_epa" in part else np.nan,
                "adj_def_epa": float(np.nanmean(part["adj_def_epa"])) if "adj_def_epa" in part else np.nan,
                "pass_yards": float(part["pass_yards"].sum()) if "pass_yards" in part else np.nan,
                "rush_yards": float(part["rush_yards"].sum()) if "rush_yards" in part else np.nan,
                "pass_yards_allowed": float(part["pass_yards_allowed"].sum()) if "pass_yards_allowed" in part else np.nan,
                "rush_yards_allowed": float(part["rush_yards_allowed"].sum()) if "rush_yards_allowed" in part else np.nan,
                "rec_td_allowed": float(part["rec_td_allowed"].sum()) if "rec_td_allowed" in part else np.nan,
                "rush_td_allowed": float(part["rush_td_allowed"].sum()) if "rush_td_allowed" in part else np.nan,
                "rz_td_allowed": float(part["rz_td_allowed"].sum()) if "rz_td_allowed" in part else np.nan,
                "pass_epa_allowed": _wmean(part, "pass_epa_allowed", "pass_plays_allowed")
                if "pass_epa_allowed" in part
                else np.nan,
                "rush_epa_allowed": _wmean(part, "rush_epa_allowed", "rush_plays_allowed")
                if "rush_epa_allowed" in part
                else np.nan,
            }
        )
    return pd.DataFrame(rows)


def player_game_metrics(
    stats: pd.DataFrame,
    plays: pd.DataFrame | None = None,
    players: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Box-score metrics plus PBP-derived RZ/GL when plays identify ball-handlers."""
    if stats.empty:
        return pd.DataFrame()
    df = stats.copy()
    if "anytime_tds" not in df.columns:
        rush_td = df["rush_td"] if "rush_td" in df.columns else 0
        rec_td = df["rec_td"] if "rec_td" in df.columns else 0
        df["anytime_tds"] = rush_td.fillna(0) + rec_td.fillna(0)
    if "tds" not in df.columns:
        # Anytime TD grading is rush+rec. Passing TDs are a different market.
        df["tds"] = df["anytime_tds"]
    team_tot = df.groupby("game_id").agg(team_targets=("targets", "sum"), team_tds=("tds", "sum")).reset_index()
    df = df.merge(team_tot, on="game_id", how="left")
    df["target_share"] = np.where(df["team_targets"] > 0, df["targets"] / df["team_targets"], np.nan)
    df["team_td_share"] = np.where(df["team_tds"] > 0, df["tds"] / df["team_tds"], np.nan)
    df["rec_yards_per_target"] = np.where(df["targets"] > 0, df["rec_yards"] / df["targets"], np.nan)
    carries = df["carries"] if "carries" in df.columns else 0
    df["rush_yards_per_carry"] = np.where(carries > 0, df["rush_yards"] / carries, np.nan)
    if plays is not None and not plays.empty:
        derived = derive_opportunity(plays)
        df = df.merge(derived, on=["game_id", "player_id"], how="left", suffixes=("", "_pbp"))
        for col in ("rz_targets", "rz_carries", "gl_carries"):
            pbp_col = f"{col}_pbp"
            if pbp_col not in df.columns:
                continue
            if col not in df.columns:
                df[col] = df[pbp_col]
            else:
                box = pd.to_numeric(df[col], errors="coerce")
                pbp = pd.to_numeric(df[pbp_col], errors="coerce")
                # Keep observed box values; fill gaps from identified PBP ball-handlers.
                df[col] = np.where(box.isna() | ((box == 0) & pbp.fillna(0).gt(0)), pbp, box)
    if "rz_targets" not in df.columns:
        df["rz_targets"] = np.nan
        df["rz_carries"] = np.nan
        df["gl_carries"] = np.nan
        df["rz_from_pbp"] = False
    else:
        df["rz_from_pbp"] = df["rz_targets"].notna() | df["rz_carries"].notna()
    if players is not None and "position_group" in players.columns:
        df = df.merge(players[["player_id", "position_group"]], on="player_id", how="left")
    return df


def add_usage_trends(player_games: pd.DataFrame) -> pd.DataFrame:
    """Rolling 3/5/10 prior games. Requires kickoff_utc and is sorted per player."""
    if player_games.empty:
        return player_games
    df = player_games.copy()
    df["kickoff_utc"] = pd.to_datetime(df["kickoff_utc"], utc=True)
    df = df.sort_values(["player_id", "kickoff_utc"])
    for window, prefix in ((3, "roll3"), (5, "roll5"), (10, "roll10")):
        grp = df.groupby("player_id", sort=False)
        df[f"{prefix}_targets"] = grp["targets"].transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
        if "carries" in df.columns:
            df[f"{prefix}_carries"] = grp["carries"].transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
        if "rec_yards" in df.columns:
            df[f"{prefix}_rec_yards"] = grp["rec_yards"].transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
        if "rush_yards" in df.columns:
            df[f"{prefix}_rush_yards"] = grp["rush_yards"].transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
    df["workload_delta_3v10"] = df.get("roll3_targets") - df.get("roll10_targets")
    return df


def derive_opportunity(plays: pd.DataFrame) -> pd.DataFrame:
    """RZ targets / RZ carries / GL carries from identified ball-handlers. Missing IDs stay missing."""
    rows = []
    rz_pass = plays.loc[plays["is_red_zone"].fillna(False) & plays["pass_attempt"].fillna(False) & plays["receiver_player_id"].notna()]
    rz_rush = plays.loc[plays["is_red_zone"].fillna(False) & plays["is_rush"].fillna(False) & plays["rusher_player_id"].notna()]
    gl_rush = plays.loc[plays["is_goal_line"].fillna(False) & plays["is_rush"].fillna(False) & plays["rusher_player_id"].notna()]
    if not rz_pass.empty:
        t = rz_pass.groupby(["game_id", "receiver_player_id"]).size().reset_index(name="rz_targets")
        t = t.rename(columns={"receiver_player_id": "player_id"})
        rows.append(t)
    if not rz_rush.empty:
        c = rz_rush.groupby(["game_id", "rusher_player_id"]).size().reset_index(name="rz_carries")
        c = c.rename(columns={"rusher_player_id": "player_id"})
        rows.append(c)
    if not gl_rush.empty:
        g = gl_rush.groupby(["game_id", "rusher_player_id"]).size().reset_index(name="gl_carries")
        g = g.rename(columns={"rusher_player_id": "player_id"})
        rows.append(g)
    if not rows:
        return pd.DataFrame(columns=["game_id", "player_id", "rz_targets", "rz_carries", "gl_carries"])
    out = rows[0]
    for extra in rows[1:]:
        out = out.merge(extra, on=["game_id", "player_id"], how="outer")
    for col in ("rz_targets", "rz_carries", "gl_carries"):
        if col not in out.columns:
            out[col] = 0
        out[col] = out[col].fillna(0).astype(int)
    return out


def expected_td_opps(row: pd.Series, league_rz_td_rate: float, league_gl_td_rate: float) -> float | None:
    """Opportunity-based TD expectation from RZ/GL volume. None if those volumes were not observed."""
    rz_t = row.get("rz_targets")
    rz_c = row.get("rz_carries")
    gl = row.get("gl_carries")
    if pd.isna(rz_t) and pd.isna(rz_c) and pd.isna(gl):
        return None
    return float(
        (0.0 if pd.isna(rz_t) else rz_t) * league_rz_td_rate
        + (0.0 if pd.isna(rz_c) else rz_c) * 0.5 * league_gl_td_rate
        + (0.0 if pd.isna(gl) else gl) * league_gl_td_rate
    )


def yards_allowed_by_position(plays: pd.DataFrame, players: pd.DataFrame) -> pd.DataFrame:
    """Receiving yards allowed by defender team and receiver position_group when the catcher is identified."""
    rec = plays.loc[plays["complete_pass"].fillna(False) & plays["receiver_player_id"].notna()].copy()
    if rec.empty or players.empty:
        return pd.DataFrame()
    rec = rec.merge(players[["player_id", "position_group"]], left_on="receiver_player_id", right_on="player_id", how="left")
    rec["position_group"] = rec["position_group"].fillna("UNK")
    rec["identified"] = rec["position_group"].ne("UNK")
    grouped = (
        rec.groupby(["game_id", "defense_id", "position_group"], dropna=False)
        .agg(rec_yards_allowed=("yards", "sum"), targets_identified=("play_id", "count"))
        .reset_index()
        .rename(columns={"defense_id": "team_id"})
    )
    return grouped


def coverage_for_team_row(row: dict[str, Any]) -> dict[str, Any]:
    missing = [m for m in ("off_epa_per_play", "third_down_rate", "red_zone_td_rate", "neutral_pass_rate", "points_per_scoring_opp") if row.get(m) is None or (isinstance(row.get(m), float) and np.isnan(row.get(m)))]
    return {
        "missing_metrics": missing,
        "unavailable_premium": list(UNAVAILABLE),
        "complete": not missing,
    }


def _agg_offense(scrim: pd.DataFrame) -> pd.DataFrame:
    g = scrim.groupby(["game_id", "offense_id"], dropna=False)
    rz_td = scrim.loc[scrim["is_red_zone"].fillna(False) & scrim["is_td"].fillna(False)].groupby(["game_id", "offense_id"]).size()
    out = g.agg(
        off_plays=("play_id", "count"),
        off_epa_per_play=("epa_num", "mean"),
        off_success_rate=("success_num", "mean"),
        explosive_play_rate=("explosive", "mean"),
        pass_plays=("is_pass", "sum"),
        rush_plays=("is_rush", "sum"),
        third_down_att=("third", "sum"),
        third_down_ok=("third_ok", "sum"),
        rz_plays=("is_red_zone", "sum"),
        neutral_plays=("neutral", "sum"),
    ).reset_index()
    # redo pass/rush epa without the fragile lambda on yards
    pass_epa = scrim.loc[scrim["is_pass"]].groupby(["game_id", "offense_id"])["epa_num"].mean()
    rush_epa = scrim.loc[scrim["is_rush"]].groupby(["game_id", "offense_id"])["epa_num"].mean()
    pass_sr = scrim.loc[scrim["is_pass"]].groupby(["game_id", "offense_id"])["success_num"].mean()
    rush_sr = scrim.loc[scrim["is_rush"]].groupby(["game_id", "offense_id"])["success_num"].mean()
    pass_yd = scrim.loc[scrim["is_pass"]].groupby(["game_id", "offense_id"])["yards"].sum()
    rush_yd = scrim.loc[scrim["is_rush"]].groupby(["game_id", "offense_id"])["yards"].sum()
    npass = scrim.loc[scrim["neutral"]].groupby(["game_id", "offense_id"])["is_pass"].mean()
    out = out.rename(columns={"offense_id": "team_id"})
    idx = ["game_id", "team_id"]
    out = out.set_index(idx)
    out["pass_epa_per_play"] = pass_epa
    out["rush_epa_per_play"] = rush_epa
    out["pass_success_rate"] = pass_sr
    out["rush_success_rate"] = rush_sr
    out["pass_yards"] = pass_yd
    out["rush_yards"] = rush_yd
    out["neutral_pass_rate"] = npass
    out["third_down_rate"] = np.where(out["third_down_att"] > 0, out["third_down_ok"] / out["third_down_att"], np.nan)
    out["rz_td"] = rz_td
    out["red_zone_td_rate"] = np.where(out["rz_plays"] > 0, out["rz_td"].fillna(0) / out["rz_plays"], np.nan)
    out["plays_per_game"] = out["off_plays"]
    out["rz_trips"] = out["rz_plays"]
    return out.reset_index()


def _agg_defense(scrim: pd.DataFrame) -> pd.DataFrame:
    pass_epa = scrim.loc[scrim["is_pass"]].groupby(["game_id", "defense_id"])["epa_num"].mean()
    rush_epa = scrim.loc[scrim["is_rush"]].groupby(["game_id", "defense_id"])["epa_num"].mean()
    pass_yd = scrim.loc[scrim["is_pass"]].groupby(["game_id", "defense_id"])["yards"].sum()
    rush_yd = scrim.loc[scrim["is_rush"]].groupby(["game_id", "defense_id"])["yards"].sum()
    rec_td = scrim.loc[scrim["is_pass"] & scrim["is_td"]].groupby(["game_id", "defense_id"]).size()
    rush_td = scrim.loc[scrim["is_rush"] & scrim["is_td"]].groupby(["game_id", "defense_id"]).size()
    rz_td = scrim.loc[scrim["is_red_zone"] & scrim["is_td"]].groupby(["game_id", "defense_id"]).size()
    g = scrim.groupby(["game_id", "defense_id"], dropna=False)
    out = g.agg(
        def_plays=("play_id", "count"),
        def_epa_per_play=("epa_num", "mean"),
        def_success_rate=("success_num", "mean"),
        explosive_play_rate_allowed=("explosive", "mean"),
        pass_plays_allowed=("is_pass", "sum"),
        rush_plays_allowed=("is_rush", "sum"),
    ).reset_index().rename(columns={"defense_id": "team_id"}).set_index(["game_id", "team_id"])
    out["pass_epa_allowed"] = pass_epa
    out["rush_epa_allowed"] = rush_epa
    out["pass_yards_allowed"] = pass_yd
    out["rush_yards_allowed"] = rush_yd
    out["rec_td_allowed"] = rec_td
    out["rush_td_allowed"] = rush_td
    out["rz_td_allowed"] = rz_td
    return out.reset_index()


def _points_per_scoring_opp(scrim: pd.DataFrame) -> pd.DataFrame:
    if "drive_id" not in scrim.columns or scrim["drive_id"].fillna("").eq("").all():
        return pd.DataFrame()
    drives = (
        scrim.groupby(["game_id", "offense_id", "drive_id"], dropna=False)
        .agg(
            reached_rz=("is_red_zone", "max"),
            scored_td=("is_td", "max"),
            drive_result=("drive_result", "first"),
        )
        .reset_index()
    )
    result = drives["drive_result"].fillna("").str.lower()
    drives["is_opp"] = drives["reached_rz"].fillna(False) | result.str.contains("touchdown|field goal")
    drives["points"] = np.where(drives["scored_td"].fillna(False) | result.str.contains("touchdown"), 7, 0)
    drives["points"] = np.where(result.str.contains("field goal") & ~drives["scored_td"].fillna(False), 3, drives["points"])
    opps = drives.loc[drives["is_opp"]]
    if opps.empty:
        return pd.DataFrame()
    out = opps.groupby(["game_id", "offense_id"]).agg(scoring_opps=("drive_id", "count"), points=("points", "sum")).reset_index()
    out["points_per_scoring_opp"] = np.where(out["scoring_opps"] > 0, out["points"] / out["scoring_opps"], np.nan)
    return out.rename(columns={"offense_id": "team_id"})[["game_id", "team_id", "points_per_scoring_opp", "scoring_opps"]]


def _neutral_mask(scrim: pd.DataFrame) -> pd.Series:
    down = pd.to_numeric(scrim["down"], errors="coerce")
    period = pd.to_numeric(scrim["period"], errors="coerce")
    margin = pd.to_numeric(scrim["score_off"], errors="coerce").fillna(0) - pd.to_numeric(scrim["score_def"], errors="coerce").fillna(0)
    wp = pd.to_numeric(scrim["wp"], errors="coerce") if "wp" in scrim.columns else pd.Series(np.nan, index=scrim.index)
    wp_ok = wp.between(0.2, 0.8)
    fallback = down.le(2) & period.le(3) & margin.abs().le(8)
    return pd.Series(np.where(wp.notna(), wp_ok & down.le(2) & margin.abs().le(8), fallback), index=scrim.index)


def _pace_by_team_game(scrim: pd.DataFrame) -> pd.DataFrame:
    if "game_seconds_remaining" not in scrim.columns:
        return pd.DataFrame()
    rows = []
    for (gid, team), part in scrim.groupby(["game_id", "offense_id"], dropna=False):
        rows.append({"game_id": gid, "team_id": team, "pace_sec_per_play": _pace(part)})
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _pace(part: pd.DataFrame) -> float:
    secs = pd.to_numeric(part["game_seconds_remaining"], errors="coerce").dropna()
    if len(secs) < 2:
        return float("nan")
    diffs = secs.sort_values(ascending=False).diff().abs().dropna()
    diffs = diffs[(diffs > 0) & (diffs < 60)]
    if diffs.empty:
        return float("nan")
    return float(diffs.median())


def _wmean(part: pd.DataFrame, value: str, weight: str) -> float:
    if value not in part.columns:
        return float("nan")
    v = pd.to_numeric(part[value], errors="coerce")
    if weight not in part.columns:
        return float(np.nanmean(v))
    w = pd.to_numeric(part[weight], errors="coerce").fillna(0)
    mask = v.notna() & (w > 0)
    if not mask.any():
        return float(np.nanmean(v))
    return float(np.average(v[mask], weights=w[mask]))


def _opponent_map(df: pd.DataFrame) -> dict[tuple[Any, Any], Any]:
    """Map (game_id, team_id) -> opponent team_id using the other row in the same game."""
    out: dict[tuple[Any, Any], Any] = {}
    for gid, part in df.groupby("game_id"):
        teams = list(part["team_id"])
        if len(teams) == 2:
            out[(gid, teams[0])] = teams[1]
            out[(gid, teams[1])] = teams[0]
    return out
