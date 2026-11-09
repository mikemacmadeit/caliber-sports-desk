"""Frozen M4 research artifact for prospective capture.

Features and distributions come from m4_comparison.json.
Betas are fit once on games before the freeze cutoff and never auto-promoted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.config import settings
from spe.models.advanced import (
    apply_beta,
    binary_probability,
    fit_log_beta,
    load_model_frame,
    market_spec,
    opponent_adjustment,
    over_probability,
    poisson_over_array,
    season_mask,
    shrinkage_mean,
)
from spe.models.metrics import apply_platt, platt_calibrate

FREEZE_YEAR = 2025  # betas use games before 1 Aug 2025; examined seasons stay out of beta fit


def m4_path(league_id: str) -> Path:
    return Path(settings.parquet_dir) / "models" / f"{league_id}_m4.json"


def load_m4_artifacts(league_id: str) -> dict[str, Any]:
    path = m4_path(league_id)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def export_m4_artifacts(session: Session, league_id: str) -> dict[str, Any]:
    """Write a frozen research artifact. Does not touch *_m3.json or forecasts."""
    comparison = Path(settings.parquet_dir) / "models" / "m4_comparison.json"
    if not comparison.exists():
        raise FileNotFoundError("m4_comparison.json is required before freezing M4")
    payload = json.loads(comparison.read_text(encoding="utf-8"))
    league = next((row for row in payload.get("leagues", []) if row.get("league_id") == league_id), None)
    if league is None:
        raise ValueError(f"No M4 comparison for {league_id}")
    frame = load_model_frame(session, league_id, year_max=FREEZE_YEAR - 1, universe="involved")
    train = frame[season_mask(frame, FREEZE_YEAR, train=True)].copy()
    markets: dict[str, Any] = {}
    for market_id, body in (league.get("markets") or {}).items():
        features = list(body.get("features") or [])
        family = str(body.get("distribution") or "poisson")
        fitted = _fit_frozen_market(train, league_id, market_id, features, family)
        if fitted is None:
            continue
        markets[market_id] = fitted
    artifact = {
        "league_id": league_id,
        "frozen_at": datetime.now(UTC).isoformat(),
        "feature_selection_source": "m4_comparison.json",
        "beta_train_end": f"{FREEZE_YEAR}-08-01",
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "markets": markets,
    }
    path = m4_path(league_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, indent=2, default=str), encoding="utf-8")
    return {"league_id": league_id, "path": str(path), "markets": list(markets), "play_enabled": False}


def _fit_frozen_market(
    train: pd.DataFrame,
    league_id: str,
    market_id: str,
    features: list[str],
    family: str,
) -> dict[str, Any] | None:
    from spe.models.advanced import _prepare_matrix, _z_apply, _z_fit

    spec = market_spec(market_id)
    count_col = "receptions" if league_id == "fbs" and market_id == "opportunity" else spec["count"]
    rows = train[train["position_group"].isin(spec["positions"])].copy()
    if len(rows) < 200:
        return None
    train_z = rows.copy()
    if "opp_def" in train_z.columns:
        mu, sd = _z_fit(train_z["opp_def"])
        train_z["opp_def_z"] = _z_apply(train_z["opp_def"], mu, sd)
    else:
        train_z["opp_def_z"] = 0.0
    base_tr, _ = shrinkage_mean(train_z, train_z, count_col, league_id)
    m3_tr, _, opp_beta = opponent_adjustment(train_z, train_z, base_tr, base_tr, count_col)
    x_tr, _, used = _prepare_matrix(train_z, train_z, features)
    y = train_z[count_col].fillna(0).to_numpy(dtype=float)
    new = m3_tr.copy()
    betas: dict[str, float] = {}
    feature_mu: dict[str, float] = {}
    feature_sd: dict[str, float] = {}
    for i, name in enumerate(used):
        mu, sd = _z_fit(train_z[name])
        feature_mu[name] = mu
        feature_sd[name] = sd
        beta = fit_log_beta(new, x_tr[:, i], y)
        betas[name] = beta
        if beta != 0.0:
            new = apply_beta(new, x_tr[:, i], beta)
    line = float(spec["line"])
    if spec["binary"]:
        raw = binary_probability(new)
        a, b = platt_calibrate(raw, (y > 0).astype(float))
        sigma = None
    elif family == "lognormal":
        raw = over_probability(new, y, line, yards=True, family="lognormal")
        a, b = platt_calibrate(raw, (y > line).astype(float))
        resid = np.log(np.clip(y, 0, None) + 1.0) - np.log(np.clip(new, 1e-6, None) + 1.0)
        sigma = float(max(np.nanstd(resid), 0.15))
    elif spec["yards"]:
        raw = over_probability(new, y, line, yards=True, family="gaussian")
        a, b = platt_calibrate(raw, (y > line).astype(float))
        sigma = float(max(np.nanstd(y - new), 1.0))
    else:
        raw = poisson_over_array(new, line)
        a, b = platt_calibrate(raw, (y > line).astype(float))
        sigma = None
    return {
        "features": used,
        "betas": betas,
        "feature_mu": feature_mu,
        "feature_sd": feature_sd,
        "opp_beta": opp_beta,
        "distribution": family if spec["yards"] else ("poisson" if not spec["binary"] else "anytime_td"),
        "platt_a": float(a),
        "platt_b": float(b),
        "sigma": sigma,
        "line_default": line,
        "n_train": int(len(train_z)),
    }


def adjust_mean(mean: float, features: dict[str, float | None], market: dict[str, Any]) -> tuple[float, list[str]]:
    """Apply frozen log-betas. Missing features stay missing and are reported."""
    value = float(mean)
    missing: list[str] = []
    for name, beta in (market.get("betas") or {}).items():
        raw = features.get(name)
        if raw is None or not np.isfinite(raw):
            missing.append(name)
            continue
        mu = float((market.get("feature_mu") or {}).get(name, 0.0))
        sd = float((market.get("feature_sd") or {}).get(name, 1.0)) or 1.0
        z = (float(raw) - mu) / sd
        value = float(apply_beta(np.array([value]), np.array([z]), float(beta))[0])
    return value, missing


def probability_from_mean(mean: float, market: dict[str, Any], *, line: float | None, side: str) -> float:
    family = str(market.get("distribution") or "poisson")
    used = float(market.get("line_default") or 0.5) if line is None else float(line)
    arr = np.array([mean], dtype=float)
    if family == "anytime_td":
        raw = binary_probability(arr)
    elif family == "lognormal":
        sigma = float(market.get("sigma") or 0.4)
        from spe.models.advanced import _lognormal_sf

        raw = _lognormal_sf(used, arr, sigma)
    elif family == "gaussian":
        sigma = float(market.get("sigma") or 1.0)
        from spe.models.advanced import _norm_sf

        raw = _norm_sf(used, arr, sigma)
    else:
        raw = poisson_over_array(arr, used)
    p = float(apply_platt(raw, float(market["platt_a"]), float(market["platt_b"]))[0])
    if side in {"under", "no"}:
        p = 1.0 - p
    return float(np.clip(p, 1e-6, 1.0 - 1e-6))
