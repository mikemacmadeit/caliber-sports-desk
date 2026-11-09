"""Poisson team-TD opportunity model (numpy IRLS, no sklearn).

Separate NFL / FBS fits. Offensive TDs only — defensive/ST TDs are excluded.
Pass and rush means are constrained to sum to the total mean.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from spe.models.team_td.dataset import FEATURE_COLS

BUCKETS = (0, 1, 2, 3, 4, 5)  # 5 == 5+


@dataclass
class TeamTdModel:
    league_id: str
    feature_names: list[str]
    coef_total: list[float]
    intercept_total: float
    coef_pass: list[float]
    intercept_pass: float
    coef_rush: list[float]
    intercept_rush: float
    feature_mean: list[float]
    feature_std: list[float]
    baseline_mean: float
    train_end: str
    notes: list[str] = field(default_factory=list)
    aux_means: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TeamTdModel:
        return cls(**{k: raw[k] for k in cls.__dataclass_fields__ if k in raw})


def _design(frame: pd.DataFrame, names: list[str], mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    x = np.column_stack([pd.to_numeric(frame[c], errors="coerce").fillna(0.0).to_numpy(dtype=float) for c in names])
    return (x - mean) / np.where(std < 1e-8, 1.0, std)


def fit_poisson_glm(
    frame: pd.DataFrame,
    y_col: str,
    feature_names: list[str],
    *,
    max_iter: int = 40,
) -> tuple[float, np.ndarray, np.ndarray, np.ndarray]:
    """IRLS Poisson log-link. Returns intercept, coef, feature_mean, feature_std."""
    y = pd.to_numeric(frame[y_col], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    x_raw = np.column_stack(
        [pd.to_numeric(frame[c], errors="coerce").fillna(0.0).to_numpy(dtype=float) for c in feature_names]
    )
    mean = np.nanmean(x_raw, axis=0)
    std = np.nanstd(x_raw, axis=0)
    std = np.where(std < 1e-8, 1.0, std)
    x = (x_raw - mean) / std
    n, p = x.shape
    # Design with intercept.
    z = np.column_stack([np.ones(n), x])
    beta = np.zeros(p + 1)
    beta[0] = np.log(max(float(np.mean(y)), 0.05))
    for _ in range(max_iter):
        eta = z @ beta
        eta = np.clip(eta, -8.0, 8.0)
        mu = np.exp(eta)
        w = mu
        # Ridge for stability.
        ridge = 1e-3
        xtw = z.T * w
        hess = xtw @ z
        hess.flat[:: p + 2] += ridge
        grad = xtw @ ((y - mu) / np.maximum(w, 1e-8))
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            break
        beta = beta + step
        if float(np.max(np.abs(step))) < 1e-6:
            break
    return float(beta[0]), beta[1:].astype(float), mean.astype(float), std.astype(float)


def predict_mean(intercept: float, coef: np.ndarray, x: np.ndarray) -> np.ndarray:
    eta = intercept + x @ coef
    return np.exp(np.clip(eta, -8.0, 8.0))


def poisson_pmf_buckets(lam: np.ndarray) -> np.ndarray:
    """Return (n, 6) probabilities for counts 0,1,2,3,4,5+."""
    lam = np.clip(np.asarray(lam, dtype=float), 1e-8, None)
    n = len(lam)
    out = np.zeros((n, 6), dtype=float)
    term = np.exp(-lam)
    out[:, 0] = term
    cdf = term.copy()
    for k in range(1, 5):
        term = term * lam / k
        out[:, k] = term
        cdf = cdf + term
    out[:, 5] = np.clip(1.0 - cdf, 0.0, 1.0)
    # Renormalize tiny numeric drift.
    out = out / np.maximum(out.sum(axis=1, keepdims=True), 1e-12)
    return out


def multinomial_log_loss(y: np.ndarray, probs: np.ndarray, eps: float = 1e-15) -> float:
    """y is integer counts; map >=5 to bucket 5."""
    buckets = np.where(y >= 5, 5, y).astype(int)
    p = np.clip(probs[np.arange(len(buckets)), buckets], eps, 1.0)
    return float(-np.mean(np.log(p)))


def count_accuracy(y: np.ndarray, lam: np.ndarray) -> dict[str, float]:
    pred = np.rint(lam)
    exact = float(np.mean(pred == y))
    within1 = float(np.mean(np.abs(pred - y) <= 1))
    mae = float(np.mean(np.abs(lam - y)))
    return {"exact": exact, "within_1": within1, "mae": mae}


def calibration_by_bucket(y: np.ndarray, probs: np.ndarray) -> list[dict[str, float]]:
    buckets = np.where(y >= 5, 5, y).astype(int)
    rows = []
    for b in range(6):
        pred_p = float(np.mean(probs[:, b]))
        emp = float(np.mean(buckets == b))
        rows.append({"bucket": b if b < 5 else "5+", "predicted": pred_p, "observed": emp, "gap": pred_p - emp})
    return rows


def fit_team_td_model(train: pd.DataFrame, league_id: str, train_end: str) -> TeamTdModel:
    names = [c for c in FEATURE_COLS if c in train.columns]
    # Prefer features with variance; drop constants.
    keep: list[str] = []
    for c in names:
        vals = pd.to_numeric(train[c], errors="coerce")
        if vals.nunique(dropna=True) >= 2 and float(vals.std(skipna=True) or 0) > 1e-8:
            keep.append(c)
    if not keep:
        keep = ["prior_off_td", "opp_prior_off_td_allowed", "home_field"]
        for c in keep:
            if c not in train.columns:
                train = train.copy()
                train[c] = 0.0

    intercept_t, coef_t, mean, std = fit_poisson_glm(train, "off_td", keep)
    intercept_p, coef_p, _, _ = fit_poisson_glm(train, "pass_td", keep)
    intercept_r, coef_r, _, _ = fit_poisson_glm(train, "rush_td", keep)
    baseline = float(pd.to_numeric(train["off_td"], errors="coerce").mean())
    aux = {
        "possessions": float(pd.to_numeric(train["possessions"], errors="coerce").mean()),
        "off_plays": float(pd.to_numeric(train["off_plays"], errors="coerce").mean()),
        "rz_plays": float(pd.to_numeric(train["rz_plays"], errors="coerce").mean()),
        "scoring_drives": float(pd.to_numeric(train["scoring_drives"], errors="coerce").mean()),
    }
    return TeamTdModel(
        league_id=league_id,
        feature_names=keep,
        coef_total=coef_t.tolist(),
        intercept_total=intercept_t,
        coef_pass=coef_p.tolist(),
        intercept_pass=intercept_p,
        coef_rush=coef_r.tolist(),
        intercept_rush=intercept_r,
        feature_mean=mean.tolist(),
        feature_std=std.tolist(),
        baseline_mean=baseline,
        train_end=train_end,
        notes=[
            "poisson_glm_irls",
            "offensive_scrimmage_td_only",
            "pass_rush_constrained_to_total",
            "research_only",
            "not_promoted",
            "validated_play_disabled",
        ],
        aux_means=aux,
    )


def predict_team_row(model: TeamTdModel, row: pd.Series | dict[str, Any]) -> dict[str, Any]:
    frame = pd.DataFrame([dict(row)])
    mean = np.asarray(model.feature_mean, dtype=float)
    std = np.asarray(model.feature_std, dtype=float)
    x = _design(frame, model.feature_names, mean, std)
    lam_total_raw = float(predict_mean(model.intercept_total, np.asarray(model.coef_total), x)[0])
    lam_pass_raw = float(predict_mean(model.intercept_pass, np.asarray(model.coef_pass), x)[0])
    lam_rush_raw = float(predict_mean(model.intercept_rush, np.asarray(model.coef_rush), x)[0])
    # Preserve total: rescale pass/rush so they sum to total mean.
    split = lam_pass_raw + lam_rush_raw
    if split <= 1e-8:
        share = float(frame["prior_pass_share"].iloc[0]) if "prior_pass_share" in frame.columns else 0.6
        lam_pass = lam_total_raw * share
        lam_rush = lam_total_raw * (1.0 - share)
    else:
        lam_pass = lam_total_raw * (lam_pass_raw / split)
        lam_rush = lam_total_raw * (lam_rush_raw / split)
    lam_total = lam_pass + lam_rush

    probs = poisson_pmf_buckets(np.array([lam_total]))[0]
    pass_probs = poisson_pmf_buckets(np.array([lam_pass]))[0]
    rush_probs = poisson_pmf_buckets(np.array([lam_rush]))[0]

    # Opportunity means: shrink feature priors toward training aux.
    aux = model.aux_means
    exp_poss = float(frame["exp_possessions"].iloc[0]) if "exp_possessions" in frame.columns else aux.get("possessions", 11.5)
    prior_plays = float(frame["prior_off_plays"].iloc[0]) if "prior_off_plays" in frame.columns else aux.get("off_plays", 65.0)
    prior_rz = float(frame["prior_rz_plays"].iloc[0]) if "prior_rz_plays" in frame.columns else aux.get("rz_plays", 8.0)
    prior_scoring = float(frame["prior_scoring_drives"].iloc[0]) if "prior_scoring_drives" in frame.columns else aux.get("scoring_drives", 3.5)

    return {
        "lambda_off_td": lam_total,
        "lambda_pass_td": lam_pass,
        "lambda_rush_td": lam_rush,
        "p_off_td": {str(b) if b < 5 else "5+": float(probs[i]) for i, b in enumerate(BUCKETS)},
        "p_pass_td": {str(b) if b < 5 else "5+": float(pass_probs[i]) for i, b in enumerate(BUCKETS)},
        "p_rush_td": {str(b) if b < 5 else "5+": float(rush_probs[i]) for i, b in enumerate(BUCKETS)},
        "exp_possessions": exp_poss,
        "exp_off_plays": prior_plays,
        "exp_rz_plays": prior_rz,
        "exp_scoring_drives": prior_scoring,
        "def_st_td_note": "modeled separately; not included in offensive TD totals",
    }


def predict_frame(model: TeamTdModel, frame: pd.DataFrame) -> pd.DataFrame:
    mean = np.asarray(model.feature_mean, dtype=float)
    std = np.asarray(model.feature_std, dtype=float)
    x = _design(frame, model.feature_names, mean, std)
    lam_t = predict_mean(model.intercept_total, np.asarray(model.coef_total), x)
    lam_p = predict_mean(model.intercept_pass, np.asarray(model.coef_pass), x)
    lam_r = predict_mean(model.intercept_rush, np.asarray(model.coef_rush), x)
    split = lam_p + lam_r
    share = np.where(split > 1e-8, lam_p / split, 0.6)
    lam_pass = lam_t * share
    lam_rush = lam_t * (1.0 - share)
    out = frame.copy()
    out["lambda_off_td"] = lam_t
    out["lambda_pass_td"] = lam_pass
    out["lambda_rush_td"] = lam_rush
    probs = poisson_pmf_buckets(lam_t)
    for i, b in enumerate(BUCKETS):
        key = f"p_{b}" if b < 5 else "p_5plus"
        out[key] = probs[:, i]
    return out


def evaluate_predictions(frame: pd.DataFrame, model: TeamTdModel) -> dict[str, Any]:
    pred = predict_frame(model, frame)
    y = pd.to_numeric(frame["off_td"], errors="coerce").fillna(0).to_numpy(dtype=float)
    lam = pred["lambda_off_td"].to_numpy(dtype=float)
    probs = np.column_stack([pred[f"p_{b}"] if b < 5 else pred["p_5plus"] for b in BUCKETS])
    baseline_lam = np.full_like(y, model.baseline_mean)
    baseline_probs = poisson_pmf_buckets(baseline_lam)
    # Historical prior baseline (team's own prior_off_td feature).
    prior_lam = pd.to_numeric(frame["prior_off_td"], errors="coerce").fillna(model.baseline_mean).to_numpy(dtype=float)
    prior_probs = poisson_pmf_buckets(prior_lam)

    env = _by_scoring_environment(frame, y, lam, probs)
    return {
        "n": int(len(frame)),
        "model": {
            "count": count_accuracy(y, lam),
            "log_loss": multinomial_log_loss(y, probs),
            "calibration": calibration_by_bucket(y, probs),
            "mean_lambda": float(np.mean(lam)),
            "mean_actual": float(np.mean(y)),
        },
        "baseline_league_mean": {
            "count": count_accuracy(y, baseline_lam),
            "log_loss": multinomial_log_loss(y, baseline_probs),
            "calibration": calibration_by_bucket(y, baseline_probs),
        },
        "baseline_team_prior": {
            "count": count_accuracy(y, prior_lam),
            "log_loss": multinomial_log_loss(y, prior_probs),
            "calibration": calibration_by_bucket(y, prior_probs),
        },
        "by_scoring_environment": env,
        "pass_rush": {
            "pass_mae": float(
                np.mean(
                    np.abs(
                        pred["lambda_pass_td"].to_numpy()
                        - pd.to_numeric(frame["pass_td"], errors="coerce").fillna(0).to_numpy()
                    )
                )
            ),
            "rush_mae": float(
                np.mean(
                    np.abs(
                        pred["lambda_rush_td"].to_numpy()
                        - pd.to_numeric(frame["rush_td"], errors="coerce").fillna(0).to_numpy()
                    )
                )
            ),
            "pass_mean_pred": float(np.mean(pred["lambda_pass_td"])),
            "rush_mean_pred": float(np.mean(pred["lambda_rush_td"])),
            "pass_mean_actual": float(pd.to_numeric(frame["pass_td"], errors="coerce").mean()),
            "rush_mean_actual": float(pd.to_numeric(frame["rush_td"], errors="coerce").mean()),
        },
    }


def _by_scoring_environment(
    frame: pd.DataFrame,
    y: np.ndarray,
    lam: np.ndarray,
    probs: np.ndarray,
) -> dict[str, Any]:
    """Split by predicted scoring environment (low/mid/high expected TDs)."""
    out: dict[str, Any] = {}
    if len(lam) < 30:
        return out
    q1, q2 = np.quantile(lam, [0.33, 0.66])
    labels = np.where(lam <= q1, "low", np.where(lam <= q2, "mid", "high"))
    for name in ("low", "mid", "high"):
        mask = labels == name
        if int(mask.sum()) < 10:
            continue
        out[name] = {
            "n": int(mask.sum()),
            "count": count_accuracy(y[mask], lam[mask]),
            "log_loss": multinomial_log_loss(y[mask], probs[mask]),
            "mean_lambda": float(np.mean(lam[mask])),
            "mean_actual": float(np.mean(y[mask])),
        }
    return out
