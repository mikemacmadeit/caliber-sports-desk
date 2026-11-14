"""Player Anytime TD probabilities from team λ × opportunity shares.

P(ATD) = avail_factor * (1 - exp(-λ_player)), then Platt-calibrated.
Uncertainty from Poisson rate bands + availability mixture — not fixed ±ranges.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from spe.models.advanced import apply_beta, binary_probability, fit_log_beta
from spe.models.metrics import apply_platt, brier_score, expected_calibration_error, log_loss, platt_calibrate
from spe.models.player_td.shares import ShareWeights, attach_shares, fit_share_weights
from spe.models.player_td.temporal import split_games

MATCHUP_FEATURES = (
    "opp_def_z",
    "opp_adj_def",
    "usage_x_defense",
    "own_off_epa",
    "team_rz_td",
    "is_home",
    "workload_delta",
    "snap_roll3",
)


@dataclass
class PlayerTdModel:
    league_id: str
    share_weights: dict[str, Any]
    feature_betas: dict[str, float]
    feature_z: dict[str, dict[str, float]]
    opp_beta: float
    platt_a: float
    platt_b: float
    platt_by_pos: dict[str, list[float]]
    train_end: str
    notes: list[str] = field(default_factory=list)
    calibration_start: str | None = None
    calibration_end: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PlayerTdModel:
        return cls(**{k: raw[k] for k in cls.__dataclass_fields__ if k in raw})


def _z_fit(series: pd.Series) -> tuple[float, float]:
    vals = pd.to_numeric(series, errors="coerce")
    vals = vals[np.isfinite(vals.to_numpy(dtype=float))]
    if vals.empty:
        return 0.0, 1.0
    mu = float(vals.mean())
    sd = float(vals.std(ddof=0))
    return mu, 1.0 if sd < 1e-8 else sd


def apply_matchup_adjustments(
    lam: np.ndarray,
    frame: pd.DataFrame,
    feature_betas: dict[str, float],
    feature_z: dict[str, dict[str, float]],
    opp_beta: float,
) -> np.ndarray:
    out = np.clip(np.asarray(lam, dtype=float), 1e-8, None)
    if "opp_def_z" in frame.columns and opp_beta:
        z = pd.to_numeric(frame["opp_def_z"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
        out = apply_beta(out, z, opp_beta)
    for name, beta in feature_betas.items():
        if not beta or name not in frame.columns:
            continue
        meta = feature_z.get(name) or {"mean": 0.0, "std": 1.0}
        raw = pd.to_numeric(frame[name], errors="coerce").to_numpy(dtype=float)
        z = (raw - float(meta.get("mean") or 0.0)) / (float(meta.get("std") or 1.0) or 1.0)
        z = np.where(np.isfinite(z), z, 0.0)
        # Missing measurements: do not apply beta (leave mean unchanged for those rows).
        missing = ~np.isfinite(raw)
        adjusted = apply_beta(out, z, float(beta))
        out = np.where(missing, out, adjusted)
    return np.clip(out, 1e-8, None)


def availability_factor(status: pd.Series | None, prob: pd.Series | None, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Return (multiplier for λ path, p_active for mixture uncertainty).

    Missing reports are never treated as healthy: unknown → factor 1.0 for research
    mean but p_active marked nan so uncertainty widens.
    """
    factor = np.ones(n, dtype=float)
    p_active = np.full(n, np.nan, dtype=float)
    if status is None:
        return factor, p_active
    st = status.fillna("unknown").astype(str).str.lower().to_numpy()
    if prob is not None:
        pr = pd.to_numeric(prob, errors="coerce").to_numpy(dtype=float)
    else:
        pr = np.full(n, np.nan)
    for i, s in enumerate(st):
        if s in {"confirmed_inactive", "out", "doubtful"} and np.isfinite(pr[i]) and pr[i] <= 0.35:
            factor[i] = float(pr[i])
            p_active[i] = float(pr[i])
        elif s in {"expected_active", "probable"}:
            factor[i] = float(pr[i]) if np.isfinite(pr[i]) else 0.92
            p_active[i] = factor[i]
        elif s in {"uncertain", "questionable"}:
            factor[i] = float(pr[i]) if np.isfinite(pr[i]) else 0.7
            p_active[i] = factor[i]
        else:
            # unknown — do not assume healthy; keep rate but flag via nan p_active
            factor[i] = 1.0
            p_active[i] = np.nan
    return factor, p_active


def poisson_atd_probability(lam: np.ndarray) -> np.ndarray:
    return binary_probability(lam)


def uncertainty_bands(
    lam: np.ndarray,
    p: np.ndarray,
    p_active: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Descriptive sensitivity ranges; not validated statistical confidence intervals."""
    lam = np.clip(np.asarray(lam, dtype=float), 1e-8, None)
    sd = np.sqrt(lam)
    p_hi_rate = poisson_atd_probability(lam + 1.64 * sd)
    p_lo_rate = poisson_atd_probability(np.clip(lam - 1.64 * sd, 1e-8, None))
    # Mixture with availability when known.
    known = np.isfinite(p_active)
    pa = np.where(known, p_active, 1.0)
    p_lo = pa * p_lo_rate
    p_hi = pa * p_hi_rate + (1.0 - pa) * 0.0
    # Unknown availability: widen toward [0.5*p, min(1, 1.5*p)] research band.
    p_lo = np.where(known, p_lo, np.clip(p * 0.5, 1e-4, 1.0))
    p_hi = np.where(known, p_hi, np.clip(np.maximum(p * 1.5, p + 0.08), 1e-4, 0.999))
    p_lo = np.minimum(p_lo, p)
    p_hi = np.maximum(p_hi, p)
    return p_lo, p_hi


def fit_player_td_model(train: pd.DataFrame, league_id: str, train_end: str, weights: ShareWeights) -> PlayerTdModel:
    train, calibration = split_games(train, 0.8)
    # All fitted components, including shares, exclude the calibration period.
    weights = fit_share_weights(train, league_id)
    train = attach_shares(train, weights)
    calibration = attach_shares(calibration, weights)
    for part in (train, calibration):
        part["lambda_player_td"] = (part["team_lambda_rush_td"] * part["rush_td_share"]
                                    + part["team_lambda_pass_td"] * part["rec_td_share"])
    base = train["lambda_player_td"].to_numpy(dtype=float)

    # Opponent beta from opp_def_z when present.
    if "opp_def_z" in train.columns:
        z = pd.to_numeric(train["opp_def_z"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
        opp_beta = fit_log_beta(base, z, train["anytime_tds"].fillna(0).to_numpy(dtype=float))
        base = apply_beta(base, z, opp_beta)
    else:
        opp_beta = 0.0

    feature_betas: dict[str, float] = {}
    feature_z: dict[str, dict[str, float]] = {}
    for name in MATCHUP_FEATURES:
        if name in {"opp_def_z"} or name not in train.columns:
            continue
        if league_id == "fbs" and name in {"snap_roll3", "prior_rz_targets", "prior_gl_carries"}:
            continue
        cov = float(np.isfinite(pd.to_numeric(train[name], errors="coerce").to_numpy(dtype=float)).mean())
        if cov < 0.25:
            continue
        mu, sd = _z_fit(train[name])
        feature_z[name] = {"mean": mu, "std": sd}
        raw = pd.to_numeric(train[name], errors="coerce").to_numpy(dtype=float)
        z = np.where(np.isfinite(raw), (raw - mu) / sd, 0.0)
        # Only fit on observed rows.
        mask = np.isfinite(raw)
        if int(mask.sum()) < 80:
            continue
        beta = fit_log_beta(base[mask], z[mask], train["anytime_tds"].fillna(0).to_numpy(dtype=float)[mask])
        if beta == 0.0:
            continue
        feature_betas[name] = beta
        adjusted = apply_beta(base, z, beta)
        base = np.where(mask, adjusted, base)

    # Calibrate frozen coefficients on later games, never on the fitting predictions.
    a, b = 0.0, 1.0
    platt_by_pos: dict[str, list[float]] = {}
    calibrated = len(calibration) >= 80 and (calibration["anytime_tds"].fillna(0) > 0).nunique() == 2
    if calibrated:
        cy = (calibration["anytime_tds"].fillna(0) > 0).to_numpy(dtype=float)
        cp = poisson_atd_probability(apply_matchup_adjustments(
            calibration["lambda_player_td"].to_numpy(), calibration,
            feature_betas, feature_z, opp_beta))
        a, b = platt_calibrate(cp, cy)
        for pos in calibration["position_group"].unique():
            mask = calibration["position_group"].eq(pos).to_numpy()
            if int(mask.sum()) >= 80 and len(np.unique(cy[mask])) == 2:
                aa, bb = platt_calibrate(cp[mask], cy[mask])
                platt_by_pos[str(pos)] = [aa, bb]

    return PlayerTdModel(
        league_id=league_id,
        share_weights=weights.to_dict(),
        feature_betas=feature_betas,
        feature_z=feature_z,
        opp_beta=float(opp_beta),
        platt_a=float(a),
        platt_b=float(b),
        platt_by_pos=platt_by_pos,
        train_end=str(pd.to_datetime(train["kickoff_utc"], utc=True).max()),
        calibration_start=str(calibration["kickoff_utc"].min()) if calibrated else None,
        calibration_end=str(calibration["kickoff_utc"].max()) if calibrated else None,
        notes=[
            "separate_temporal_calibration" if calibrated else "uncalibrated_insufficient_later_games",
            "phase3_player_td_v1",
            "team_lambda_x_share",
            "qb_pass_td_excluded",
            "rush_plus_rec_anytime",
            "research_only",
            "not_promoted",
            "fbs_no_invented_rz_gl" if league_id == "fbs" else "nfl_rz_gl_priors",
        ],
    )


def predict_frame(model: PlayerTdModel, frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    if "separate_temporal_calibration" in model.notes or "uncalibrated_insufficient_later_games" in model.notes:
        out = attach_shares(out, ShareWeights.from_dict(model.share_weights))
        out["lambda_player_td"] = (out["team_lambda_rush_td"] * out["rush_td_share"]
                                  + out["team_lambda_pass_td"] * out["rec_td_share"])
    if "lambda_player_td" not in out.columns:
        out["lambda_rush_td"] = out["team_lambda_rush_td"] * out["rush_td_share"]
        out["lambda_rec_td"] = out["team_lambda_pass_td"] * out["rec_td_share"]
        out["lambda_player_td"] = out["lambda_rush_td"] + out["lambda_rec_td"]

    lam = apply_matchup_adjustments(
        out["lambda_player_td"].to_numpy(dtype=float),
        out,
        model.feature_betas,
        model.feature_z,
        model.opp_beta,
    )
    status = out["availability_status"] if "availability_status" in out.columns else None
    aprog = out["availability_prob"] if "availability_prob" in out.columns else None
    factor, p_active = availability_factor(status, aprog, len(out))
    lam = lam * factor
    p_raw = poisson_atd_probability(lam)

    # Position-specific Platt when available.
    p = apply_platt(p_raw, model.platt_a, model.platt_b)
    if model.platt_by_pos:
        for pos, ab in model.platt_by_pos.items():
            mask = out["position_group"].astype(str).eq(pos).to_numpy()
            if not mask.any():
                continue
            p = np.where(mask, apply_platt(p_raw, ab[0], ab[1]), p)

    # Confirmed inactive → force near-zero after calibration.
    if status is not None:
        st = status.fillna("").astype(str).str.lower()
        inactive = st.isin({"confirmed_inactive", "out"}) | (
            st.eq("doubtful") & (pd.to_numeric(aprog, errors="coerce").fillna(1) <= 0.05
                                if aprog is not None else False)
        )
        p = np.where(inactive.to_numpy(), np.minimum(p, 0.02), p)
        lam = np.where(inactive.to_numpy(), np.minimum(lam, 0.02), lam)

    p_lo, p_hi = uncertainty_bands(lam, p, p_active)
    out["lambda_player_td_adj"] = lam
    out["p_atd"] = p
    out["p_atd_low"] = p_lo
    out["p_atd_high"] = p_hi
    out["uncertainty_status"] = "unvalidated_sensitivity_range"
    out["p_active"] = p_active
    return out


def evaluate_binary(frame: pd.DataFrame, p_col: str = "p_atd") -> dict[str, Any]:
    y = frame["y_atd"].to_numpy(dtype=float) if "y_atd" in frame.columns else (frame["anytime_tds"].fillna(0) > 0).astype(float)
    p = frame[p_col].to_numpy(dtype=float)
    by_pos: dict[str, Any] = {}
    for pos, part in frame.groupby("position_group"):
        yy = part["y_atd"].to_numpy(dtype=float) if "y_atd" in part.columns else (part["anytime_tds"].fillna(0) > 0).astype(float)
        pp = part[p_col].to_numpy(dtype=float)
        by_pos[str(pos)] = {
            "n": int(len(part)),
            "brier": brier_score(yy, pp),
            "log_loss": log_loss(yy, pp),
            "ece": expected_calibration_error(yy, pp),
            "base_rate": float(np.mean(yy)),
            "mean_p": float(np.mean(pp)),
        }
    # Probability ranges
    ranges: dict[str, Any] = {}
    bins = [(0.0, 0.1), (0.1, 0.2), (0.2, 0.35), (0.35, 0.5), (0.5, 1.01)]
    for lo, hi in bins:
        mask = (p >= lo) & (p < hi)
        if int(mask.sum()) < 20:
            continue
        ranges[f"{lo:.2f}-{hi:.2f}"] = {
            "n": int(mask.sum()),
            "mean_p": float(np.mean(p[mask])),
            "base_rate": float(np.mean(y[mask])),
            "brier": brier_score(y[mask], p[mask]),
        }
    return {
        "n": int(len(frame)),
        "brier": brier_score(y, p),
        "log_loss": log_loss(y, p),
        "ece": expected_calibration_error(y, p),
        "base_rate": float(np.mean(y)),
        "mean_p": float(np.mean(p)),
        "by_position": by_pos,
        "by_probability_range": ranges,
    }
