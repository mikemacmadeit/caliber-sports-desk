"""Chronological evaluation harness. Baseline rates are not a production model."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.analytics.engine import shrink
from spe.analytics.persist import load_frames
from spe.db.base import new_id
from spe.db.models import BacktestPrediction, BacktestRun, ModelHealthSnapshot, ModelVersion
from spe.eval.leakage import assert_event_time
from spe.models.metrics import brier_score, expected_calibration_error, log_loss


def run_baseline_anytime_td(
    session: Session,
    league_id: str,
    *,
    train_end: datetime,
    test_start: datetime,
    test_end: datetime,
    k: float | None = None,
) -> dict[str, Any]:
    """Empirical-Bayes anytime-TD rate using only games with kickoff < as_of.

    This is infrastructure + a naive baseline, not a validated forecasting model.
    """
    from spe.analytics.definitions import SHRINKAGE_K

    k = SHRINKAGE_K.get(league_id, 10.0) if k is None else k
    games, _plays, stats, players = load_frames(session, league_id)
    if stats.empty:
        return {"status": "no_stats", "league_id": league_id, "n": 0}
    stats["kickoff_utc"] = pd.to_datetime(stats["kickoff_utc"], utc=True)
    if "tds" not in stats.columns:
        stats["tds"] = stats["rush_td"].fillna(0) + stats["rec_td"].fillna(0)
    stats["y"] = (stats["tds"] > 0).astype(float)
    if players is not None and not players.empty:
        stats = stats.merge(players[["player_id", "position_group"]], on="player_id", how="left")
    stats = stats[stats.get("position_group", "WR").isin(["WR", "TE", "RB", "QB"])] if "position_group" in stats.columns else stats

    test = stats[(stats["kickoff_utc"] >= pd.Timestamp(test_start)) & (stats["kickoff_utc"] <= pd.Timestamp(test_end))]
    train_all = stats[stats["kickoff_utc"] <= pd.Timestamp(train_end)]
    if test.empty:
        return {"status": "no_test_rows", "league_id": league_id, "n": 0}

    pos_rate = train_all.groupby("position_group")["y"].mean().to_dict() if "position_group" in train_all.columns else {}
    league_rate = float(train_all["y"].mean()) if len(train_all) else 0.15

    preds: list[dict[str, Any]] = []
    for rec in test.itertuples(index=False):
        as_of = pd.Timestamp(rec.kickoff_utc).to_pydatetime()
        hist = stats[(stats["player_id"] == rec.player_id) & (stats["kickoff_utc"] < rec.kickoff_utc)]
        hist = hist[hist["kickoff_utc"] <= pd.Timestamp(train_end)]
        n = len(hist)
        raw = float(hist["y"].mean()) if n else league_rate
        prior = float(pos_rate.get(getattr(rec, "position_group", None), league_rate))
        p = float(np.clip(shrink(raw, prior, n, k), 0.02, 0.85))
        observed = hist["kickoff_utc"].max().to_pydatetime() if n else as_of.replace(year=as_of.year - 1)
        if n:
            assert_event_time(as_of=as_of, observed_at=observed)
        preds.append(
            {
                "game_id": rec.game_id,
                "player_id": rec.player_id,
                "as_of": as_of,
                "train_cutoff": min(as_of, train_end),
                "p": p,
                "y": float(rec.y),
                "components": {"n_prior": n, "raw": raw, "prior": prior, "k": k},
            }
        )

    y = np.array([r["y"] for r in preds], dtype=float)
    p = np.array([r["p"] for r in preds], dtype=float)
    metrics = {
        "n": int(len(preds)),
        "brier": brier_score(y, p) if len(preds) else None,
        "log_loss": log_loss(y, p) if len(preds) else None,
        "ece": expected_calibration_error(y, p) if len(preds) else None,
        "coverage": 1.0,
        "base_rate": float(y.mean()) if len(preds) else None,
        "mean_p": float(p.mean()) if len(preds) else None,
    }

    version = session.get(ModelVersion, f"baseline-td-{league_id}")
    if version is None:
        session.add(
            ModelVersion(
                model_version_id=f"baseline-td-{league_id}",
                code_sha="baseline",
                training_cutoff=train_end,
                data_snapshot="historical_pregame",
                hyperparameters={"k": k, "family": "empirical_bayes_rate"},
                calibration_version="none",
                league_id=league_id,
                family="baseline_anytime_td",
            )
        )
        session.flush()

    run = BacktestRun(
        run_id=new_id(),
        league_id=league_id,
        market_id="anytime_td",
        model_family="baseline_empirical_rate",
        train_end=train_end,
        test_start=test_start,
        test_end=test_end,
        n=metrics["n"],
        brier=metrics["brier"],
        log_loss=metrics["log_loss"],
        ece=metrics["ece"],
        coverage=metrics["coverage"],
        notes="Naive shrinkage baseline for harness testing. Not a validated Caliber forecast.",
    )
    session.add(run)
    session.flush()
    for row in preds:
        session.add(
            BacktestPrediction(
                run_id=run.run_id,
                game_id=row["game_id"],
                player_id=row["player_id"],
                as_of=row["as_of"],
                train_cutoff=row["train_cutoff"],
                p=row["p"],
                y=row["y"],
                components=row["components"],
            )
        )
    session.add(
        ModelHealthSnapshot(
            league_id=league_id,
            market_id="anytime_td",
            as_of=datetime.now(UTC),
            brier=metrics["brier"] or 0,
            log_loss=metrics["log_loss"] or 0,
            ece=metrics["ece"] or 0,
            skill_vs_novig=None,
            n=metrics["n"],
            coverage=1.0,
            freshness_minutes=0,
            notes="baseline_empirical_rate; not validated. No-vig unidentified without historical prices.",
        )
    )
    session.flush()
    return {"status": "ok", "league_id": league_id, "run_id": run.run_id, **metrics}
