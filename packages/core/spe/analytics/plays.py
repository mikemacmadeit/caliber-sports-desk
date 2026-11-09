"""Normalize nflverse and CFBD plays into one analytics frame. Do not use FTN columns."""

from __future__ import annotations

from typing import Any

import pandas as pd

from spe.ingest.values import as_float, as_int, as_str, is_missing

NFL_SCRIMMAGE = {"pass", "run"}
FTN_PREFIX = "ftn_"


def drop_ftn(df: pd.DataFrame) -> pd.DataFrame:
    drop = [c for c in df.columns if str(c).startswith(FTN_PREFIX)]
    return df.drop(columns=drop) if drop else df


def _bool_series(frame: pd.DataFrame, col: str) -> pd.Series:
    if col not in frame.columns:
        return pd.Series(False, index=frame.index)
    s = frame[col]
    if s.dtype == bool:
        return s.fillna(False)
    return pd.to_numeric(s, errors="coerce").fillna(0).astype(float) > 0


def nflverse_to_canonical(df: pd.DataFrame, *, player_prefix: str = "nfl:") -> pd.DataFrame:
    df = drop_ftn(df)
    if df.empty:
        return _empty()
    play_type = df["play_type"].astype(str).str.lower() if "play_type" in df.columns else pd.Series("", index=df.index)
    yardline = pd.to_numeric(df["yardline_100"], errors="coerce") if "yardline_100" in df.columns else pd.Series(pd.NA, index=df.index)
    yards = pd.to_numeric(df["yards_gained"], errors="coerce") if "yards_gained" in df.columns else pd.Series(0.0, index=df.index)
    epa = pd.to_numeric(df["epa"], errors="coerce") if "epa" in df.columns else pd.Series(pd.NA, index=df.index)
    is_pass = play_type.eq("pass") | _bool_series(df, "pass_attempt")
    is_rush = play_type.eq("run") | _bool_series(df, "rush_attempt")
    is_td = _bool_series(df, "touchdown") | _bool_series(df, "pass_touchdown") | _bool_series(df, "rush_touchdown")
    is_to = _bool_series(df, "interception") | _bool_series(df, "fumble_lost")
    success = _bool_series(df, "success") if "success" in df.columns else (epa > 0)
    first_down = _bool_series(df, "first_down")
    out = pd.DataFrame(
        {
            "provider_game_id": df["game_id"].astype(str) if "game_id" in df.columns else "",
            "play_id": df["play_id"].astype(str) if "play_id" in df.columns else "",
            "offense_abbr": df["posteam"].astype(str) if "posteam" in df.columns else "",
            "defense_abbr": df["defteam"].astype(str) if "defteam" in df.columns else "",
            "down": pd.to_numeric(df["down"], errors="coerce") if "down" in df.columns else pd.NA,
            "distance": pd.to_numeric(df["ydstogo"], errors="coerce") if "ydstogo" in df.columns else pd.NA,
            "yardline": yardline,
            "play_type": play_type,
            "yards": yards.fillna(0.0),
            "epa": epa,
            "ppa": pd.NA,
            "wp": pd.to_numeric(df["wp"], errors="coerce") if "wp" in df.columns else pd.NA,
            "success": success,
            "is_pass": is_pass.fillna(False),
            "is_rush": is_rush.fillna(False),
            "is_td": is_td.fillna(False),
            "is_turnover": is_to.fillna(False),
            "is_red_zone": (yardline <= 20) & yardline.notna(),
            "is_goal_line": (yardline <= 5) & yardline.notna(),
            "is_scrimmage": play_type.isin(NFL_SCRIMMAGE),
            "first_down": first_down,
            "game_seconds_remaining": pd.to_numeric(df["game_seconds_remaining"], errors="coerce")
            if "game_seconds_remaining" in df.columns
            else pd.NA,
            "period": pd.to_numeric(df["qtr"], errors="coerce") if "qtr" in df.columns else pd.NA,
            "score_off": pd.to_numeric(df["posteam_score"], errors="coerce") if "posteam_score" in df.columns else 0,
            "score_def": pd.to_numeric(df["defteam_score"], errors="coerce") if "defteam_score" in df.columns else 0,
            "drive_id": df["drive"].astype(str) if "drive" in df.columns else "",
            "passer_raw": df["passer_player_id"] if "passer_player_id" in df.columns else None,
            "rusher_raw": df["rusher_player_id"] if "rusher_player_id" in df.columns else None,
            "receiver_raw": df["receiver_player_id"] if "receiver_player_id" in df.columns else None,
            "complete_pass": _bool_series(df, "complete_pass"),
            "pass_attempt": _bool_series(df, "pass_attempt") | is_pass,
            "drive_result": df["fixed_drive_result"].astype(str) if "fixed_drive_result" in df.columns else "",
        }
    )
    out["passer_player_id"] = out["passer_raw"].map(lambda v: _pid(v, player_prefix))
    out["rusher_player_id"] = out["rusher_raw"].map(lambda v: _pid(v, player_prefix))
    out["receiver_player_id"] = out["receiver_raw"].map(lambda v: _pid(v, player_prefix))
    return out.drop(columns=["passer_raw", "rusher_raw", "receiver_raw"])


def cfbd_to_canonical(records: list[dict[str, Any]], *, player_prefix: str = "fbs:") -> pd.DataFrame:
    if not records:
        return _empty()
    rows = []
    for raw in records:
        ptype = (as_str(raw.get("playType") or raw.get("play_type")) or "unknown").lower()
        yards = as_float(raw.get("yardsGained") or raw.get("yards_gained")) or 0.0
        ppa = as_float(raw.get("ppa"))
        yardline = as_int(raw.get("yardsToGoal") or raw.get("yards_to_goal") or raw.get("yardLine") or raw.get("yard_line"), None)
        is_pass = any(tok in ptype for tok in ("pass", "sack"))
        is_rush = "rush" in ptype or ptype == "run"
        is_td = "touchdown" in ptype or bool(raw.get("scoring")) and "touchdown" in ptype
        if raw.get("scoring") and "touchdown" in str(raw.get("playText") or raw.get("play_text") or "").lower():
            is_td = True
        is_to = any(tok in ptype for tok in ("intercept", "fumble"))
        clock = raw.get("clock")
        clock_sec = 0
        if isinstance(clock, dict):
            clock_sec = int(clock.get("minutes") or 0) * 60 + int(clock.get("seconds") or 0)
        elif clock is not None:
            clock_sec = as_int(clock, 0) or 0
        period = as_int(raw.get("period"), 1) or 1
        rows.append(
            {
                "provider_game_id": as_str(raw.get("gameId") or raw.get("game_id")) or "",
                "play_id": as_str(raw.get("id") or raw.get("playNumber") or raw.get("play_number")) or "",
                "offense_abbr": as_str(raw.get("offenseId") or raw.get("offense_id") or raw.get("offense")) or "",
                "defense_abbr": as_str(raw.get("defenseId") or raw.get("defense_id") or raw.get("defense")) or "",
                "home_name": as_str(raw.get("home")) or "",
                "away_name": as_str(raw.get("away")) or "",
                "down": as_int(raw.get("down"), None),
                "distance": as_float(raw.get("distance")),
                "yardline": yardline,
                "play_type": ptype,
                "yards": yards,
                "epa": ppa,
                "ppa": ppa,
                "wp": None,
                "success": (ppa is not None and ppa > 0),
                "is_pass": is_pass,
                "is_rush": is_rush,
                "is_td": is_td,
                "is_turnover": is_to,
                "is_red_zone": yardline is not None and yardline <= 20,
                "is_goal_line": yardline is not None and yardline <= 5,
                "is_scrimmage": is_pass or is_rush,
                "first_down": (as_float(raw.get("distance")) or 99) <= yards,
                "game_seconds_remaining": max(0, (5 - period) * 15 * 60 + clock_sec) if period <= 4 else clock_sec,
                "period": period,
                "score_off": as_int(raw.get("offenseScore") or raw.get("offense_score"), 0) or 0,
                "score_def": as_int(raw.get("defenseScore") or raw.get("defense_score"), 0) or 0,
                "drive_id": as_str(raw.get("driveNumber") or raw.get("drive_number")) or "",
                "passer_player_id": None,
                "rusher_player_id": None,
                "receiver_player_id": None,
                "complete_pass": "reception" in ptype or "complete" in ptype,
                "pass_attempt": is_pass,
                "drive_result": "",
            }
        )
    return pd.DataFrame(rows)


def _pid(value: Any, prefix: str) -> str | None:
    if is_missing(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("nfl:") or text.startswith("fbs:"):
        return text
    return f"{prefix}{text}"


def _empty() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "provider_game_id",
            "play_id",
            "offense_abbr",
            "defense_abbr",
            "home_name",
            "away_name",
            "down",
            "distance",
            "yardline",
            "play_type",
            "yards",
            "epa",
            "ppa",
            "wp",
            "success",
            "is_pass",
            "is_rush",
            "is_td",
            "is_turnover",
            "is_red_zone",
            "is_goal_line",
            "is_scrimmage",
            "first_down",
            "game_seconds_remaining",
            "period",
            "score_off",
            "score_def",
            "drive_id",
            "passer_player_id",
            "rusher_player_id",
            "receiver_player_id",
            "complete_pass",
            "pass_attempt",
            "drive_result",
        ]
    )
