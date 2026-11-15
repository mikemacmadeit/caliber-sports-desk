"""Fit upstream team predictions and shares within chronological research folds."""
from __future__ import annotations

import hashlib
import json
import pandas as pd

from spe.models.player_td.shares import attach_shares, fit_share_weights
from spe.models.team_td.model import fit_team_td_model, predict_frame


def split_calibration(frame: pd.DataFrame, fraction: float = 0.2):
    """Keep complete games and simultaneous kickoffs together."""
    times = pd.to_datetime(frame["kickoff_utc"], utc=True)
    unique = sorted(times.dropna().unique())
    if len(unique) < 3:
        return frame.copy(), frame.iloc[:0].copy()
    boundary = unique[min(len(unique) - 1, max(1, int(len(unique) * (1 - fraction))))]
    return frame.loc[times < boundary].copy(), frame.loc[times >= boundary].copy()


def fit_upstream(team_frame: pd.DataFrame, fit: pd.DataFrame, calibration: pd.DataFrame,
                 test: pd.DataFrame, league_id: str, fit_end):
    """No saved global model or later labels may enter this fold's fit."""
    boundary = pd.Timestamp(fit_end)
    if boundary.tzinfo is None:
        boundary = boundary.tz_localize("UTC")
    for name, part in (("fit", fit), ("calibration", calibration), ("test", test)):
        if part.empty:
            continue
        times = pd.to_datetime(part["kickoff_utc"], utc=True)
        if (name == "fit" and (times >= boundary).any()) or (name != "fit" and (times < boundary).any()):
            raise ValueError("Upstream fit cutoff overlaps held-out games")
    if not calibration.empty and not test.empty:
        if pd.to_datetime(calibration["kickoff_utc"], utc=True).max() >= pd.to_datetime(test["kickoff_utc"], utc=True).min():
            raise ValueError("Calibration and final test overlap")
    if team_frame.empty:
        raise ValueError("No historical team rows for independent fold fit")
    team_times = pd.to_datetime(team_frame["kickoff_utc"], utc=True)
    # Allow statistics to arrive; games immediately before the cutoff are excluded.
    team_train = team_frame.loc[team_times < boundary - pd.Timedelta(hours=48)].copy()
    if len(team_train) < 30:
        raise ValueError("Insufficient team games before fold cutoff")
    team_model = fit_team_td_model(team_train, league_id, boundary.isoformat())
    digest = hashlib.sha256(json.dumps(team_model.to_dict(), sort_keys=True, default=str).encode()).hexdigest()
    wanted = set(pd.concat([fit, calibration, test])["game_id"])
    target = team_frame.loc[team_frame["game_id"].isin(wanted)]
    preds = predict_frame(team_model, target)
    cols = ["game_id", "team_id", "lambda_off_td", "lambda_pass_td", "lambda_rush_td"]
    if "exp_possessions" in preds:
        cols.append("exp_possessions")
    preds = preds[cols].rename(columns={
        "lambda_off_td": "team_lambda_off_td", "lambda_pass_td": "team_lambda_pass_td",
        "lambda_rush_td": "team_lambda_rush_td", "exp_possessions": "team_exp_possessions"})
    if preds.duplicated(["game_id", "team_id"]).any():
        raise ValueError("Duplicate team-game predictions")
    weights = fit_share_weights(fit, league_id)
    parts = []
    for part in (fit, calibration, test):
        stale = [c for c in part if c.startswith("team_lambda_") or c == "team_exp_possessions"]
        out = part.drop(columns=stale).merge(preds, on=["game_id", "team_id"], how="left", validate="many_to_one")
        for col, multiplier in (("off", 1), ("pass", .6), ("rush", .4)):
            key = "team_lambda_" + col + "_td"
            out[key] = out[key].fillna(team_model.baseline_mean * multiplier)
        out = attach_shares(out, weights)
        out["lambda_rush_td"] = out["team_lambda_rush_td"] * out["rush_td_share"]
        out["lambda_rec_td"] = out["team_lambda_pass_td"] * out["rec_td_share"]
        out["lambda_player_td"] = out["lambda_rush_td"] + out["lambda_rec_td"]
        out["team_td_source"] = "fold_fitted:" + digest
        parts.append(out)
    return (*parts, weights, {
        "team_artifact_sha256": digest, "team_fit_cutoff": boundary.isoformat(),
        "team_last_training_kickoff": team_times.loc[team_train.index].max().isoformat(),
        "team_fit_n": len(team_train), "calibration_n": len(calibration),
        "team_model": team_model.to_dict(),
        "vintage_limitation": "Revised historical statistics; original publication vintages are unavailable.",
    })
