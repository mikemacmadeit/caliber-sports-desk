"""Allocate team rush/receiving TDs to players.

QB passing TDs are never allocated to the QB (Anytime TD = rush + receiving only).
Shares are normalized within each team-game; residual mass is left for non-modeled offense.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

SKILL = {"QB", "RB", "WR", "TE"}
# Default residual for players outside the modeled skill set / depth pieces.
DEFAULT_OTHER_SHARE = 0.08


@dataclass
class ShareWeights:
    """Train-fit multiplicative weights for opportunity features."""

    league_id: str
    rush_carry: float = 1.0
    rush_gl: float = 2.5
    rush_td_hist: float = 1.5
    rec_target: float = 1.0
    rec_rz: float = 2.0
    rec_td_hist: float = 1.5
    pos_rush: dict[str, float] | None = None
    pos_rec: dict[str, float] | None = None
    other_share: float = DEFAULT_OTHER_SHARE
    sharpness: float = 1.0  # softmax temperature inverse

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ShareWeights:
        return cls(**{k: raw[k] for k in cls.__dataclass_fields__ if k in raw})


def default_weights(league_id: str) -> ShareWeights:
    if league_id == "fbs":
        # No invented RZ/GL — those weights stay but inputs will be NaN → ignored.
        return ShareWeights(
            league_id=league_id,
            rush_carry=1.0,
            rush_gl=0.0,
            rush_td_hist=1.8,
            rec_target=0.0,  # FBS targets often equal receptions; use receptions path
            rec_rz=0.0,
            rec_td_hist=1.8,
            pos_rush={"RB": 1.4, "QB": 0.9, "WR": 0.15, "TE": 0.2},
            pos_rec={"WR": 1.3, "TE": 1.0, "RB": 0.55, "QB": 0.05},
            other_share=0.10,
            sharpness=1.1,
        )
    return ShareWeights(
        league_id=league_id,
        rush_carry=1.0,
        rush_gl=2.5,
        rush_td_hist=1.5,
        rec_target=1.0,
        rec_rz=2.0,
        rec_td_hist=1.5,
        pos_rush={"RB": 1.35, "QB": 0.85, "WR": 0.12, "TE": 0.18},
        pos_rec={"WR": 1.25, "TE": 1.05, "RB": 0.5, "QB": 0.05},
        other_share=0.08,
        sharpness=1.0,
    )


def _col(frame: pd.DataFrame, name: str) -> np.ndarray:
    if name not in frame.columns:
        return np.zeros(len(frame), dtype=float)
    return pd.to_numeric(frame[name], errors="coerce").fillna(0.0).to_numpy(dtype=float)


def raw_rush_scores(frame: pd.DataFrame, weights: ShareWeights) -> np.ndarray:
    carries = _col(frame, "prior_carries")
    if np.allclose(carries, 0.0) and "prior_carry_share" in frame.columns:
        carries = _col(frame, "prior_carry_share") * 15.0
    gl = _col(frame, "prior_gl_carries") if weights.league_id == "nfl" else np.zeros(len(frame))
    hist = _col(frame, "prior_td_rate")  # anytime prior; rush component refined below
    rush_hist = _col(frame, "prior_rush_td") if "prior_rush_td" in frame.columns else hist * 0.4
    score = weights.rush_carry * carries + weights.rush_gl * gl + weights.rush_td_hist * rush_hist
    pos = frame["position_group"].astype(str).to_numpy()
    mult = np.array([(weights.pos_rush or {}).get(p, 0.3) for p in pos], dtype=float)
    # QB never receives passing TD share; rush score stays.
    score = score * mult
    score = np.where(pos == "WR", score * 0.5, score)  # WR rush TDs rare
    return np.clip(score, 0.0, None)


def raw_rec_scores(frame: pd.DataFrame, weights: ShareWeights) -> np.ndarray:
    if weights.league_id == "fbs":
        # Do not invent targets/RZ; receptions are the measured receiving opportunity.
        targets = _col(frame, "prior_receptions")
        rz = np.zeros(len(frame))
    else:
        targets = _col(frame, "prior_targets")
        if np.allclose(targets, 0.0) and "prior_target_share" in frame.columns:
            targets = _col(frame, "prior_target_share") * 8.0
        rz = _col(frame, "prior_rz_targets")
    hist = _col(frame, "prior_rec_td") if "prior_rec_td" in frame.columns else _col(frame, "prior_td_rate") * 0.6
    score = weights.rec_target * targets + weights.rec_rz * rz + weights.rec_td_hist * hist
    # For FBS, rec_target weight is 0 — use receptions via td_hist + explicit receptions term.
    if weights.league_id == "fbs":
        score = 1.0 * _col(frame, "prior_receptions") + weights.rec_td_hist * hist
    pos = frame["position_group"].astype(str).to_numpy()
    mult = np.array([(weights.pos_rec or {}).get(p, 0.3) for p in pos], dtype=float)
    score = score * mult
    # QB receiving TDs are rare but allowed; keep tiny. Passing TDs thrown are never allocated.
    score = np.where(pos == "QB", score * 0.15, score)
    return np.clip(score, 0.0, None)


def normalize_shares(
    frame: pd.DataFrame,
    raw: np.ndarray,
    *,
    other_share: float,
    sharpness: float,
) -> np.ndarray:
    """Softmax-normalize within (game_id, team_id). Leaves ``other_share`` for non-modeled players."""
    out = np.zeros(len(frame), dtype=float)
    if len(frame) == 0:
        return out
    keys = frame["game_id"].astype(str) + "|" + frame["team_id"].fillna("").astype(str)
    sharp = max(float(sharpness), 1e-3)
    other = float(np.clip(other_share, 0.0, 0.4))
    for key in keys.unique():
        mask = keys == key
        vals = raw[mask]
        if not np.any(vals > 0):
            # Uniform among skill players when no usage history.
            n = int(mask.sum())
            out[mask] = (1.0 - other) / max(n, 1)
            continue
        # Numerical stable softmax
        z = sharp * np.log1p(vals)
        z = z - np.max(z)
        ex = np.exp(z)
        shares = ex / ex.sum()
        out[mask] = shares * (1.0 - other)
    return out


def attach_shares(frame: pd.DataFrame, weights: ShareWeights) -> pd.DataFrame:
    out = frame.copy()
    rush_raw = raw_rush_scores(out, weights)
    rec_raw = raw_rec_scores(out, weights)
    out["rush_td_share"] = normalize_shares(
        out, rush_raw, other_share=weights.other_share, sharpness=weights.sharpness
    )
    out["rec_td_share"] = normalize_shares(
        out, rec_raw, other_share=weights.other_share, sharpness=weights.sharpness
    )
    # QB cannot claim receiving TDs from thrown passes — already tiny; clamp pass share for pure pocket QBs with no rec history.
    qb = out["position_group"].astype(str).eq("QB")
    rec_hist = _col(out, "prior_receptions")
    out.loc[qb & (rec_hist <= 0.05), "rec_td_share"] = 0.0
    # Re-normalize rec shares after QB clamp.
    if qb.any():
        out["rec_td_share"] = _renorm_column(out, "rec_td_share", weights.other_share)
    out["other_td_share_residual"] = weights.other_share
    return out


def _renorm_column(frame: pd.DataFrame, col: str, other: float) -> np.ndarray:
    keys = frame["game_id"].astype(str) + "|" + frame["team_id"].fillna("").astype(str)
    vals = frame[col].to_numpy(dtype=float)
    out = vals.copy()
    for key in keys.unique():
        mask = keys == key
        s = float(out[mask].sum())
        if s <= 1e-12:
            n = int(mask.sum())
            out[mask] = (1.0 - other) / max(n, 1)
        else:
            out[mask] = out[mask] / s * (1.0 - other)
    return out


def fit_share_weights(train: pd.DataFrame, league_id: str) -> ShareWeights:
    """Light tune of sharpness / other_share on train rush+rec TD allocation error."""
    base = default_weights(league_id)
    best = base
    best_err = float("inf")
    for sharp in (0.8, 1.0, 1.25, 1.5):
        for other in (0.05, 0.08, 0.12):
            w = ShareWeights(**{**base.to_dict(), "sharpness": sharp, "other_share": other})
            scored = attach_shares(train, w)
            team_rush = (
                scored.groupby(["game_id", "team_id"], sort=False)["rush_td"]
                .transform("sum")
                .to_numpy(dtype=float)
            )
            team_rec = (
                scored.groupby(["game_id", "team_id"], sort=False)["rec_td"]
                .transform("sum")
                .to_numpy(dtype=float)
            )
            pred_rush = scored["rush_td_share"].to_numpy(dtype=float) * team_rush
            pred_rec = scored["rec_td_share"].to_numpy(dtype=float) * team_rec
            y_rush = scored["rush_td"].fillna(0).to_numpy(dtype=float)
            y_rec = scored["rec_td"].fillna(0).to_numpy(dtype=float)
            err = float(np.mean(np.abs(pred_rush - y_rush) + np.abs(pred_rec - y_rec)))
            if err < best_err:
                best_err = err
                best = w
    return best
