"""Chronological ATS backtests. Never trains on games after the prediction date."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.config import settings
from spe.db.base import new_id
from spe.db.models import BacktestPrediction, BacktestRun, ModelVersion
from spe.market.odds import american_to_implied, ev_per_dollar
from spe.models.ats.labels import cover_indicator
from spe.models.ats.model import (
    AtsModelArtifact,
    calibrate_cover_platt,
    cover_metrics,
    fit_league_mean_baseline,
    fit_ridge_margin,
    fit_simple_rating_baseline,
    regression_metrics,
    score_predictions,
)
from spe.models.ats.ratings import FEATURE_COLS, build_team_strength_frame
from spe.models.ats.simulate import compare_simulation_to_model


HOLD_OUT_START = {
    # Untouched prospective-style window: most recent completed seasons.
    "nfl": "2024-09-01T00:00:00+00:00",
    "fbs": "2024-08-15T00:00:00+00:00",
}


def run_ats_backtest(
    session: Session,
    league_id: str,
    *,
    holdout_start: str | None = None,
    min_train_games: int = 200,
    feature_ablation: bool = True,
) -> dict[str, Any]:
    frame = build_team_strength_frame(session, league_id)
    if frame.empty:
        return {"status": "no_features", "league_id": league_id}

    holdout = pd.Timestamp(holdout_start or HOLD_OUT_START[league_id])
    frame["kickoff_utc"] = pd.to_datetime(frame["kickoff_utc"], utc=True)
    labeled = frame.dropna(subset=["margin"]).copy()
    train = labeled[labeled["kickoff_utc"] < holdout]
    test = labeled[labeled["kickoff_utc"] >= holdout]
    if len(train) < min_train_games or test.empty:
        return {
            "status": "insufficient_split",
            "league_id": league_id,
            "train_n": int(len(train)),
            "test_n": int(len(test)),
            "holdout_start": holdout.isoformat(),
        }

    models: dict[str, AtsModelArtifact] = {
        "league_home_mean": fit_league_mean_baseline(train, league_id=league_id),
        "simple_rating": fit_simple_rating_baseline(train, league_id=league_id),
        "ridge_margin": fit_ridge_margin(train, league_id=league_id, alpha=8.0 if league_id == "nfl" else 12.0),
    }
    # Calibrate cover probs on train games that have closing lines (NFL).
    if train["spread_line"].notna().sum() >= 40:
        models["ridge_margin"] = calibrate_cover_platt(models["ridge_margin"], train)

    comparisons: dict[str, Any] = {}
    for name, art in models.items():
        scored = score_predictions(art, test)
        y = scored["margin"].to_numpy(dtype=float)
        yhat = scored["predicted_margin"].to_numpy(dtype=float)
        reg = regression_metrics(y, yhat)
        cover = _ats_cover_eval(scored)
        comparisons[name] = {
            "family": art.family,
            "regression": reg,
            "cover": cover,
            "sigma": art.sigma,
            "train_n": art.train_n,
        }

    primary = models["ridge_margin"]
    scored_primary = score_predictions(primary, test)
    ablation = _feature_ablation(train, test, league_id) if feature_ablation else {}
    sim_compare = compare_simulation_to_model(scored_primary.head(min(400, len(scored_primary))), primary)

    version_id = f"ats-ridge-{league_id}-{holdout.strftime('%Y%m%d')}"
    primary.model_version_id = version_id
    _persist_model_version(session, primary, holdout)
    run_id = _persist_backtest_run(session, league_id, version_id, holdout, comparisons, scored_primary)

    report = {
        "status": "ok",
        "league_id": league_id,
        "holdout_start": holdout.isoformat(),
        "train_n": int(len(train)),
        "test_n": int(len(test)),
        "model_version_id": version_id,
        "backtest_run_id": run_id,
        "comparisons": comparisons,
        "feature_ablation": ablation,
        "simulation_compare": sim_compare,
        "market_line_coverage": {
            "train_with_spread": int(train["spread_line"].notna().sum()),
            "test_with_spread": int(test["spread_line"].notna().sum()),
            "source": "nflverse_consensus" if league_id == "nfl" else "cfbd",
            "note": (
                "NFL consensus closing spreads from nflverse — not bookmaker ticks."
                if league_id == "nfl"
                else "FBS home handicaps from CollegeFootballData /lines. Negative means home favored."
            ),
        },
        "slices": _slice_metrics(scored_primary),
        "research_only": True,
        "play_enabled": False,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    _write_report(league_id, report)
    _write_artifact(league_id, primary)
    return report


def _ats_cover_eval(scored: pd.DataFrame) -> dict[str, Any]:
    has_line = scored.dropna(subset=["spread_line", "margin"]).copy()
    if has_line.empty:
        return {
            "n": 0,
            "note": "No historical spreads available for ATS grading on this slice.",
        }
    y = []
    p = []
    returns = []
    for row in has_line.itertuples(index=False):
        # Evaluate home side once. cover_indicator expects sportsbook handicap h.
        handicap = getattr(row, "home_handicap", None)
        if handicap is None or (isinstance(handicap, float) and np.isnan(handicap)):
            # Legacy path: treat stored spread_line as nflverse L → h = -L.
            handicap = -float(row.spread_line)
        ind = cover_indicator(float(row.margin), float(handicap), "home")
        if ind is None:
            continue
        y.append(ind)
        p_home = float(row.p_home_cover)
        p.append(p_home)
        home_price = getattr(row, "home_spread_price", None)
        away_price = getattr(row, "away_spread_price", None)
        # Model-directed flat stake: take the side with higher cover probability
        # only when priced; one side per game.
        if p_home >= 0.5:
            price = home_price
            won = ind >= 0.5
        else:
            price = away_price
            won = ind < 0.5
            # away cover when home did not
        if price is not None and not (isinstance(price, float) and np.isnan(price)):
            am = int(price)
            if won:
                returns.append((am / 100.0) if am > 0 else (100.0 / abs(am)))
            else:
                returns.append(-1.0)
    metrics = cover_metrics(np.asarray(y, dtype=float), np.asarray(p, dtype=float))
    # Market baseline: implied from home_spread_price when present.
    market = _market_baseline(has_line)
    flat = None
    if returns:
        flat = {
            "n": len(returns),
            "flat_stake_roi": float(np.mean(returns)),
            "note": (
                "Hypothetical flat $1 on the model-favored side at consensus closing price. "
                "Not a profit claim. Closing consensus ≠ live book ticks."
            ),
        }
    return {
        **metrics,
        "market_baseline": market,
        "flat_stake": flat,
        "beats_market_brier": (
            metrics["brier"] is not None
            and market.get("brier") is not None
            and metrics["brier"] < market["brier"]
        ),
    }


def _market_baseline(has_line: pd.DataFrame) -> dict[str, Any]:
    """Market cover baseline from real spread prices only — never invent -110 or 0.5."""
    y = []
    p = []
    skipped_no_price = 0
    for row in has_line.itertuples(index=False):
        handicap = getattr(row, "home_handicap", None)
        if handicap is None or (isinstance(handicap, float) and np.isnan(handicap)):
            handicap = -float(row.spread_line)
        ind = cover_indicator(float(row.margin), float(handicap), "home")
        if ind is None:
            continue
        price = getattr(row, "home_spread_price", None)
        if price is None or (isinstance(price, float) and np.isnan(price)):
            skipped_no_price += 1
            continue
        p.append(float(american_to_implied(int(price))))
        y.append(ind)
    out = cover_metrics(np.asarray(y, dtype=float), np.asarray(p, dtype=float))
    out["skipped_no_price"] = skipped_no_price
    out["note"] = (
        "Vigged implied probabilities from nflverse home_spread_odds when present. "
        "Consensus prices are not executable sportsbook ticks. Missing prices excluded."
    )
    return out


def _feature_ablation(train: pd.DataFrame, test: pd.DataFrame, league_id: str) -> dict[str, Any]:
    """Test whether groups of features improve held-out MAE vs a lean core."""
    groups = {
        "core_epa": ["epa_edge", "home_field", "sample_size"],
        "plus_success_pace": [
            "epa_edge",
            "success_edge",
            "home_pace",
            "away_pace",
            "home_field",
            "sample_size",
        ],
        "plus_scoring": [
            "epa_edge",
            "success_edge",
            "ppg_edge",
            "home_ppg",
            "away_ppg",
            "home_field",
            "sample_size",
        ],
        "plus_rest_weather": [
            "epa_edge",
            "success_edge",
            "ppg_edge",
            "rest_edge",
            "weather_wind_mps",
            "weather_available",
            "home_field",
            "sample_size",
        ],
        "full": list(FEATURE_COLS),
    }
    out: dict[str, Any] = {}
    for name, cols in groups.items():
        art = fit_ridge_margin(train, league_id=league_id, alpha=10.0, feature_cols=cols)
        scored = score_predictions(art, test, apply_calibration=False)
        y = scored["margin"].to_numpy(dtype=float)
        yhat = scored["predicted_margin"].to_numpy(dtype=float)
        out[name] = regression_metrics(y, yhat)
    # Rank by MAE
    ranked = sorted(out.items(), key=lambda kv: kv[1]["mae"])
    out["_best_by_mae"] = ranked[0][0] if ranked else None
    out["_note"] = "Lower MAE is better. Groups are nested expansions; do not overfit to this single holdout."
    return out


def _slice_metrics(scored: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {}
    labeled = scored.dropna(subset=["margin"])
    if labeled.empty:
        return out
    y = labeled["margin"].to_numpy(dtype=float)
    yhat = labeled["predicted_margin"].to_numpy(dtype=float)
    out["overall"] = regression_metrics(y, yhat)

    # By absolute market spread size when available.
    if labeled["spread_line"].notna().any():
        abs_line = labeled["spread_line"].abs()
        for label, mask in (
            ("spread_0_3", abs_line <= 3),
            ("spread_3_7", (abs_line > 3) & (abs_line <= 7)),
            ("spread_7_plus", abs_line > 7),
        ):
            part = labeled[mask]
            if len(part) >= 20:
                out[label] = regression_metrics(part["margin"].to_numpy(), part["predicted_margin"].to_numpy())

    # Favorites: nflverse L > 0 ⇒ home favored. Prefer market_home_margin when present.
    if labeled["spread_line"].notna().any():
        mkt = labeled["market_home_margin"] if "market_home_margin" in labeled.columns else labeled["spread_line"]
        fav = labeled[mkt > 0]
        dog = labeled[mkt < 0]
        if len(fav) >= 20:
            out["home_favorites"] = regression_metrics(fav["margin"].to_numpy(), fav["predicted_margin"].to_numpy())
        if len(dog) >= 20:
            out["home_dogs"] = regression_metrics(dog["margin"].to_numpy(), dog["predicted_margin"].to_numpy())

    # Confidence proxy: sample_size and |epa_edge|
    high = labeled[labeled["sample_size"] >= labeled["sample_size"].median()]
    if len(high) >= 20:
        out["higher_sample"] = regression_metrics(high["margin"].to_numpy(), high["predicted_margin"].to_numpy())
    return out


def _persist_model_version(session: Session, art: AtsModelArtifact, holdout: pd.Timestamp) -> None:
    existing = session.get(ModelVersion, art.model_version_id)
    payload = {
        "model_version_id": art.model_version_id,
        "code_sha": "ats-research",
        "training_cutoff": holdout.to_pydatetime(),
        "data_snapshot": "ats_pregame_v1",
        "hyperparameters": art.to_dict(),
        "calibration_version": "platt_v1" if art.platt_b != 1.0 or art.platt_a != 0.0 else "none",
        "league_id": art.league_id,
        "family": "ats_margin",
    }
    if existing is None:
        session.add(ModelVersion(**payload))
    else:
        for key, value in payload.items():
            if key != "model_version_id":
                setattr(existing, key, value)
    session.flush()


def _persist_backtest_run(
    session: Session,
    league_id: str,
    version_id: str,
    holdout: pd.Timestamp,
    comparisons: dict[str, Any],
    scored: pd.DataFrame,
) -> str:
    run_id = new_id()
    ridge = comparisons.get("ridge_margin", {})
    cover = ridge.get("cover") or {}
    session.add(
        BacktestRun(
            run_id=run_id,
            league_id=league_id,
            market_id="point_spread",
            model_family=version_id,
            train_end=holdout.to_pydatetime(),
            test_start=holdout.to_pydatetime(),
            test_end=datetime.now(UTC),
            n=int((ridge.get("regression") or {}).get("n") or len(scored)),
            brier=cover.get("brier"),
            log_loss=cover.get("log_loss"),
            ece=cover.get("ece"),
            coverage=1.0,
            notes=json.dumps(
                {
                    "comparisons": comparisons,
                    "research_only": True,
                    "play_enabled": False,
                },
                default=str,
            )[:4000],
        )
    )
    session.flush()
    # Store a capped sample of predictions for audit.
    sample = scored.dropna(subset=["margin"]).head(500)
    for row in sample.itertuples(index=False):
        session.add(
            BacktestPrediction(
                id=new_id(),
                run_id=run_id,
                game_id=str(row.game_id),
                player_id=None,
                as_of=pd.Timestamp(row.kickoff_utc).to_pydatetime(),
                train_cutoff=holdout.to_pydatetime(),
                p=float(getattr(row, "p_home_cover", 0.5) or 0.5),
                y=float(row.margin),
                components={
                    "predicted_margin": float(row.predicted_margin),
                    "spread_line": None if pd.isna(getattr(row, "spread_line", np.nan)) else float(row.spread_line),
                    "family": "ats_ridge_margin",
                },
            )
        )
    session.flush()
    return run_id


def _write_report(league_id: str, report: dict[str, Any]) -> Path:
    path = Path(settings.parquet_dir) / "models" / "ats" / f"backtest_{league_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path


def _write_artifact(league_id: str, art: AtsModelArtifact) -> Path:
    """Persist research outputs without silently clobbering protected production ridge files.

    If model_{league}.json already exists, leave ridge weights untouched and write any
    contract-valid Platt overlay to research_calibrator_{league}.json only.
    """
    from spe.models.ats.research_bundle import research_calibrator_path
    from spe.models.ats.spread_contract import CURRENT_SPREAD_CONTRACT

    root = Path(settings.parquet_dir) / "models" / "ats"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"model_{league_id}.json"
    snapshot = root / f"model_{league_id}_backtest_snapshot.json"
    snapshot.write_text(json.dumps(art.to_dict(), indent=2), encoding="utf-8")

    if (art.metrics or {}).get("spread_contract") == CURRENT_SPREAD_CONTRACT:
        cal_path = research_calibrator_path(league_id)
        cal_path.write_text(json.dumps(art.to_dict(), indent=2), encoding="utf-8")

    if path.exists():
        # Protected production artifact — do not overwrite.
        return path

    # First-time install only: write ridge file with identity Platt (calibrator is separate).
    bare = art.to_dict()
    bare["platt_a"] = 0.0
    bare["platt_b"] = 1.0
    bare["metrics"] = {
        **(bare.get("metrics") or {}),
        "note": "Initial ridge install; apply research_calibrator_* for cover probs.",
    }
    path.write_text(json.dumps(bare, indent=2), encoding="utf-8")
    return path


def load_ats_artifact(league_id: str) -> AtsModelArtifact | None:
    """Raw production margin file. Prefer ``load_ats_research_bundle`` for live research paths."""
    path = Path(settings.parquet_dir) / "models" / "ats" / f"model_{league_id}.json"
    if not path.exists():
        return None
    return AtsModelArtifact.from_dict(json.loads(path.read_text(encoding="utf-8")))
