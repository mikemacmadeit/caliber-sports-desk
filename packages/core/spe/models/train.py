"""Separate NFL and FBS production models. Chronological train/test. No future features."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.analytics.definitions import EPA_PPA_NOTE, SHRINKAGE_K
from spe.config import settings
from spe.db.base import new_id
from spe.db.models import BacktestRun, ModelHealthSnapshot, ModelVersion
from spe.models.dataset import DEFAULT_LINES, assert_prior_available, build_model_frame, chronological_split, zscore_within_train
from spe.models.hierarchical import HierarchicalCountModel, PlayerObservation
from spe.models.metrics import (
    apply_platt,
    brier_score,
    crps_gaussian,
    expected_calibration_error,
    log_loss,
    mean_absolute_error,
    platt_calibrate,
)

MODEL_FAMILIES = ("opportunity", "anytime_td", "receptions", "receiving_yards", "rushing_yards")
SKILL = {"WR", "TE", "RB", "QB"}


@dataclass
class FittedMarket:
    league_id: str
    market_id: str
    family: str
    train_end: str
    epa_scale: str
    league_rate: float
    team_rates: dict[str, float]
    pos_rates: dict[str, float]
    player_rates: dict[str, float]
    player_n: dict[str, int]
    k_player: float
    opp_beta: float
    z_mean: float
    z_std: float
    platt_a: float
    platt_b: float
    sigma: float
    default_line: float
    notes: list[str]


def train_league_models(
    session: Session,
    league_id: str,
    *,
    train_end: datetime,
    test_start: datetime,
    test_end: datetime,
    persist: bool = True,
) -> dict[str, Any]:
    if league_id not in {"nfl", "fbs"}:
        raise ValueError("Models are league-specific")
    frame = build_model_frame(session, league_id)
    if frame.empty:
        return {"status": "no_stats", "league_id": league_id, "epa_scale": "epa" if league_id == "nfl" else "ppa"}
    assert_prior_available(frame)
    train, test = chronological_split(frame, train_end=train_end, test_start=test_start, test_end=test_end)
    if train.empty or test.empty:
        return {
            "status": "no_split",
            "league_id": league_id,
            "train_n": int(len(train)),
            "test_n": int(len(test)),
        }
    opp_train = train["opp_def_epa"] if "opp_def_epa" in train.columns else pd.Series(dtype=float)
    opp_test = test["opp_def_epa"] if "opp_def_epa" in test.columns else pd.Series(dtype=float)
    z_train, z_test, z_meta = zscore_within_train(opp_train.fillna(np.nanmean(opp_train) if len(opp_train) else 0.0), opp_test.fillna(np.nanmean(opp_train) if len(opp_train) else 0.0))
    train = train.copy()
    test = test.copy()
    train["opp_def_z"] = z_train
    test["opp_def_z"] = z_test

    results: dict[str, Any] = {
        "status": "ok",
        "league_id": league_id,
        "epa_scale": "epa" if league_id == "nfl" else "ppa",
        "epa_ppa_note": EPA_PPA_NOTE,
        "train_n": int(len(train)),
        "test_n": int(len(test)),
        "train_end": train_end.isoformat(),
        "markets": {},
        "play_enabled": False,
        "play_gate": "out_of_sample_not_promoted",
    }
    artifacts: dict[str, FittedMarket] = {}
    for market_id, spec in _market_specs(league_id).items():
        fitted, metrics = _fit_market(train, test, league_id, market_id, spec, z_meta, train_end)
        artifacts[market_id] = fitted
        results["markets"][market_id] = metrics
        if persist:
            _persist_eval(session, league_id, market_id, fitted, metrics, train_end, test_start, test_end)
    if persist:
        _write_artifacts(league_id, artifacts, train_end)
        session.flush()
    results["artifact_path"] = str(_artifact_path(league_id))
    return results


def load_artifacts(league_id: str) -> dict[str, FittedMarket]:
    path = _artifact_path(league_id)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, FittedMarket] = {}
    for key, blob in raw.get("markets", {}).items():
        out[key] = FittedMarket(**blob)
    return out


def predict_row(
    fitted: FittedMarket,
    player_id: str,
    team_id: str,
    position_group: str,
    opp_def_z: float,
    prior_games: int,
    line: float | None = None,
) -> dict[str, float]:
    """Probability from a saved league model.

    ``line`` is the posted sportsbook number. Counting and yards markets use it
    instead of the training default. Anytime touchdown stays P(at least one).
    """
    prior = fitted.pos_rates.get(position_group, fitted.league_rate)
    team_prior = fitted.team_rates.get(team_id, prior)
    n = int(fitted.player_n.get(player_id, prior_games) or 0)
    raw = fitted.player_rates.get(player_id, team_prior)
    shrink = n / (n + fitted.k_player) if n else 0.0
    rate = shrink * raw + (1 - shrink) * team_prior
    # Anytime TD with zero player games: full team_prior clones every depth-chart
    # name to the same %. Refuse that path — callers must skip or use research.
    if fitted.family in {"anytime_td"} and n <= 0:
        return {
            "mean": None,
            "p": None,
            "p_low": None,
            "p_high": None,
            "status": "rejected_no_player_history",
            "reason": "anytime_td_team_prior_clone_blocked",
        }
    mult = float(np.exp(np.clip(fitted.opp_beta * opp_def_z, -0.35, 0.35)))
    mean = max(1e-6, rate * mult)
    if fitted.family in {"anytime_td"}:
        p_raw = float(1.0 - np.exp(-mean))
        p = float(apply_platt(np.array([p_raw]), fitted.platt_a, fitted.platt_b)[0])
        return {"mean": mean, "p": p, "p_low": max(1e-6, p - 0.08), "p_high": min(1.0, p + 0.08)}
    used = fitted.default_line if line is None else float(line)
    if fitted.family in {"receptions", "opportunity"}:
        p = HierarchicalCountModel.poisson_over(mean, used)
        return {
            "mean": mean,
            "p": float(p),
            "line": used,
            "p_low": max(1e-6, p - 0.08),
            "p_high": min(1.0, p + 0.08),
        }
    p = float(1.0 - _norm_cdf(used, mean, fitted.sigma))
    return {
        "mean": mean,
        "p": p,
        "line": used,
        "sigma": fitted.sigma,
        "p_low": max(1e-6, p - 0.08),
        "p_high": min(1.0, p + 0.08),
    }


def is_reportable_health(notes: str | None) -> bool:
    """Real model-performance rows only. Seed and prototype snapshots stay stored."""
    text = notes or ""
    lowered = text.lower()
    if "prototype" in lowered or "seed" in lowered:
        return False
    return text.startswith("m3 ")


def _market_specs(league_id: str) -> dict[str, dict[str, Any]]:
    return {
        "opportunity": {"count": "targets", "exposure": "prior_games_unit", "family": "opportunity", "line": 4.5, "positions": {"WR", "TE", "RB"}},
        "anytime_td": {"count": "anytime_tds", "exposure": "prior_games_unit", "family": "anytime_td", "line": 0.5, "positions": SKILL, "binary": True},
        "receptions": {"count": "receptions", "exposure": "prior_games_unit", "family": "receptions", "line": DEFAULT_LINES["receptions"], "positions": {"WR", "TE", "RB"}},
        "receiving_yards": {"count": "rec_yards", "exposure": "prior_games_unit", "family": "receiving_yards", "line": DEFAULT_LINES["receiving_yards"], "positions": {"WR", "TE", "RB"}, "gaussian": True},
        "rushing_yards": {"count": "rush_yards", "exposure": "prior_games_unit", "family": "rushing_yards", "line": DEFAULT_LINES["rushing_yards"], "positions": {"RB", "QB"}, "gaussian": True},
    }


def _fit_market(
    train: pd.DataFrame,
    test: pd.DataFrame,
    league_id: str,
    market_id: str,
    spec: dict[str, Any],
    z_meta: dict[str, float],
    train_end: datetime,
) -> tuple[FittedMarket, dict[str, Any]]:
    positions = spec["positions"]
    tr = train[train["position_group"].isin(positions)].copy()
    te = test[test["position_group"].isin(positions)].copy()
    count_col = spec["count"]
    notes = [EPA_PPA_NOTE]
    if league_id == "fbs":
        notes.append("FBS targets are receptions (CFBD box does not publish targets). RZ/GL from PBP unavailable.")
        if count_col == "targets":
            notes.append("opportunity model uses receptions as the CFBD-supported usage proxy.")
            count_col = "receptions"
    if tr.empty or te.empty:
        dummy = FittedMarket(
            league_id=league_id,
            market_id=market_id,
            family=spec["family"],
            train_end=train_end.isoformat(),
            epa_scale="epa" if league_id == "nfl" else "ppa",
            league_rate=0.0,
            team_rates={},
            pos_rates={},
            player_rates={},
            player_n={},
            k_player=SHRINKAGE_K[league_id],
            opp_beta=0.0,
            z_mean=z_meta["mean"],
            z_std=z_meta["std"],
            platt_a=0.0,
            platt_b=1.0,
            sigma=1.0,
            default_line=float(spec["line"]),
            notes=notes + ["insufficient_rows"],
        )
        return dummy, {"status": "no_rows", "n": 0}

    grouped = tr.groupby(["player_id", "team_id", "position_group"], dropna=False)
    obs: list[PlayerObservation] = []
    for (pid, team_id, pos), g in grouped:
        n = max(int(len(g)), 1)
        obs.append(
            PlayerObservation(
                player_id=str(pid),
                team_id=str(team_id or ""),
                conference=None,
                position_group=str(pos),
                league_id=league_id,
                games=n,
                count_sum=float(g[count_col].fillna(0).sum()),
                exposure=float(n),
            )
        )
    model = HierarchicalCountModel(league_id, spec["family"])
    model.fit(obs)
    tr_pred = _predict_frame(tr, model, 0.0)
    te_pred = _predict_frame(te, model, 0.0)
    y_tr = tr[count_col].fillna(0).to_numpy(dtype=float)
    y_te = te[count_col].fillna(0).to_numpy(dtype=float)
    # opponent beta: log(y+eps) ~ a + b * z  using TRAIN only
    beta = _fit_opp_beta(tr_pred, tr["opp_def_z"].to_numpy(), y_tr)
    tr_mean = tr_pred * np.exp(np.clip(beta * tr["opp_def_z"].to_numpy(), -0.35, 0.35))
    te_mean = te_pred * np.exp(np.clip(beta * te["opp_def_z"].to_numpy(), -0.35, 0.35))
    sigma = float(np.nanstd(y_tr - tr_mean)) or 1.0
    sigma = max(sigma, 1.0)

    if spec.get("binary"):
        y_bin_tr = (y_tr > 0).astype(float)
        y_bin_te = (y_te > 0).astype(float)
        p_tr = np.clip(1.0 - np.exp(-tr_mean), 1e-6, 1 - 1e-6)
        p_te = np.clip(1.0 - np.exp(-te_mean), 1e-6, 1 - 1e-6)
        a, b = platt_calibrate(p_tr, y_bin_tr)
        p_tr_c = apply_platt(p_tr, a, b)
        p_te_c = apply_platt(p_te, a, b)
        p_base = _baseline_binary(tr, te)
        metrics = _binary_metrics(y_bin_te, p_te_c, p_base, notes)
        metrics["includes_zero_td_players"] = True
        metrics["td_rate"] = float(y_bin_te.mean())
    elif spec.get("gaussian"):
        a, b = 0.0, 1.0
        line = float(spec["line"])
        p_te = np.array([1.0 - _norm_cdf(line, m, sigma) for m in te_mean])
        y_over = (y_te > line).astype(float)
        p_base = np.full_like(p_te, float((y_tr > line).mean()) if len(y_tr) else 0.5)
        metrics = {
            "n": int(len(te)),
            "mae": mean_absolute_error(y_te, te_mean),
            "mae_baseline": mean_absolute_error(y_te, np.full_like(y_te, float(np.mean(y_tr)))),
            "crps": float(np.mean([crps_gaussian(y, m, sigma) for y, m in zip(y_te, te_mean)])),
            "brier_over": brier_score(y_over, p_te),
            "brier_over_baseline": brier_score(y_over, p_base),
            "ece_over": expected_calibration_error(y_over, p_te),
            "mean_actual": float(np.mean(y_te)),
            "mean_pred": float(np.mean(te_mean)),
            "notes": notes,
        }
        metrics["skill_mae"] = metrics["mae_baseline"] - metrics["mae"]
    else:
        a, b = 0.0, 1.0
        line = float(spec["line"])
        p_te = np.array([HierarchicalCountModel.poisson_over(m, line) for m in te_mean])
        y_over = (y_te > line).astype(float)
        p_base = np.full_like(p_te, float((y_tr > line).mean()) if len(y_tr) else 0.5)
        metrics = {
            "n": int(len(te)),
            "mae": mean_absolute_error(y_te, te_mean),
            "mae_baseline": mean_absolute_error(y_te, np.full_like(y_te, float(np.mean(y_tr)))),
            "brier_over": brier_score(y_over, p_te),
            "brier_over_baseline": brier_score(y_over, p_base),
            "ece_over": expected_calibration_error(y_over, p_te),
            "mean_actual": float(np.mean(y_te)),
            "mean_pred": float(np.mean(te_mean)),
            "notes": notes,
        }
        metrics["skill_mae"] = metrics["mae_baseline"] - metrics["mae"]

    fitted = FittedMarket(
        league_id=league_id,
        market_id=market_id,
        family=spec["family"],
        train_end=train_end.isoformat(),
        epa_scale="epa" if league_id == "nfl" else "ppa",
        league_rate=model.league_rate,
        team_rates=model.team_rates,
        pos_rates=model.pos_rates,
        player_rates=model.player_rates,
        player_n={k: int(v) for k, v in model.player_n.items()},
        k_player=model.k_player,
        opp_beta=float(beta),
        z_mean=z_meta["mean"],
        z_std=z_meta["std"],
        platt_a=float(a),
        platt_b=float(b),
        sigma=float(sigma),
        default_line=float(spec["line"]),
        notes=notes,
    )
    metrics["status"] = "ok"
    metrics["opp_beta"] = float(beta)
    metrics["epa_scale"] = fitted.epa_scale
    return fitted, metrics


def _predict_frame(df: pd.DataFrame, model: HierarchicalCountModel, beta: float) -> np.ndarray:
    out = []
    for rec in df.itertuples(index=False):
        pred = model.predict_rate(
            rec.player_id,
            str(getattr(rec, "team_id", "") or ""),
            None,
            str(rec.position_group),
            sample_games=int(getattr(rec, "prior_games", 0) or 0),
        )
        out.append(pred.mean)
    return np.array(out, dtype=float)


def _fit_opp_beta(mean: np.ndarray, z: np.ndarray, y: np.ndarray) -> float:
    mask = np.isfinite(mean) & np.isfinite(z) & np.isfinite(y) & (mean > 0)
    if int(mask.sum()) < 30:
        return 0.0
    resid = np.log(np.clip(y[mask], 0, None) + 0.1) - np.log(mean[mask] + 0.1)
    zc = z[mask]
    denom = float(np.dot(zc, zc))
    if denom < 1e-6:
        return 0.0
    return float(np.clip(np.dot(zc, resid) / denom, -0.5, 0.5))


def _baseline_binary(train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    pos_rate = train.groupby("position_group")["y_td"].mean().to_dict()
    league = float(train["y_td"].mean()) if len(train) else 0.15
    return np.array([float(pos_rate.get(p, league)) for p in test["position_group"]], dtype=float)


def _binary_metrics(y: np.ndarray, p: np.ndarray, p_base: np.ndarray, notes: list[str]) -> dict[str, Any]:
    return {
        "n": int(len(y)),
        "brier": brier_score(y, p),
        "brier_baseline": brier_score(y, p_base),
        "log_loss": log_loss(y, p),
        "log_loss_baseline": log_loss(y, p_base),
        "ece": expected_calibration_error(y, p),
        "ece_baseline": expected_calibration_error(y, p_base),
        "base_rate": float(y.mean()),
        "mean_p": float(p.mean()),
        "skill_brier": brier_score(y, p_base) - brier_score(y, p),
        "notes": notes,
    }


def _norm_cdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 0.0 if x < mu else 1.0
    z = (x - mu) / (sigma * np.sqrt(2))
    return float(0.5 * (1.0 + math.erf(z)))


def _persist_eval(
    session: Session,
    league_id: str,
    market_id: str,
    fitted: FittedMarket,
    metrics: dict[str, Any],
    train_end: datetime,
    test_start: datetime,
    test_end: datetime,
) -> None:
    vid = f"m3-{league_id}-{market_id}"
    if session.get(ModelVersion, vid) is None:
        session.add(
            ModelVersion(
                model_version_id=vid,
                code_sha="m3-hierarchical-v1",
                training_cutoff=train_end,
                data_snapshot="historical_pregame_complete",
                hyperparameters={
                    "k_player": fitted.k_player,
                    "opp_beta": fitted.opp_beta,
                    "epa_scale": fitted.epa_scale,
                    "platt": [fitted.platt_a, fitted.platt_b],
                },
                calibration_version="platt_v1" if market_id == "anytime_td" else "none",
                league_id=league_id,
                family=fitted.family,
            )
        )
        session.flush()
    session.add(
        BacktestRun(
            run_id=new_id(),
            league_id=league_id,
            market_id=market_id,
            model_family=f"m3_{fitted.family}",
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            n=int(metrics.get("n") or 0),
            brier=metrics.get("brier") or metrics.get("brier_over"),
            log_loss=metrics.get("log_loss"),
            ece=metrics.get("ece") or metrics.get("ece_over"),
            coverage=1.0,
            notes=json.dumps({k: v for k, v in metrics.items() if k != "notes"}, default=str)[:4000],
        )
    )
    session.add(
        ModelHealthSnapshot(
            league_id=league_id,
            market_id=market_id,
            as_of=datetime.now(UTC),
            brier=float(metrics.get("brier") or metrics.get("brier_over") or 0),
            log_loss=float(metrics.get("log_loss") or 0),
            ece=float(metrics.get("ece") or metrics.get("ece_over") or 0),
            skill_vs_novig=metrics.get("skill_brier") or metrics.get("skill_mae"),
            n=int(metrics.get("n") or 0),
            coverage=1.0,
            freshness_minutes=0,
            notes=f"m3 {league_id} {market_id}; play_enabled=false; {fitted.epa_scale}",
        )
    )


def _write_artifacts(league_id: str, artifacts: dict[str, FittedMarket], train_end: datetime) -> None:
    path = _artifact_path(league_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "league_id": league_id,
                "train_end": train_end.isoformat(),
                "play_enabled": False,
                "markets": {k: asdict(v) for k, v in artifacts.items()},
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _artifact_path(league_id: str) -> Path:
    return Path(settings.parquet_dir) / "models" / f"{league_id}_m3.json"
