"""Rolling-origin checks on the corrected player universe.

2022 through 2025 are examined seasons. They are not a promotion window.
2026 finals are the prospective shadow period. Nothing here is promoted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.config import settings
from spe.models.advanced import (
    _prepare_matrix,
    _zscore_defense,
    apply_beta,
    binary_probability,
    fit_log_beta,
    market_spec,
    opponent_adjustment,
    over_probability,
    score_block,
    season_mask,
    shrinkage_mean,
)
from spe.models.metrics import apply_platt, platt_calibrate

EXAMINED_YEARS = (2023, 2024, 2025)
SHADOW_YEARS = (2026,)
POSITION_TASKS = (
    ("rushing_yards", None, "shared"),
    ("rushing_yards", frozenset({"QB"}), "QB"),
    ("rushing_yards", frozenset({"RB"}), "RB"),
    ("receiving_yards", None, "shared"),
    ("receiving_yards", frozenset({"WR"}), "WR"),
    ("receiving_yards", frozenset({"TE"}), "TE"),
    ("anytime_td", None, "shared"),
    ("anytime_td", frozenset({"QB"}), "QB"),
    ("anytime_td", frozenset({"RB"}), "RB"),
    ("anytime_td", frozenset({"WR"}), "WR"),
    ("anytime_td", frozenset({"TE"}), "TE"),
)


def market_comparison_status(settled_outcomes: int, stored_rows: int) -> dict[str, Any]:
    """Research lines stay out of the market table. An empty table means no settled licensed props."""
    return {
        "settled_sportsbook_outcomes": int(settled_outcomes),
        "stored_market_comparisons": int(stored_rows),
        "market_comparison": "unavailable" if settled_outcomes == 0 else "available",
        "research_lines_are_market_comparisons": False,
        "note": "Declared 4.5, 49.5, and 39.5 lines are research lines. They are not sportsbook prices.",
    }


def _split(frame: pd.DataFrame, year: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    train = frame[season_mask(frame, year - 1, train=True)]
    calibrate = frame[season_mask(frame, year - 1, train=False)]
    test = frame[season_mask(frame, year, train=False)]
    return train, calibrate, test


def _apply_features(train: pd.DataFrame, other: pd.DataFrame, train_mean: np.ndarray, other_mean: np.ndarray, extra: list[str], y_train: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    x_train, x_other, used = _prepare_matrix(train, other, extra)
    new_train, new_other = train_mean.copy(), other_mean.copy()
    for i, _name in enumerate(used):
        beta = fit_log_beta(new_train, x_train[:, i], y_train)
        if beta == 0.0:
            continue
        new_train = apply_beta(new_train, x_train[:, i], beta)
        new_other = apply_beta(new_other, x_other[:, i], beta)
    return new_train, new_other


def score_window(
    frame: pd.DataFrame,
    league_id: str,
    market_id: str,
    year: int,
    extra: list[str],
    family: str,
    positions: frozenset[str] | None = None,
) -> dict[str, Any] | None:
    """Means use seasons before the calibration year. Platt uses the calibration year only."""
    spec = market_spec(market_id)
    allowed = set(positions) if positions is not None else set(spec["positions"])
    count_col = "receptions" if league_id == "fbs" and market_id == "opportunity" else spec["count"]
    train, calibrate, test = _split(frame, year)
    train = train[train["position_group"].isin(allowed)]
    calibrate = calibrate[calibrate["position_group"].isin(allowed)]
    test = test[test["position_group"].isin(allowed)]
    if len(train) < 200 or len(calibrate) < 50 or len(test) < 30:
        return None
    y_train = train[count_col].fillna(0).to_numpy(dtype=float)
    y_cal = calibrate[count_col].fillna(0).to_numpy(dtype=float)
    y_test = test[count_col].fillna(0).to_numpy(dtype=float)
    train_z, calibrate = _zscore_defense(train, calibrate)
    _, test = _zscore_defense(train, test)
    base_tr, base_cal = shrinkage_mean(train_z, calibrate, count_col, league_id)
    _, base_te = shrinkage_mean(train_z, test, count_col, league_id)
    m3_tr, m3_cal, beta = opponent_adjustment(train_z, calibrate, base_tr, base_cal, count_col)
    z_te = test["opp_def_z"].to_numpy(dtype=float)
    m3_te = apply_beta(base_te, np.where(np.isfinite(z_te), z_te, 0.0), beta)
    new_tr, new_cal = _apply_features(train_z, calibrate, m3_tr, m3_cal, extra, y_train)
    _, new_te = _apply_features(train_z, test, m3_tr, m3_te, extra, y_train)
    line = float(spec["line"])
    raw_te = _raw_probability(new_tr, new_te, y_train, spec, family, line)
    raw_cal = _raw_probability(new_tr, new_cal, y_train, spec, family, line)
    p_model = apply_platt(raw_te, *platt_calibrate(raw_cal, _event(y_cal, spec, line)))
    return {
        "season": year,
        "role": "prospective_shadow" if year in SHADOW_YEARS else "examined",
        "n_train": int(len(train)),
        "n_calibrate": int(len(calibrate)),
        "n_test": int(len(test)),
        "metrics": score_block(y_test, new_te, p_model, binary=spec["binary"], line=line),
    }


def _event(y: np.ndarray, spec: dict[str, Any], line: float) -> np.ndarray:
    if spec["binary"]:
        return (y > 0).astype(float)
    return (y > line).astype(float)


def _raw_probability(train_mean: np.ndarray, other_mean: np.ndarray, y_train: np.ndarray, spec: dict[str, Any], family: str, line: float) -> np.ndarray:
    if spec["binary"]:
        return binary_probability(other_mean)
    chosen = family if spec["yards"] else "poisson"
    return over_probability(np.concatenate([train_mean, other_mean]), y_train, line, yards=spec["yards"], family=chosen)[len(train_mean) :]


def sportsbook_audit(session: Session) -> dict[str, Any]:
    settled = session.execute(
        text(
            """
            SELECT count(DISTINCT o.outcome_id)
            FROM odds_ticks o
            JOIN outcomes oc ON oc.outcome_id = o.outcome_id
            JOIN games g ON g.game_id = oc.game_id
            WHERE g.status = 'final'
            """
        )
    ).scalar()
    stored = session.execute(text("SELECT count(*) FROM market_comparisons")).scalar()
    return market_comparison_status(int(settled or 0), int(stored or 0))


def run_validation_study(session: Session) -> dict[str, Any]:
    """Score frozen research choices. Does not write forecasts, odds, or M3 artifacts."""
    from spe.models.advanced import load_model_frame

    m3_paths = [Path(settings.parquet_dir) / "models" / f"{league}_m3.json" for league in ("nfl", "fbs")]
    before = {path: path.read_bytes() if path.exists() else None for path in m3_paths}
    forecast_before = session.execute(text("SELECT count(*) FROM forecasts")).scalar()
    odds_before = session.execute(text("SELECT count(*) FROM odds_ticks")).scalar()
    frozen = _frozen_features()
    leagues = []
    for league_id in ("nfl", "fbs"):
        involved = load_model_frame(session, league_id, year_max=2026, universe="involved")
        corrected = load_model_frame(session, league_id, year_max=2026, universe="corrected")
        leagues.append(
            {
                "league_id": league_id,
                "eligibility": corrected.attrs.get("eligibility", {}),
                "involved_rows": int(len(involved)),
                "corrected_rows": int(len(corrected)),
                "zero_impact": _zero_impact(involved, corrected, league_id),
                "position_specific": _position_results(corrected, league_id),
                "m3_vs_m4": _frozen_comparison(corrected, league_id, frozen.get(league_id, {})),
            }
        )
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "play_enabled": False,
        "promoted": False,
        "promotion_policy": (
            "One league-market can be promoted on its own. Promotion requires a prospective season "
            "that was not used to choose features, distributions, or position splits. 2023-2025 are examined. "
            "No model is promoted from this study."
        ),
        "windows": {
            "train": "games before 1 August of the calibration season",
            "calibration": "the season before the test season, Platt only",
            "examined": list(EXAMINED_YEARS),
            "prospective_shadow": list(SHADOW_YEARS),
        },
        "sportsbook": sportsbook_audit(session),
        "leagues": leagues,
    }
    path = Path(settings.parquet_dir) / "models" / "validation_study.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    for path_m3, payload in before.items():
        after = path_m3.read_bytes() if path_m3.exists() else None
        if after != payload:
            raise RuntimeError(f"M3 artifact changed: {path_m3}")
    if session.execute(text("SELECT count(*) FROM forecasts")).scalar() != forecast_before:
        raise RuntimeError("forecast rows changed")
    if session.execute(text("SELECT count(*) FROM odds_ticks")).scalar() != odds_before:
        raise RuntimeError("odds ticks changed")
    return report


def _frozen_features() -> dict[str, dict[str, Any]]:
    path = Path(settings.parquet_dir) / "models" / "m4_comparison.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    found: dict[str, dict[str, Any]] = {}
    for league in payload.get("leagues", []):
        found[league["league_id"]] = {
            market: {"features": body.get("features", []), "distribution": body.get("distribution", "poisson")}
            for market, body in league.get("markets", {}).items()
        }
    return found


def _zero_impact(involved: pd.DataFrame, corrected: pd.DataFrame, league_id: str) -> list[dict[str, Any]]:
    rows = []
    for market_id in ("anytime_td", "receptions", "receiving_yards", "rushing_yards"):
        family = "gaussian" if market_spec(market_id)["yards"] else "poisson"
        for year in (2025, 2026):
            current = score_window(involved, league_id, market_id, year, [], family)
            full = score_window(corrected, league_id, market_id, year, [], family)
            rows.append({"market": market_id, "season": year, "involved_only": current, "corrected": full})
    return rows


def _fit_means(
    train: pd.DataFrame,
    other: pd.DataFrame,
    league_id: str,
    count_col: str,
) -> tuple[np.ndarray, np.ndarray]:
    train_z, other_z = _zscore_defense(train, other)
    base_tr, base_other = shrinkage_mean(train_z, other_z, count_col, league_id)
    m3_tr, m3_other, _beta = opponent_adjustment(train_z, other_z, base_tr, base_other, count_col)
    return m3_tr, m3_other


def score_position_fair(
    frame: pd.DataFrame,
    league_id: str,
    market_id: str,
    year: int,
    position: str,
) -> dict[str, Any] | None:
    """Score a shared model and a position-only model on the same position test rows."""
    spec = market_spec(market_id)
    if position not in spec["positions"]:
        return None
    count_col = "receptions" if league_id == "fbs" and market_id == "opportunity" else spec["count"]
    family = "gaussian" if spec["yards"] else "poisson"
    train, calibrate, test = _split(frame, year)
    shared_train = train[train["position_group"].isin(spec["positions"])]
    shared_cal = calibrate[calibrate["position_group"].isin(spec["positions"])]
    pos_train = train[train["position_group"] == position]
    pos_cal = calibrate[calibrate["position_group"] == position]
    pos_test = test[test["position_group"] == position]
    if len(shared_train) < 200 or len(pos_train) < 80 or len(pos_cal) < 20 or len(pos_test) < 20:
        return None
    y_shared = shared_train[count_col].fillna(0).to_numpy(dtype=float)
    y_pos = pos_train[count_col].fillna(0).to_numpy(dtype=float)
    y_cal = pos_cal[count_col].fillna(0).to_numpy(dtype=float)
    y_test = pos_test[count_col].fillna(0).to_numpy(dtype=float)
    shared_cal_pos = shared_cal[shared_cal["position_group"] == position]
    if len(shared_cal_pos) < 20:
        return None
    shared_train_mean, shared_cal_pos_mean = _fit_means(shared_train, shared_cal_pos, league_id, count_col)
    _, shared_test_mean = _fit_means(shared_train, pos_test, league_id, count_col)
    y_shared_cal = shared_cal_pos[count_col].fillna(0).to_numpy(dtype=float)
    pos_tr_mean, pos_cal_mean = _fit_means(pos_train, pos_cal, league_id, count_col)
    _, pos_test_mean = _fit_means(pos_train, pos_test, league_id, count_col)
    line = float(spec["line"])
    shared_raw_cal = _raw_probability(shared_train_mean, shared_cal_pos_mean, y_shared, spec, family, line)
    shared_raw_te = _raw_probability(shared_train_mean, shared_test_mean, y_shared, spec, family, line)
    shared_p = apply_platt(shared_raw_te, *platt_calibrate(shared_raw_cal, _event(y_shared_cal, spec, line)))
    pos_raw_cal = _raw_probability(pos_tr_mean, pos_cal_mean, y_pos, spec, family, line)
    pos_raw_te = _raw_probability(pos_tr_mean, pos_test_mean, y_pos, spec, family, line)
    pos_p = apply_platt(pos_raw_te, *platt_calibrate(pos_raw_cal, _event(y_cal, spec, line)))
    return {
        "market": market_id,
        "position": position,
        "season": year,
        "role": "prospective_shadow" if year in SHADOW_YEARS else "examined",
        "n_test": int(len(pos_test)),
        "shared": score_block(y_test, shared_test_mean, shared_p, binary=spec["binary"], line=line),
        "position_specific": score_block(y_test, pos_test_mean, pos_p, binary=spec["binary"], line=line),
    }


def _position_results(frame: pd.DataFrame, league_id: str) -> list[dict[str, Any]]:
    rows = []
    for market_id, positions, label in POSITION_TASKS:
        family = "gaussian" if market_spec(market_id)["yards"] else "poisson"
        for year in (*EXAMINED_YEARS, *SHADOW_YEARS):
            scored = score_window(frame, league_id, market_id, year, [], family, positions)
            if scored is None:
                continue
            rows.append({"market": market_id, "position": label, **scored})
            if label != "shared":
                fair = score_position_fair(frame, league_id, market_id, year, label)
                if fair is not None:
                    rows[-1]["fair_same_rows"] = {
                        "shared": fair["shared"],
                        "position_specific": fair["position_specific"],
                    }
    return rows


def _frozen_comparison(frame: pd.DataFrame, league_id: str, frozen: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for market_id, spec in frozen.items():
        if market_id not in {"opportunity", "anytime_td", "receptions", "receiving_yards", "rushing_yards"}:
            continue
        features = list(spec.get("features") or [])
        family = str(spec.get("distribution") or "poisson")
        m3_family = "gaussian" if market_spec(market_id)["yards"] else "poisson"
        for year in (2025, 2026):
            baseline = score_window(frame, league_id, market_id, year, [], m3_family)
            candidate = score_window(frame, league_id, market_id, year, features, family)
            rows.append(
                {
                    "market": market_id,
                    "season": year,
                    "role": "prospective_shadow" if year in SHADOW_YEARS else "examined",
                    "features": features,
                    "m3": None if baseline is None else baseline["metrics"],
                    "m4": None if candidate is None else candidate["metrics"],
                }
            )
    return rows
