from __future__ import annotations

import math

import numpy as np


def brier_score(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def log_loss(y: np.ndarray, p: np.ndarray, eps: float = 1e-15) -> float:
    p = np.clip(p, eps, 1 - eps)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def expected_calibration_error(y: np.ndarray, p: np.ndarray, n_bins: int = 10) -> float:
    bins = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    n = len(y)
    for i in range(n_bins):
        mask = (p >= bins[i]) & (p < bins[i + 1] if i < n_bins - 1 else p <= bins[i + 1])
        if not np.any(mask):
            continue
        ece += (np.sum(mask) / n) * abs(float(np.mean(y[mask]) - np.mean(p[mask])))
    return float(ece)


def mean_absolute_error(y: np.ndarray, yhat: np.ndarray) -> float:
    return float(np.mean(np.abs(y - yhat)))


def platt_calibrate(p: np.ndarray, y: np.ndarray, *, max_iter: int = 40) -> tuple[float, float]:
    """Fit p' = sigmoid(a + b * logit(p)) on train only. Returns (a, b)."""
    eps = 1e-6
    p = np.clip(np.asarray(p, dtype=float), eps, 1 - eps)
    y = np.asarray(y, dtype=float)
    z = np.log(p / (1 - p))
    a, b = 0.0, 1.0
    for _ in range(max_iter):
        s = 1.0 / (1.0 + np.exp(-(a + b * z)))
        s = np.clip(s, eps, 1 - eps)
        w = s * (1 - s)
        # Newton step on [a, b]
        xa = np.ones_like(z)
        xb = z
        g0 = float(np.sum(s - y))
        g1 = float(np.sum((s - y) * z))
        h00 = float(np.sum(w * xa * xa)) + 1e-6
        h01 = float(np.sum(w * xa * xb))
        h11 = float(np.sum(w * xb * xb)) + 1e-6
        det = h00 * h11 - h01 * h01
        if abs(det) < 1e-12:
            break
        da = (h11 * g0 - h01 * g1) / det
        db = (-h01 * g0 + h00 * g1) / det
        a -= da
        b -= db
        if abs(da) + abs(db) < 1e-6:
            break
    return float(a), float(b)


def apply_platt(p: np.ndarray, a: float, b: float) -> np.ndarray:
    eps = 1e-6
    p = np.clip(np.asarray(p, dtype=float), eps, 1 - eps)
    z = np.log(p / (1 - p))
    cal = 1.0 / (1.0 + np.exp(-(a + b * z)))
    return np.clip(cal, eps, 1 - eps)


def crps_gaussian(y: float, mu: float, sigma: float) -> float:
    """Closed-form CRPS for N(mu, sigma^2). Used for yards quantiles."""
    if sigma <= 0:
        return abs(y - mu)
    z = (y - mu) / sigma
    pdf = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    cdf = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    return float(sigma * (z * (2 * cdf - 1) + 2 * pdf - 1 / math.sqrt(math.pi)))
