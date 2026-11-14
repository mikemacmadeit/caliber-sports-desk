"""Train/validate Phase 3 player Anytime TD models. Leaves M3/M4 bytes unchanged."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.config import settings
from spe.db.base import as_utc
from spe.models.advanced import binary_probability, opponent_adjustment, shrinkage_mean
from spe.models.metrics import apply_platt, brier_score, expected_calibration_error, log_loss, platt_calibrate
from spe.models.player_td.dataset import build_player_td_frame, chronological_player_split
from spe.models.player_td.model import PlayerTdModel, evaluate_binary, fit_player_td_model, predict_frame
from spe.models.temporal_integrity import training_cutoff
from spe.models.train import FittedMarket, load_artifacts
from spe.shadow.m4_artifact import adjust_mean, load_m4_artifacts, probability_from_mean

VERSION = "player_td_v1"
EVAL_WINDOWS = (
    ("2024", datetime(2024, 8, 1, tzinfo=UTC), datetime(2024, 8, 1, tzinfo=UTC), datetime(2025, 2, 15, tzinfo=UTC)),
    ("2025", datetime(2025, 8, 1, tzinfo=UTC), datetime(2025, 8, 1, tzinfo=UTC), datetime(2026, 2, 15, tzinfo=UTC)),
    ("2026_ytd", datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 11, 6, 23, 59, tzinfo=UTC)),
)


def artifact_path(league_id: str) -> Path:
    return Path(settings.parquet_dir) / "models" / f"{league_id}_{VERSION}.json"


def m3_path(league_id: str) -> Path:
    return Path(settings.parquet_dir) / "models" / f"{league_id}_m3.json"


def m4_path(league_id: str) -> Path:
    return Path(settings.parquet_dir) / "models" / f"{league_id}_m4.json"


def report_path() -> Path:
    return Path(settings.parquet_dir).parent / "phase3_player_td_report.json"


def train_player_td_models(
    session: Session,
    *,
    leagues: tuple[str, ...] = ("nfl", "fbs"),
    now: datetime | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    moment = as_utc(now) or datetime.now(UTC)
    m3_before = {lg: m3_path(lg).read_bytes() if m3_path(lg).exists() else None for lg in leagues}
    m4_before = {lg: m4_path(lg).read_bytes() if m4_path(lg).exists() else None for lg in leagues}
    cutoff = training_cutoff(session, now=moment)

    report: dict[str, Any] = {
        "status": "ok",
        "phase": 3,
        "version": VERSION,
        "as_of": moment.isoformat(),
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "validated_play": False,
        "m3_m4_untouched": True,
        "shadow_ledger_preserved": True,
        "architecture": {
            "family": "team_lambda_x_opportunity_share",
            "anytime_definition": "rush_td + rec_td only; QB pass TDs excluded",
            "team_source": "walk_forward_team_td",
            "leagues_separate": True,
            "availability": "never_assume_missing_injury_healthy",
        },
        "training_cutoff": cutoff,
        "leagues": {},
    }

    for league_id in leagues:
        cut = cutoff["leagues"].get(league_id, {})
        train_end = (
            datetime.fromisoformat(cut["train_cutoff"]) if cut.get("train_cutoff") else moment
        )
        frame, weights = build_player_td_frame(session, league_id, year_max=2026, universe="corrected")
        if frame.empty:
            report["leagues"][league_id] = {"status": "no_data"}
            continue
        kick = pd.to_datetime(frame["kickoff_utc"], utc=True)
        usable = frame.loc[kick < pd.Timestamp(train_end)].copy()
        # Leakage guard for 2026: any test row's features must only use prior games (priors are shifted).
        windows: list[dict[str, Any]] = []
        for label, te, ts, tend in EVAL_WINDOWS:
            train, test = chronological_player_split(usable, train_end=te, test_start=ts, test_end=tend)
            if len(train) < 300 or len(test) < 80:
                windows.append(
                    {
                        "window": label,
                        "status": "insufficient_rows",
                        "train_n": int(len(train)),
                        "test_n": int(len(test)),
                    }
                )
                continue
            # The model separates coefficient fitting and calibration internally.
            from spe.models.player_td.shares import default_weights
            model = fit_player_td_model(train, league_id, te.isoformat(), default_weights(league_id))
            pred = predict_frame(model, test)
            metrics = evaluate_binary(pred)
            baselines = _baseline_metrics(train, test, league_id)
            windows.append(
                {
                    "window": label,
                    "status": "ok",
                    "train_n": int(len(train)),
                    "test_n": int(len(test)),
                    "train_end": te.isoformat(),
                    "test_start": ts.isoformat(),
                    "test_end": tend.isoformat(),
                    "features": list(model.feature_betas.keys()),
                    "calibration_start": model.calibration_start,
                    "calibration_end": model.calibration_end,
                    "metrics": metrics,
                    "baselines": baselines,
                    "leakage_check": _leakage_check_2026(test, label),
                }
            )

        # Final artifact on all usable except last 45 days shadow.
        shadow_start = pd.to_datetime(usable["kickoff_utc"], utc=True).max() - pd.Timedelta(days=45)
        # Restrict to usable kickoffs
        ukick = pd.to_datetime(usable["kickoff_utc"], utc=True)
        train_final = usable.loc[ukick < shadow_start].copy()
        test_final = usable.loc[ukick >= shadow_start].copy()
        if len(train_final) < 500:
            from spe.models.player_td.temporal import split_games
            train_final, test_final = split_games(usable, 0.85)
        from spe.models.player_td.shares import default_weights
        final_model = fit_player_td_model(train_final, league_id, train_end.isoformat(), default_weights(league_id))
        shadow_pred = predict_frame(final_model, test_final) if len(test_final) else test_final
        shadow_metrics = evaluate_binary(shadow_pred) if len(test_final) else {}
        shadow_baselines = _baseline_metrics(train_final, test_final, league_id) if len(test_final) else {}

        if persist:
            path = artifact_path(league_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            blob = final_model.to_dict()
            blob["shadow_metrics"] = shadow_metrics
            blob["shadow_baselines"] = shadow_baselines
            blob["play_enabled"] = False
            blob["promoted"] = False
            blob["research_only"] = True
            path.write_text(json.dumps(blob, indent=2, default=str), encoding="utf-8")

        report["leagues"][league_id] = {
            "status": "ok",
            "n_rows": int(len(frame)),
            "n_usable": int(len(usable)),
            "seasons": sorted({int(y) for y in usable["season_year"].dropna().unique()}),
            "train_end": train_end.isoformat(),
            "artifact_path": str(artifact_path(league_id)),
            "share_weights": final_model.share_weights,
            "feature_betas": final_model.feature_betas,
            "shadow_metrics": shadow_metrics,
            "shadow_baselines": shadow_baselines,
            "windows": windows,
            "play_enabled": False,
            "promoted": False,
        }
        print(
            f"[player_td] {league_id} usable={len(usable)} shadow_n={len(test_final)} "
            f"brier={shadow_metrics.get('brier')} ll={shadow_metrics.get('log_loss')}",
            flush=True,
        )

    for league_id in leagues:
        if m3_path(league_id).exists() and m3_path(league_id).read_bytes() != m3_before[league_id]:
            raise RuntimeError(f"M3 changed during player_td train: {m3_path(league_id)}")
        if m4_path(league_id).exists() and m4_path(league_id).read_bytes() != m4_before[league_id]:
            raise RuntimeError(f"M4 changed during player_td train: {m4_path(league_id)}")

    if persist:
        report_path().parent.mkdir(parents=True, exist_ok=True)
        report_path().write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def load_player_td_model(league_id: str) -> PlayerTdModel | None:
    path = artifact_path(league_id)
    if not path.exists():
        return None
    return PlayerTdModel.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _leakage_check_2026(test: pd.DataFrame, label: str) -> dict[str, Any]:
    if test.empty:
        return {"status": "n/a"}
    # Priors are shift(1) expanding — verify no row has prior equal to same-game outcome correlation leak marker.
    # Practical check: kickoff must be <= cutoff and prior columns finite without using later games
    # (enforced by construction). Report max kickoff.
    kick = pd.to_datetime(test["kickoff_utc"], utc=True)
    team_fits_before = bool((pd.to_datetime(test["team_td_train_end"], utc=True) < kick).all())
    return {
        "status": "ok" if team_fits_before else "failed",
        "n": int(len(test)),
        "max_kickoff": kick.max().isoformat(),
        "note": "Team coefficients fit only on seasons before each row; later revisions of source data remain a limitation.",
        "team_fits_before_kickoff": team_fits_before,
        "upstream_artifacts": test[["team_td_train_end", "team_td_artifact_sha256"]].drop_duplicates().to_dict("records"),
    }


def _baseline_metrics(train: pd.DataFrame, test: pd.DataFrame, league_id: str) -> dict[str, Any]:
    y = (test["anytime_tds"].fillna(0) > 0).astype(float).to_numpy()
    # Historical TD-rate baseline (player prior)
    prior = pd.to_numeric(test.get("prior_td_rate"), errors="coerce").fillna(float(train["anytime_tds"].mean())).to_numpy()
    p_hist = binary_probability(np.clip(prior, 1e-6, None))
    out: dict[str, Any] = {
        "historical_td_rate": {
            "brier": brier_score(y, p_hist),
            "log_loss": log_loss(y, p_hist),
            "ece": expected_calibration_error(y, p_hist),
        }
    }
    # M3 refit on this window (same recipe as production M3) — does not write artifacts.
    try:
        base_tr, base_te = shrinkage_mean(train, test, "anytime_tds", league_id)
        m3_tr, m3_te, _ = opponent_adjustment(train, test, base_tr, base_te, "anytime_tds")
        y_tr = (train["anytime_tds"].fillna(0) > 0).astype(float).to_numpy()
        p_m3_tr = binary_probability(m3_tr)
        p_m3 = apply_platt(binary_probability(m3_te), *platt_calibrate(p_m3_tr, y_tr))
        out["m3_refit"] = {
            "brier": brier_score(y, p_m3),
            "log_loss": log_loss(y, p_m3),
            "ece": expected_calibration_error(y, p_m3),
        }
    except Exception as exc:
        out["m3_refit"] = {"status": "error", "error": str(exc)}

    # Frozen M3 artifact rates
    try:
        arts = load_artifacts(league_id)
        fitted = arts.get("anytime_td")
        if fitted is None:
            out["m3_artifact"] = {"status": "missing"}
        else:
            p_art = _predict_m3_frame(fitted, test)
            out["m3_artifact"] = {
                "comparable": False, "reason": "artifact_not_refit_inside_evaluation_fold",
                "brier": brier_score(y, p_art),
                "log_loss": log_loss(y, p_art),
                "ece": expected_calibration_error(y, p_art),
            }
    except Exception as exc:
        out["m3_artifact"] = {"status": "error", "error": str(exc)}

    # Frozen M4
    try:
        m4 = load_m4_artifacts(league_id)
        market = (m4.get("markets") or {}).get("anytime_td")
        if not market:
            out["m4_artifact"] = {"status": "missing"}
        else:
            # Start from M3 shrinkage means on test, then apply frozen betas.
            base_tr, base_te = shrinkage_mean(train, test, "anytime_tds", league_id)
            m3_tr, m3_te, _ = opponent_adjustment(train, test, base_tr, base_te, "anytime_tds")
            ps = []
            for i, rec in enumerate(test.itertuples(index=False)):
                feats = {name: getattr(rec, name, None) for name in (market.get("features") or [])}
                # Also common feature names
                for name in ("opp_def_z", "is_home", "own_off_epa", "workload_delta", "prior_td_rate"):
                    if hasattr(rec, name):
                        feats[name] = getattr(rec, name)
                mean, _missing = adjust_mean(float(m3_te[i]), feats, market)
                ps.append(probability_from_mean(mean, market, line=0.5, side="yes"))
            p_m4 = np.asarray(ps, dtype=float)
            out["m4_artifact"] = {
                "comparable": False, "reason": "artifact_not_refit_inside_evaluation_fold",
                "brier": brier_score(y, p_m4),
                "log_loss": log_loss(y, p_m4),
                "ece": expected_calibration_error(y, p_m4),
            }
    except Exception as exc:
        out["m4_artifact"] = {"status": "error", "error": str(exc)}
    return out


def _predict_m3_frame(fitted: FittedMarket, test: pd.DataFrame) -> np.ndarray:
    ps = []
    for rec in test.itertuples(index=False):
        prior = fitted.pos_rates.get(str(rec.position_group), fitted.league_rate)
        team_prior = fitted.team_rates.get(str(rec.team_id or ""), prior)
        n = int(fitted.player_n.get(str(rec.player_id), 0))
        raw = fitted.player_rates.get(str(rec.player_id), team_prior)
        shrink = n / (n + fitted.k_player) if n else 0.0
        rate = shrink * raw + (1 - shrink) * team_prior
        z = float(getattr(rec, "opp_def_z", 0.0) or 0.0)
        mean = max(1e-6, rate * float(np.exp(np.clip(fitted.opp_beta * z, -0.35, 0.35))))
        p_raw = float(1.0 - np.exp(-mean))
        p = float(apply_platt(np.array([p_raw]), fitted.platt_a, fitted.platt_b)[0])
        ps.append(p)
    return np.asarray(ps, dtype=float)
