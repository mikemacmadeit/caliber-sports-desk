"""Pregame player TD opportunity frame joined to Phase 2 team TD expectations."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.models.advanced import add_player_priors, load_model_frame
from spe.models.player_td.shares import ShareWeights, attach_shares, fit_share_weights
from spe.models.rolling_td import prepare_td_frame
from spe.models.team_td.dataset import build_team_td_frame
from spe.models.team_td.model import predict_frame
from spe.models.team_td.train import load_team_td_model

SKILL = {"QB", "RB", "WR", "TE"}


def _add_component_td_priors(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.sort_values(["player_id", "kickoff_utc"]).copy()
    group = out.groupby("player_id", sort=False)
    for src, dest in (("rush_td", "prior_rush_td"), ("rec_td", "prior_rec_td")):
        if src not in out.columns:
            out[dest] = np.nan
            continue
        out[dest] = group[src].transform(lambda s: s.shift(1).expanding(min_periods=1).mean())
    if "prior_carries" not in out.columns and "carries" in out.columns:
        out["prior_carries"] = group["carries"].transform(lambda s: s.shift(1).expanding(min_periods=1).mean())
    if "prior_receptions" not in out.columns and "receptions" in out.columns:
        out["prior_receptions"] = group["receptions"].transform(
            lambda s: s.shift(1).expanding(min_periods=1).mean()
        )
    return out


def attach_team_td_lambdas(session: Session, players: pd.DataFrame, league_id: str) -> pd.DataFrame:
    """Merge Phase 2 predicted team λ (prior features only) onto player rows."""
    model = load_team_td_model(league_id)
    if model is None:
        # Fallback: use prior team TD rates from player environment if Phase 2 missing.
        out = players.copy()
        out["team_lambda_off_td"] = out.get("prior_td_rate", pd.Series(np.nan, index=out.index)).fillna(0.2) * 4.0
        out["team_lambda_pass_td"] = out["team_lambda_off_td"] * 0.6
        out["team_lambda_rush_td"] = out["team_lambda_off_td"] * 0.4
        out["team_td_source"] = "fallback_prior"
        return out

    print(f"[player_td] building team TD frame for {league_id}…", flush=True)
    team = build_team_td_frame(session, league_id)
    if team.empty:
        out = players.copy()
        out["team_lambda_off_td"] = 2.3 if league_id == "nfl" else 3.0
        out["team_lambda_pass_td"] = out["team_lambda_off_td"] * 0.6
        out["team_lambda_rush_td"] = out["team_lambda_off_td"] * 0.4
        out["team_td_source"] = "league_default"
        return out

    pred = predict_frame(model, team)
    team_pred = pred[
        ["game_id", "team_id", "lambda_off_td", "lambda_pass_td", "lambda_rush_td", "exp_possessions"]
    ].rename(
        columns={
            "lambda_off_td": "team_lambda_off_td",
            "lambda_pass_td": "team_lambda_pass_td",
            "lambda_rush_td": "team_lambda_rush_td",
            "exp_possessions": "team_exp_possessions",
        }
    )
    out = players.merge(team_pred, on=["game_id", "team_id"], how="left")
    # Fill rare misses with league baseline from the model.
    base = float(model.baseline_mean)
    out["team_lambda_off_td"] = out["team_lambda_off_td"].fillna(base)
    out["team_lambda_pass_td"] = out["team_lambda_pass_td"].fillna(base * 0.6)
    out["team_lambda_rush_td"] = out["team_lambda_rush_td"].fillna(base * 0.4)
    out["team_td_source"] = "phase2_team_td_v1"
    return out


def build_player_td_frame(
    session: Session,
    league_id: str,
    *,
    year_max: int = 2026,
    universe: str = "corrected",
) -> tuple[pd.DataFrame, ShareWeights]:
    """Skill-position player-games with prior features, shares, and team λ.

    Uses the canonical opportunity feature pipeline (shared with M5/M6 research),
    then attaches team_td_v1 λ and opportunity shares.
    ``universe=corrected`` includes eligible zeros where the eligibility layer supports it
    (NFL). FBS stays box-score involved when corrected adds nothing.
    """
    print(f"[player_td] loading canonical opportunity frame {league_id} universe={universe}…", flush=True)
    from spe.research.atd_features import build_canonical_opportunity_frame

    frame = pd.DataFrame()
    try:
        frame, _meta = build_canonical_opportunity_frame(
            session, league_id, year_max=year_max, universe=universe
        )
    except Exception as exc:  # pragma: no cover - migrate-safe fallback
        print(f"[player_td] canonical frame failed ({exc}); using legacy path", flush=True)

    if frame.empty:
        if universe == "corrected":
            frame = load_model_frame(session, league_id, year_max=year_max, universe="corrected")
            if not frame.empty:
                frame = frame[frame["position_group"].isin(SKILL)].copy()
                if "anytime_tds" not in frame.columns:
                    frame["anytime_tds"] = frame["rush_td"].fillna(0) + frame["rec_td"].fillna(0)
                from spe.models.rolling_td import _add_td_recency

                frame = _add_td_recency(frame)
                if league_id == "fbs":
                    for col in ("prior_rz_targets", "prior_gl_carries", "prior_target_share", "snap_roll3"):
                        if col in frame.columns:
                            frame[col] = np.nan
            else:
                frame = prepare_td_frame(session, league_id, year_max=year_max)
        else:
            frame = prepare_td_frame(session, league_id, year_max=year_max)

    if frame.empty:
        return frame, default_or_fit_empty(league_id)

    frame = _add_component_td_priors(frame)
    if "prior_carries" not in frame.columns:
        frame = add_player_priors(frame, league_id)
        frame = _add_component_td_priors(frame)

    frame = attach_team_td_lambdas(session, frame, league_id)
    kick = pd.to_datetime(frame["kickoff_utc"], utc=True)
    share_train = frame.loc[kick < pd.Timestamp("2024-08-01", tz="UTC")]
    if len(share_train) < 500:
        share_train = frame.iloc[: max(int(len(frame) * 0.6), 1)]
    weights = fit_share_weights(share_train, league_id)
    frame = attach_shares(frame, weights)
    frame["lambda_rush_td"] = frame["team_lambda_rush_td"] * frame["rush_td_share"]
    frame["lambda_rec_td"] = frame["team_lambda_pass_td"] * frame["rec_td_share"]
    frame["lambda_player_td"] = frame["lambda_rush_td"] + frame["lambda_rec_td"]
    frame["y_atd"] = (frame["anytime_tds"].fillna(0) > 0).astype(float)
    frame["league_id"] = league_id
    return frame.reset_index(drop=True), weights


def default_or_fit_empty(league_id: str) -> ShareWeights:
    from spe.models.player_td.shares import default_weights

    return default_weights(league_id)


def chronological_player_split(
    frame: pd.DataFrame,
    *,
    train_end: datetime,
    test_start: datetime,
    test_end: datetime,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    kick = pd.to_datetime(frame["kickoff_utc"], utc=True)
    te = pd.Timestamp(train_end)
    ts = pd.Timestamp(test_start)
    tend = pd.Timestamp(test_end)
    if te.tzinfo is None:
        te = te.tz_localize("UTC")
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    if tend.tzinfo is None:
        tend = tend.tz_localize("UTC")
    return frame.loc[kick < te].copy(), frame.loc[(kick >= ts) & (kick <= tend)].copy()
