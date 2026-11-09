"""Candidate Anytime TD model families for research comparison.

Families:
- hierarchical_opp: shrinkage mean + opponent/feature multipliers (M5-style)
- opportunity_poisson: 1 - exp(-λ) from opportunity features via Poisson GLM mean
- logistic_glm: Bernoulli logit on z-scored features (numpy IRLS)
- hist_gbm: sklearn HistGradientBoostingClassifier when available
- ensemble: probability average of calibrated survivors

Complexity is not assumed to win — chronological Brier decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from spe.analytics.definitions import SHRINKAGE_K
from spe.models.advanced import apply_beta, binary_probability, fit_log_beta, shrinkage_mean
from spe.models.metrics import apply_platt, brier_score, expected_calibration_error, log_loss, platt_calibrate

MIN_COVERAGE = 0.25


def _coverage(series: pd.Series) -> float:
    arr = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
    return float(np.isfinite(arr).mean()) if len(arr) else 0.0


def _z_fit(train: pd.Series) -> tuple[float, float]:
    obs = pd.to_numeric(train, errors="coerce")
    obs = obs[np.isfinite(obs.to_numpy(dtype=float))]
    if obs.empty:
        return 0.0, 1.0
    mu = float(obs.mean())
    sd = float(obs.std(ddof=0))
    return mu, 1.0 if sd < 1e-8 else sd


def _z_apply(values: pd.Series, mu: float, sd: float) -> np.ndarray:
    raw = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
    z = (raw - mu) / sd
    return np.where(np.isfinite(z), z, 0.0)


def select_usable_features(train: pd.DataFrame, candidates: list[str], *, min_coverage: float = MIN_COVERAGE) -> list[str]:
    return [c for c in candidates if c in train.columns and _coverage(train[c]) >= min_coverage]


def score_probs(y: np.ndarray, p: np.ndarray) -> dict[str, float]:
    y = np.asarray(y, dtype=float)
    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    return {
        "n": float(len(y)),
        "brier": brier_score(y, p),
        "log_loss": log_loss(y, p),
        "ece": expected_calibration_error(y, p),
        "base_rate": float(y.mean()) if len(y) else 0.0,
        "mean_p": float(p.mean()) if len(p) else 0.0,
    }


def by_position_scores(frame: pd.DataFrame, y: np.ndarray, p: np.ndarray) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    positions = frame["position_group"].to_numpy()
    for pos in sorted(set(positions)):
        mask = positions == pos
        if int(mask.sum()) < 25:
            continue
        out[str(pos)] = score_probs(y[mask], p[mask])
    return out


def by_prob_bucket(y: np.ndarray, p: np.ndarray) -> list[dict[str, float]]:
    edges = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.01]
    rows: list[dict[str, float]] = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=False):
        mask = (p >= lo) & (p < hi)
        if int(mask.sum()) < 20:
            continue
        rows.append(
            {
                "lo": float(lo),
                "hi": float(hi),
                "n": float(mask.sum()),
                "mean_p": float(p[mask].mean()),
                "mean_y": float(y[mask].mean()),
                "brier": brier_score(y[mask], p[mask]),
            }
        )
    return rows


@dataclass
class FitResult:
    name: str
    features: list[str]
    metrics: dict[str, float]
    by_position: dict[str, dict[str, float]]
    by_bucket: list[dict[str, float]]
    p_test: np.ndarray
    notes: list[str] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)


def fit_hierarchical_opp(
    train: pd.DataFrame,
    test: pd.DataFrame,
    league_id: str,
    features: list[str],
) -> FitResult:
    usable = select_usable_features(train, features)
    if "opp_def_z" not in train.columns:
        train = train.copy()
        test = test.copy()
        train["opp_def_z"] = 0.0
        test["opp_def_z"] = 0.0
    base_tr, base_te = shrinkage_mean(train, test, "anytime_tds", league_id)
    # Opponent beta on def z.
    z_tr = np.where(np.isfinite(train["opp_def_z"].to_numpy(dtype=float)), train["opp_def_z"].to_numpy(dtype=float), 0.0)
    z_te = np.where(np.isfinite(test["opp_def_z"].to_numpy(dtype=float)), test["opp_def_z"].to_numpy(dtype=float), 0.0)
    y_tr = train["anytime_tds"].fillna(0).to_numpy(dtype=float)
    opp_beta = fit_log_beta(base_tr, z_tr, y_tr)
    mean_tr = apply_beta(base_tr, z_tr, opp_beta)
    mean_te = apply_beta(base_te, z_te, opp_beta)
    betas: dict[str, float] = {"opp_def_z": float(opp_beta)}
    zmeta: dict[str, dict[str, float]] = {}
    for name in usable:
        mu, sd = _z_fit(train[name])
        zmeta[name] = {"mean": mu, "std": sd}
        x_tr = _z_apply(train[name], mu, sd)
        x_te = _z_apply(test[name], mu, sd)
        beta = fit_log_beta(mean_tr, x_tr, y_tr)
        betas[name] = float(beta)
        if beta == 0.0:
            continue
        mean_tr = apply_beta(mean_tr, x_tr, beta)
        mean_te = apply_beta(mean_te, x_te, beta)
    y_bin_tr = (y_tr > 0).astype(float)
    y_bin_te = (test["anytime_tds"].fillna(0).to_numpy(dtype=float) > 0).astype(float)
    a, b = platt_calibrate(binary_probability(mean_tr), y_bin_tr)
    p = apply_platt(binary_probability(mean_te), a, b)
    return FitResult(
        name="hierarchical_opp",
        features=[k for k, v in betas.items() if k != "opp_def_z" and v != 0.0],
        metrics=score_probs(y_bin_te, p),
        by_position=by_position_scores(test, y_bin_te, p),
        by_bucket=by_prob_bucket(y_bin_te, p),
        p_test=p,
        notes=["m5_style_shrinkage_plus_feature_betas", "platt_global"],
        extras={"betas": betas, "platt": [a, b], "feature_z": zmeta, "k_player": SHRINKAGE_K[league_id]},
    )


def _poisson_glm_mean(y: np.ndarray, x: np.ndarray, *, max_iter: int = 35) -> tuple[float, np.ndarray]:
    n, n_feat = x.shape
    z = np.column_stack([np.ones(n), x])
    beta = np.zeros(n_feat + 1)
    beta[0] = np.log(max(float(np.mean(y)), 0.05))
    for _ in range(max_iter):
        eta = np.clip(z @ beta, -8.0, 8.0)
        mu = np.exp(eta)
        w = mu
        xtw = z.T * w
        hess = xtw @ z
        hess.flat[:: n_feat + 2] += 1e-3
        grad = xtw @ ((y - mu) / np.maximum(w, 1e-8))
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            break
        beta += step
        if float(np.max(np.abs(step))) < 1e-6:
            break
    return float(beta[0]), beta[1:]


def fit_opportunity_poisson(
    train: pd.DataFrame,
    test: pd.DataFrame,
    league_id: str,
    features: list[str],
) -> FitResult:
    del league_id
    usable = select_usable_features(train, features)
    if not usable:
        p = np.full(len(test), float((train["anytime_tds"].fillna(0) > 0).mean() if len(train) else 0.15))
        y = (test["anytime_tds"].fillna(0).to_numpy(dtype=float) > 0).astype(float)
        return FitResult("opportunity_poisson", [], score_probs(y, p), {}, [], p, notes=["no_usable_features"])
    zmeta = {c: dict(zip(("mean", "std"), _z_fit(train[c]), strict=False)) for c in usable}
    x_tr = np.column_stack([_z_apply(train[c], zmeta[c]["mean"], zmeta[c]["std"]) for c in usable])
    x_te = np.column_stack([_z_apply(test[c], zmeta[c]["mean"], zmeta[c]["std"]) for c in usable])
    y_count = train["anytime_tds"].fillna(0).to_numpy(dtype=float)
    intercept, coef = _poisson_glm_mean(y_count, x_tr)
    mean_tr = np.exp(np.clip(intercept + x_tr @ coef, -8.0, 8.0))
    mean_te = np.exp(np.clip(intercept + x_te @ coef, -8.0, 8.0))
    y_bin_tr = (y_count > 0).astype(float)
    y_bin_te = (test["anytime_tds"].fillna(0).to_numpy(dtype=float) > 0).astype(float)
    a, b = platt_calibrate(binary_probability(mean_tr), y_bin_tr)
    p = apply_platt(binary_probability(mean_te), a, b)
    return FitResult(
        name="opportunity_poisson",
        features=usable,
        metrics=score_probs(y_bin_te, p),
        by_position=by_position_scores(test, y_bin_te, p),
        by_bucket=by_prob_bucket(y_bin_te, p),
        p_test=p,
        notes=["poisson_mean_then_1_minus_exp", "platt_global"],
        extras={"intercept": intercept, "coef": coef.tolist(), "platt": [a, b], "feature_z": zmeta},
    )


def _logistic_irls(y: np.ndarray, x: np.ndarray, *, max_iter: int = 40) -> tuple[float, np.ndarray]:
    n, n_feat = x.shape
    z = np.column_stack([np.ones(n), x])
    beta = np.zeros(n_feat + 1)
    for _ in range(max_iter):
        eta = np.clip(z @ beta, -12.0, 12.0)
        prob = 1.0 / (1.0 + np.exp(-eta))
        w = np.clip(prob * (1 - prob), 1e-6, None)
        xtw = z.T * w
        hess = xtw @ z
        hess.flat[:: n_feat + 2] += 1e-3
        grad = z.T @ (y - prob)
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            break
        beta += step
        if float(np.max(np.abs(step))) < 1e-6:
            break
    return float(beta[0]), beta[1:]


def fit_logistic_glm(
    train: pd.DataFrame,
    test: pd.DataFrame,
    league_id: str,
    features: list[str],
) -> FitResult:
    del league_id
    usable = select_usable_features(train, features)
    y_tr = (train["anytime_tds"].fillna(0).to_numpy(dtype=float) > 0).astype(float)
    y_te = (test["anytime_tds"].fillna(0).to_numpy(dtype=float) > 0).astype(float)
    if not usable:
        p = np.full(len(test), float(y_tr.mean() if len(y_tr) else 0.15))
        return FitResult("logistic_glm", [], score_probs(y_te, p), {}, [], p, notes=["no_usable_features"])
    zmeta = {c: dict(zip(("mean", "std"), _z_fit(train[c]), strict=False)) for c in usable}
    x_tr = np.column_stack([_z_apply(train[c], zmeta[c]["mean"], zmeta[c]["std"]) for c in usable])
    x_te = np.column_stack([_z_apply(test[c], zmeta[c]["mean"], zmeta[c]["std"]) for c in usable])
    intercept, coef = _logistic_irls(y_tr, x_tr)
    p_tr = 1.0 / (1.0 + np.exp(-np.clip(intercept + x_tr @ coef, -12, 12)))
    p_raw = 1.0 / (1.0 + np.exp(-np.clip(intercept + x_te @ coef, -12, 12)))
    a, b = platt_calibrate(p_tr, y_tr)
    p = apply_platt(p_raw, a, b)
    return FitResult(
        name="logistic_glm",
        features=usable,
        metrics=score_probs(y_te, p),
        by_position=by_position_scores(test, y_te, p),
        by_bucket=by_prob_bucket(y_te, p),
        p_test=p,
        notes=["bernoulli_logit_irls", "platt_global"],
        extras={"intercept": intercept, "coef": coef.tolist(), "platt": [a, b], "feature_z": zmeta},
    )


def fit_hist_gbm(
    train: pd.DataFrame,
    test: pd.DataFrame,
    league_id: str,
    features: list[str],
) -> FitResult:
    del league_id
    usable = select_usable_features(train, features, min_coverage=0.15)
    y_tr = (train["anytime_tds"].fillna(0).to_numpy(dtype=float) > 0).astype(int)
    y_te = (test["anytime_tds"].fillna(0).to_numpy(dtype=float) > 0).astype(float)
    try:
        from sklearn.ensemble import HistGradientBoostingClassifier
    except ImportError:
        p = np.full(len(test), float(y_tr.mean() if len(y_tr) else 0.15))
        return FitResult("hist_gbm", [], score_probs(y_te, p), {}, [], p, notes=["sklearn_unavailable"])
    if not usable or len(train) < 400:
        p = np.full(len(test), float(y_tr.mean() if len(y_tr) else 0.15))
        return FitResult("hist_gbm", usable, score_probs(y_te, p), {}, [], p, notes=["insufficient_rows"])
    x_tr = np.column_stack([pd.to_numeric(train[c], errors="coerce").to_numpy(dtype=float) for c in usable])
    x_te = np.column_stack([pd.to_numeric(test[c], errors="coerce").to_numpy(dtype=float) for c in usable])
    model = HistGradientBoostingClassifier(
        max_depth=4,
        learning_rate=0.05,
        max_iter=120,
        l2_regularization=0.1,
        early_stopping=True,
        validation_fraction=0.1,
        random_state=7,
    )
    model.fit(x_tr, y_tr)
    p_tr = model.predict_proba(x_tr)[:, 1]
    p_raw = model.predict_proba(x_te)[:, 1]
    a, b = platt_calibrate(p_tr, y_tr.astype(float))
    p = apply_platt(p_raw, a, b)
    return FitResult(
        name="hist_gbm",
        features=usable,
        metrics=score_probs(y_te, p),
        by_position=by_position_scores(test, y_te, p),
        by_bucket=by_prob_bucket(y_te, p),
        p_test=p,
        notes=["sklearn_hist_gradient_boosting", "platt_global", "native_nan_support"],
        extras={"platt": [a, b]},
    )


def fit_ensemble(fits: list[FitResult], test: pd.DataFrame) -> FitResult:
    usable = [f for f in fits if f.p_test is not None and len(f.p_test) == len(test) and f.metrics.get("n", 0) > 0]
    y = (test["anytime_tds"].fillna(0).to_numpy(dtype=float) > 0).astype(float)
    if not usable:
        p = np.full(len(test), float(y.mean() if len(y) else 0.15))
        return FitResult("ensemble", [], score_probs(y, p), {}, [], p, notes=["empty"])
    # Inverse-Brier weights on the same test window would leak — equal weight on calibrated members.
    stack = np.column_stack([f.p_test for f in usable])
    p = np.clip(stack.mean(axis=1), 1e-6, 1 - 1e-6)
    return FitResult(
        name="ensemble",
        features=sorted({feat for f in usable for feat in f.features}),
        metrics=score_probs(y, p),
        by_position=by_position_scores(test, y, p),
        by_bucket=by_prob_bucket(y, p),
        p_test=p,
        notes=["equal_weight_calibrated_members", *[f.name for f in usable]],
        extras={"members": [f.name for f in usable]},
    )


FAMILY_FITTERS = {
    "hierarchical_opp": fit_hierarchical_opp,
    "opportunity_poisson": fit_opportunity_poisson,
    "logistic_glm": fit_logistic_glm,
    "hist_gbm": fit_hist_gbm,
}
