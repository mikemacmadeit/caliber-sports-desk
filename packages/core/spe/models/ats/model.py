"""Margin models and cover-probability calibration for ATS.

Separate coefficients per league. Complexity is earned by held-out improvement.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from spe.models.ats.labels import normal_cover_probs
from spe.models.ats.ratings import FEATURE_COLS
from spe.models.ats.spread_contract import CURRENT_SPREAD_CONTRACT, attach_spread_fields
from spe.models.metrics import (
    apply_platt,
    brier_score,
    expected_calibration_error,
    log_loss,
    mean_absolute_error,
    platt_calibrate,
)


def artifact_platt_is_valid(artifact: AtsModelArtifact) -> bool:
    """Legacy Platt was fit on M+L targets; do not apply it under the corrected contract."""
    meta = artifact.metrics or {}
    return meta.get("spread_contract") == CURRENT_SPREAD_CONTRACT


def _fit_ridge(x: np.ndarray, y: np.ndarray, alpha: float) -> tuple[np.ndarray, float]:
    """Closed-form ridge with intercept. No sklearn dependency."""
    n, p = x.shape
    x_mean = x.mean(axis=0)
    y_mean = float(y.mean())
    xc = x - x_mean
    yc = y - y_mean
    xtx = xc.T @ xc + alpha * np.eye(p)
    coef = np.linalg.solve(xtx, xc.T @ yc)
    intercept = y_mean - float(x_mean @ coef)
    return coef.astype(float), float(intercept)


@dataclass
class AtsModelArtifact:
    league_id: str
    family: str
    feature_cols: list[str]
    coef: list[float]
    intercept: float
    sigma: float
    hfa: float
    ridge_alpha: float
    platt_a: float
    platt_b: float
    train_n: int
    metrics: dict[str, Any] = field(default_factory=dict)
    model_version_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AtsModelArtifact:
        return cls(**raw)


def _design(frame: pd.DataFrame, cols: list[str]) -> np.ndarray:
    x = frame.reindex(columns=cols).apply(pd.to_numeric, errors="coerce")
    return np.nan_to_num(x.to_numpy(dtype=float), nan=0.0)


def fit_ridge_margin(
    train: pd.DataFrame,
    *,
    league_id: str,
    alpha: float = 5.0,
    feature_cols: list[str] | None = None,
) -> AtsModelArtifact:
    cols = list(feature_cols or FEATURE_COLS)
    labeled = train.dropna(subset=["margin"]).copy()
    if labeled.empty:
        raise ValueError("No labeled training rows for ATS margin model")
    x = _design(labeled, cols)
    y = labeled["margin"].to_numpy(dtype=float)
    coef, intercept = _fit_ridge(x, y, alpha=alpha)
    pred = intercept + x @ coef
    resid = y - pred
    sigma = float(max(np.std(resid), 8.0))
    # Empirical home-field residual mean as a diagnostic (already in features).
    hfa = float(labeled.loc[labeled["home_field"] > 0.5, "margin"].mean() - labeled.loc[labeled["home_field"] <= 0.5, "margin"].mean()) if labeled["home_field"].nunique() > 1 else 2.5
    return AtsModelArtifact(
        league_id=league_id,
        family="ridge_margin",
        feature_cols=cols,
        coef=[float(c) for c in coef],
        intercept=float(intercept),
        sigma=sigma,
        hfa=hfa,
        ridge_alpha=alpha,
        platt_a=0.0,
        platt_b=1.0,
        train_n=int(len(labeled)),
    )


def fit_simple_rating_baseline(train: pd.DataFrame, *, league_id: str) -> AtsModelArtifact:
    """Margin ≈ home_margin_prior - away_margin_prior + HFA."""
    labeled = train.dropna(subset=["margin"]).copy()
    hfa = 2.5
    if not labeled.empty and labeled["home_field"].nunique() > 1:
        # Solve HFA as mean residual of prior-margin difference.
        prior = labeled["home_margin_prior"] - labeled["away_margin_prior"]
        hfa = float((labeled["margin"] - prior).mean())
    cols = ["home_margin_prior", "away_margin_prior", "home_field"]
    # Encode as margin_hat = 1*home_margin_prior + (-1)*away_margin_prior + hfa*home_field
    return AtsModelArtifact(
        league_id=league_id,
        family="simple_rating",
        feature_cols=cols,
        coef=[1.0, -1.0, hfa],
        intercept=0.0,
        sigma=float(max(labeled["margin"].std() if len(labeled) else 13.5, 8.0)),
        hfa=hfa,
        ridge_alpha=0.0,
        platt_a=0.0,
        platt_b=1.0,
        train_n=int(len(labeled)),
    )


def fit_league_mean_baseline(train: pd.DataFrame, *, league_id: str) -> AtsModelArtifact:
    labeled = train.dropna(subset=["margin"]).copy()
    mean_margin = float(labeled["margin"].mean()) if len(labeled) else 2.5
    return AtsModelArtifact(
        league_id=league_id,
        family="league_home_mean",
        feature_cols=["home_field"],
        coef=[0.0],
        intercept=mean_margin,
        sigma=float(max(labeled["margin"].std() if len(labeled) else 13.5, 8.0)),
        hfa=mean_margin,
        ridge_alpha=0.0,
        platt_a=0.0,
        platt_b=1.0,
        train_n=int(len(labeled)),
    )


def predict_margin(artifact: AtsModelArtifact, frame: pd.DataFrame) -> np.ndarray:
    x = _design(frame, artifact.feature_cols)
    return artifact.intercept + x @ np.asarray(artifact.coef, dtype=float)


def predict_scores(predicted_margin: np.ndarray, frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Split margin into home/away scores using prior PPG totals."""
    home_ppg = pd.to_numeric(frame.get("home_ppg"), errors="coerce").fillna(22.0).to_numpy()
    away_ppg = pd.to_numeric(frame.get("away_ppg"), errors="coerce").fillna(22.0).to_numpy()
    total = np.clip(home_ppg + away_ppg, 30.0, 80.0)
    home = (total + predicted_margin) / 2.0
    away = (total - predicted_margin) / 2.0
    return home, away


def calibrate_cover_platt(
    artifact: AtsModelArtifact,
    frame: pd.DataFrame,
    *,
    side: str = "home",
) -> AtsModelArtifact:
    """Fit Platt scaling on cover indicators using closing lines when present.

    Expects nflverse-style ``spread_line`` (L) unless ``home_handicap`` is already attached.
    Uses sportsbook handicap h = -L for probability + target construction.
    """
    labeled = attach_spread_fields(frame.dropna(subset=["margin", "spread_line"]).copy())
    if len(labeled) < 40:
        return artifact
    margins = predict_margin(artifact, labeled)
    p_raw = []
    y = []
    for margin_hat, handicap, actual in zip(
        margins, labeled["home_handicap"], labeled["margin"], strict=True
    ):
        if handicap is None or (isinstance(handicap, float) and np.isnan(handicap)):
            continue
        probs = normal_cover_probs(float(margin_hat), float(handicap), artifact.sigma)
        p = probs["p_home_cover"] if side == "home" else probs["p_away_cover"]
        edge = float(actual) + float(handicap)
        if abs(edge) < 1e-9:
            continue
        cover = 1.0 if edge > 0 else 0.0
        if side == "away":
            cover = 1.0 - cover
        p_raw.append(p)
        y.append(cover)
    if len(y) < 40:
        return artifact
    a, b = platt_calibrate(np.asarray(p_raw), np.asarray(y))
    artifact.platt_a = a
    artifact.platt_b = b
    artifact.metrics = {
        **(artifact.metrics or {}),
        "spread_contract": CURRENT_SPREAD_CONTRACT,
        "platt_fit_n": len(y),
        "platt_side": side,
    }
    return artifact


def score_predictions(
    artifact: AtsModelArtifact,
    frame: pd.DataFrame,
    *,
    apply_calibration: bool = True,
) -> pd.DataFrame:
    out = attach_spread_fields(frame)
    out["predicted_margin"] = predict_margin(artifact, out)
    home_s, away_s = predict_scores(out["predicted_margin"].to_numpy(), out)
    out["predicted_home_score"] = home_s
    out["predicted_away_score"] = away_s
    out["margin_sigma"] = artifact.sigma
    p_home = []
    p_away = []
    p_push = []
    handicaps = out.get("home_handicap", pd.Series([np.nan] * len(out)))
    for mu, handicap in zip(out["predicted_margin"], handicaps, strict=True):
        if handicap is None or (isinstance(handicap, float) and np.isnan(handicap)):
            # No market line: report P(home wins) via threshold 0.
            probs = normal_cover_probs(float(mu), 0.0, artifact.sigma)
        else:
            # normal_cover_probs expects sportsbook handicap h.
            probs = normal_cover_probs(float(mu), float(handicap), artifact.sigma)
        p_home.append(probs["p_home_cover"])
        p_away.append(probs["p_away_cover"])
        p_push.append(probs["p_push"])
    out["p_home_cover_raw"] = p_home
    out["p_away_cover_raw"] = p_away
    out["p_push"] = p_push
    platt_ok = artifact_platt_is_valid(artifact)
    if (
        apply_calibration
        and platt_ok
        and (artifact.platt_a != 0.0 or artifact.platt_b != 1.0)
    ):
        out["p_home_cover"] = apply_platt(np.asarray(p_home), artifact.platt_a, artifact.platt_b)
        out["p_away_cover"] = 1.0 - out["p_home_cover"] - out["p_push"]
        out["p_away_cover"] = np.clip(out["p_away_cover"], 1e-4, 1 - 1e-4)
        out["calibration_applied"] = True
    else:
        out["p_home_cover"] = out["p_home_cover_raw"]
        out["p_away_cover"] = out["p_away_cover_raw"]
        out["calibration_applied"] = False
        if apply_calibration and not platt_ok and (artifact.platt_a != 0.0 or artifact.platt_b != 1.0):
            out["calibration_invalidated"] = "legacy_platt_wrong_spread_sign"
    if "market_home_margin" in out.columns:
        # Positive model_vs_market ⇒ model likes home more than the market forecast.
        out["model_vs_market"] = out["predicted_margin"] - pd.to_numeric(
            out["market_home_margin"], errors="coerce"
        )
    return out


def regression_metrics(y: np.ndarray, yhat: np.ndarray) -> dict[str, float]:
    return {
        "n": int(len(y)),
        "mae": mean_absolute_error(y, yhat),
        "rmse": float(np.sqrt(np.mean((y - yhat) ** 2))),
        "bias": float(np.mean(yhat - y)),
        "corr": float(np.corrcoef(y, yhat)[0, 1]) if len(y) > 2 else float("nan"),
    }


def cover_metrics(y: np.ndarray, p: np.ndarray) -> dict[str, float | None]:
    if len(y) == 0:
        return {"n": 0, "brier": None, "log_loss": None, "ece": None, "accuracy": None}
    return {
        "n": int(len(y)),
        "brier": brier_score(y, p),
        "log_loss": log_loss(y, p),
        "ece": expected_calibration_error(y, p),
        "accuracy": float(np.mean((p >= 0.5) == (y >= 0.5))),
        "base_rate": float(np.mean(y)),
        "mean_p": float(np.mean(p)),
    }
