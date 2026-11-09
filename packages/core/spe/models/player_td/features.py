"""Player opportunity intelligence for Anytime TD.

All player measurements are prior-only (shifted). Missing FBS RZ/GL/target-share
stay missing — never fabricated. Pass TDs are never treated as player Anytime TDs.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.models.advanced import load_model_frame

SKILL = {"QB", "RB", "WR", "TE"}

# Candidate opportunity / matchup features. Coverage decides keep/drop.
OPPORTUNITY_FEATURES = [
    "prior_td_rate",
    "roll3_td_rate",
    "roll5_td_rate",
    "prior_targets",
    "prior_carries",
    "prior_receptions",
    "prior_rec_yards",
    "prior_rush_yards",
    "prior_target_share",
    "prior_carry_share",
    "prior_rz_targets",
    "prior_rz_carries",
    "prior_gl_carries",
    "prior_rz_touches",
    "prior_team_td_share",
    "workload_delta",
    "snap_roll3",
    "snap_delta",
    "depth_rank",
    "starter",
    "roster_active",
    "own_off_epa",
    "pace",
    "team_rz_td",
    "team_points_per_opp",
    "is_home",
]

DEFENSE_FEATURES = [
    "opp_def_z",
    "opp_adj_def",
    "opp_rush_epa_allowed",
    "opp_pass_epa_allowed",
    "opp_rush_td_allowed",
    "opp_rec_td_allowed",
    "opp_rz_td_allowed",
    "opp_explosive_allowed",
    "usage_x_defense",
    "rz_share_x_rz_def",
    "carry_x_rush_def",
    "target_x_pass_def",
]

INJURY_FEATURES = [
    "prior_inactive_rate",
    "injury_designation_out",
    "injury_designation_uncertain",
]


def _shift_expanding(group: pd.core.groupby.generic.SeriesGroupBy, col: str) -> pd.Series:
    return group[col].transform(lambda s: s.shift(1).expanding(min_periods=1).mean())


def _shift_roll(group: pd.core.groupby.generic.SeriesGroupBy, col: str, window: int) -> pd.Series:
    return group[col].transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())


def enrich_opportunity_features(frame: pd.DataFrame, league_id: str) -> pd.DataFrame:
    """Add recency, RZ touch, and team-TD-share priors. FBS RZ columns stay NaN."""
    out = frame.sort_values(["player_id", "kickoff_utc"]).copy()
    # Anytime TD label components — pass_td is intentionally excluded.
    out["rush_td"] = pd.to_numeric(out.get("rush_td"), errors="coerce").fillna(0)
    out["rec_td"] = pd.to_numeric(out.get("rec_td"), errors="coerce").fillna(0)
    out["anytime_tds"] = out["rush_td"] + out["rec_td"]
    out["y_td"] = (out["anytime_tds"] > 0).astype(float)

    if league_id == "fbs":
        for col in ("rz_targets", "rz_carries", "gl_carries"):
            if col in out.columns:
                out[col] = np.nan

    out["rz_touches"] = pd.to_numeric(out.get("rz_targets"), errors="coerce").fillna(0) + pd.to_numeric(
        out.get("rz_carries"), errors="coerce"
    ).fillna(0)
    if league_id == "fbs":
        out["rz_touches"] = np.nan

    # Team scoring opportunity share (prior-only).
    team_tds = out.groupby(["game_id", "team_id"], dropna=False)["anytime_tds"].transform("sum")
    out["team_td_share"] = np.where(team_tds > 0, out["anytime_tds"] / team_tds, np.nan)

    g = out.groupby("player_id", sort=False)
    out["roll3_td_rate"] = _shift_roll(g, "anytime_tds", 3)
    out["roll5_td_rate"] = _shift_roll(g, "anytime_tds", 5)
    if "prior_td_rate" not in out.columns:
        out["prior_td_rate"] = _shift_expanding(g, "anytime_tds")
    out["prior_rz_touches"] = _shift_expanding(g, "rz_touches")
    out["prior_rz_carries"] = _shift_expanding(g, "rz_carries") if "rz_carries" in out.columns else np.nan
    out["prior_team_td_share"] = _shift_expanding(g, "team_td_share")

    # Rushing / receiving TD priors separately (QB rush path; WR/TE receiving path).
    out["prior_rush_td_rate"] = _shift_expanding(g, "rush_td")
    out["prior_rec_td_rate"] = _shift_expanding(g, "rec_td")
    out["roll3_rush_td"] = _shift_roll(g, "rush_td", 3)
    out["roll3_rec_td"] = _shift_roll(g, "rec_td", 3)
    out["roll3_carries"] = _shift_roll(g, "carries", 3) if "carries" in out.columns else np.nan
    out["roll3_targets"] = _shift_roll(g, "targets", 3) if "targets" in out.columns else np.nan

    if league_id == "fbs":
        for col in (
            "prior_rz_targets",
            "prior_rz_carries",
            "prior_gl_carries",
            "prior_rz_touches",
            "prior_target_share",
            "snap_roll3",
            "snap_delta",
            "depth_rank",
            "starter",
            "roster_active",
        ):
            if col in out.columns:
                out[col] = np.nan
    return out


def attach_defense_matchups(session: Session, frame: pd.DataFrame, league_id: str) -> pd.DataFrame:
    """Opponent defensive efficiency from completed team_analytics — no invented CB matchups."""
    if frame.empty:
        return frame
    team = pd.read_sql(
        text(
            """
            SELECT t.team_id, t.game_id, g.kickoff_utc,
                   json_extract(t.metrics, '$.rush_epa_allowed') AS rush_epa_allowed,
                   json_extract(t.metrics, '$.pass_epa_allowed') AS pass_epa_allowed,
                   json_extract(t.metrics, '$.rush_td_allowed') AS rush_td_allowed,
                   json_extract(t.metrics, '$.rec_td_allowed') AS rec_td_allowed,
                   json_extract(t.metrics, '$.rz_td_allowed') AS rz_td_allowed,
                   json_extract(t.metrics, '$.explosive_play_rate_allowed') AS explosive_allowed,
                   json_extract(t.metrics, '$.adj_def_epa') AS adj_def
            FROM team_analytics t
            JOIN games g ON g.game_id = t.game_id
            WHERE t.league_id = :lg AND t.grain = 'game'
              AND g.status = 'final' AND g.data_tier = 'historical'
            """
        ),
        session.connection(),
        params={"lg": league_id},
    )
    out = frame.copy()
    if team.empty:
        for col in DEFENSE_FEATURES:
            if col not in out.columns:
                out[col] = np.nan
        return out

    team["kickoff_utc"] = pd.to_datetime(team["kickoff_utc"], utc=True)
    team = team.sort_values(["team_id", "kickoff_utc"])
    g = team.groupby("team_id", sort=False)
    for src, dest in (
        ("rush_epa_allowed", "prior_rush_epa_allowed"),
        ("pass_epa_allowed", "prior_pass_epa_allowed"),
        ("rush_td_allowed", "prior_rush_td_allowed"),
        ("rec_td_allowed", "prior_rec_td_allowed"),
        ("rz_td_allowed", "prior_rz_td_allowed"),
        ("explosive_allowed", "prior_explosive_allowed"),
        ("adj_def", "prior_adj_def_def"),
    ):
        team[dest] = g[src].transform(lambda s: pd.to_numeric(s, errors="coerce").shift(1).expanding(min_periods=1).mean())

    opp = team[
        [
            "game_id",
            "team_id",
            "prior_rush_epa_allowed",
            "prior_pass_epa_allowed",
            "prior_rush_td_allowed",
            "prior_rec_td_allowed",
            "prior_rz_td_allowed",
            "prior_explosive_allowed",
        ]
    ].rename(columns={"team_id": "opp_id"})
    out["opp_id"] = np.where(out["team_id"] == out["home_team_id"], out["away_team_id"], out["home_team_id"])
    out = out.drop(columns=[c for c in opp.columns if c in out.columns and c not in {"game_id", "opp_id"}], errors="ignore")
    out = out.merge(opp.drop_duplicates(["game_id", "opp_id"]), on=["game_id", "opp_id"], how="left")

    out["opp_rush_epa_allowed"] = pd.to_numeric(out["prior_rush_epa_allowed"], errors="coerce")
    out["opp_pass_epa_allowed"] = pd.to_numeric(out["prior_pass_epa_allowed"], errors="coerce")
    out["opp_rush_td_allowed"] = pd.to_numeric(out["prior_rush_td_allowed"], errors="coerce")
    out["opp_rec_td_allowed"] = pd.to_numeric(out["prior_rec_td_allowed"], errors="coerce")
    out["opp_rz_td_allowed"] = pd.to_numeric(out["prior_rz_td_allowed"], errors="coerce")
    out["opp_explosive_allowed"] = pd.to_numeric(out["prior_explosive_allowed"], errors="coerce")

    # Interactions — missing stays missing.
    out["rz_share_x_rz_def"] = out.get("prior_rz_touches", pd.Series(np.nan, index=out.index)) * out["opp_rz_td_allowed"]
    out["carry_x_rush_def"] = out.get("prior_carry_share", pd.Series(np.nan, index=out.index)) * out["opp_rush_td_allowed"]
    out["target_x_pass_def"] = out.get("prior_target_share", pd.Series(np.nan, index=out.index)) * out["opp_rec_td_allowed"]
    if league_id == "fbs":
        out["target_x_pass_def"] = np.nan
        out["rz_share_x_rz_def"] = np.nan
    return out


def attach_injury_signals(session: Session, frame: pd.DataFrame, league_id: str) -> pd.DataFrame:
    """Pregame injury designations reported before kickoff. Missing ≠ healthy."""
    out = frame.copy()
    out["injury_designation_out"] = np.nan
    out["injury_designation_uncertain"] = np.nan
    out["prior_inactive_rate"] = np.nan
    if frame.empty:
        return out
    inj = pd.read_sql(
        text(
            """
            SELECT i.game_id, i.player_id, i.designation, i.reported_at, g.kickoff_utc
            FROM injury_events i
            JOIN games g ON g.game_id = i.game_id
            WHERE g.league_id = :lg AND i.reported_at < g.kickoff_utc
            """
        ),
        session.connection(),
        params={"lg": league_id},
    )
    if inj.empty:
        return out
    inj = inj.sort_values("reported_at").drop_duplicates(["game_id", "player_id"], keep="last")
    inj["designation_l"] = inj["designation"].fillna("").astype(str).str.lower()
    inj["injury_designation_out"] = inj["designation_l"].isin({"out", "ir", "injured reserve", "pup"}).astype(float)
    inj["injury_designation_uncertain"] = inj["designation_l"].isin({"questionable", "doubtful"}).astype(float)
    out = out.merge(
        inj[["game_id", "player_id", "injury_designation_out", "injury_designation_uncertain"]],
        on=["game_id", "player_id"],
        how="left",
        suffixes=("", "_inj"),
    )
    # Historical inactivity from prior designations (shifted by kickoff order).
    keys = out[["player_id", "game_id", "kickoff_utc"]].drop_duplicates()
    hist = inj.merge(keys, on=["player_id", "game_id"], how="inner")
    if not hist.empty and "kickoff_utc" in hist.columns:
        hist = hist.sort_values(["player_id", "kickoff_utc"])
        hist["prior_inactive_rate"] = hist.groupby("player_id")["injury_designation_out"].transform(
            lambda s: s.shift(1).expanding(min_periods=1).mean()
        )
        out = out.drop(columns=["prior_inactive_rate"], errors="ignore").merge(
            hist[["player_id", "game_id", "prior_inactive_rate"]].drop_duplicates(["player_id", "game_id"]),
            on=["player_id", "game_id"],
            how="left",
        )
    return out


def build_opportunity_study_frame(
    session: Session,
    league_id: str,
    *,
    year_max: int = 2026,
    universe: str = "corrected",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Research frame for m6 opportunity/ablation study (not Phase 3 team-λ allocator).

    NFL default universe includes eligible zeros via prior-week roster correction.
    FBS has no weekly roster — involved box-score rows only (zeros not invented).
    Does not call Agent 1 team_td trainers.
    """
    if league_id == "fbs" and universe == "corrected":
        universe = "involved"
    meta: dict[str, Any] = {
        "league_id": league_id,
        "year_max": year_max,
        "universe": universe,
        "label": "rush_td + rec_td > 0 (pass_td excluded)",
        "fbs_rz_gl_invented": False,
        "lane": "m6_opportunity_study",
    }
    base = load_model_frame(session, league_id, year_max=year_max, universe=universe)
    if base.empty:
        return base, meta
    if hasattr(base, "attrs") and base.attrs.get("eligibility"):
        meta["eligibility"] = base.attrs["eligibility"]
    frame = enrich_opportunity_features(base, league_id)
    frame = attach_defense_matchups(session, frame, league_id)
    frame = attach_injury_signals(session, frame, league_id)
    frame = frame[frame["position_group"].isin(SKILL)].copy()
    meta["rows"] = int(len(frame))
    meta["players"] = int(frame["player_id"].nunique()) if len(frame) else 0
    meta["td_rate"] = float(frame["y_td"].mean()) if len(frame) else None
    return frame.reset_index(drop=True), meta


def feature_coverage(frame: pd.DataFrame, columns: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for col in columns:
        if col not in frame.columns:
            out[col] = 0.0
            continue
        arr = pd.to_numeric(frame[col], errors="coerce").to_numpy(dtype=float)
        out[col] = float(np.isfinite(arr).mean()) if len(arr) else 0.0
    return out


def all_candidate_features(league_id: str) -> list[str]:
    feats = list(OPPORTUNITY_FEATURES) + list(DEFENSE_FEATURES) + list(INJURY_FEATURES)
    feats.extend(
        [
            "prior_rush_td_rate",
            "prior_rec_td_rate",
            "roll3_rush_td",
            "roll3_rec_td",
            "roll3_carries",
            "roll3_targets",
        ]
    )
    if league_id == "fbs":
        blocked = {
            "prior_rz_targets",
            "prior_rz_carries",
            "prior_gl_carries",
            "prior_rz_touches",
            "prior_target_share",
            "snap_roll3",
            "snap_delta",
            "depth_rank",
            "starter",
            "roster_active",
            "rz_share_x_rz_def",
            "target_x_pass_def",
            "ngs_separation",
            "ngs_cushion",
            "ngs_yac_oe",
            "ngs_ryoe",
        }
        feats = [f for f in feats if f not in blocked]
    return feats
