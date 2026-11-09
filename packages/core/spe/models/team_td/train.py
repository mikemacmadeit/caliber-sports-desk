"""Train and chronologically validate team-TD opportunity models.

Does not touch M3/M4 artifacts. Does not enable Validated Play. Research-only.
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
from spe.db.base import as_utc
from spe.models.team_td.dataset import build_team_td_frame, chronological_split_frame
from spe.models.team_td.model import TeamTdModel, evaluate_predictions, fit_team_td_model, predict_frame
from spe.models.temporal_integrity import training_cutoff

VERSION = "team_td_v1"
EVAL_WINDOWS = (
    # (label, train_end, test_start, test_end)
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
    return Path(settings.parquet_dir).parent / "phase2_team_td_report.json"


def train_team_td_models(
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
        "phase": 2,
        "version": VERSION,
        "as_of": moment.isoformat(),
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "validated_play": False,
        "m3_m4_untouched": True,
        "architecture": {
            "family": "poisson_glm_irls",
            "targets": ["off_td", "pass_td", "rush_td"],
            "distributions": ["0", "1", "2", "3", "4", "5+"],
            "pass_rush": "independent Poisson means rescaled to sum to total",
            "def_st": "excluded from offensive totals",
            "leagues_separate": True,
        },
        "training_cutoff": cutoff,
        "leagues": {},
    }

    for league_id in leagues:
        print(f"[team_td] building frame {league_id}…", flush=True)
        frame = build_team_td_frame(session, league_id)
        if frame.empty:
            report["leagues"][league_id] = {"status": "no_data"}
            continue
        cut = cutoff["leagues"].get(league_id, {})
        train_end = (
            datetime.fromisoformat(cut["train_cutoff"]) if cut.get("train_cutoff") else moment
        )
        usable = frame.loc[pd.to_datetime(frame["kickoff_utc"], utc=True) < pd.Timestamp(train_end)].copy()
        seasons = sorted({int(y) for y in usable["season_year"].dropna().unique()})
        windows: list[dict[str, Any]] = []
        for label, te, ts, tend in EVAL_WINDOWS:
            train, test = chronological_split_frame(usable, train_end=te, test_start=ts, test_end=tend)
            if len(train) < 200 or len(test) < 50:
                windows.append(
                    {
                        "window": label,
                        "status": "insufficient_rows",
                        "train_n": int(len(train)),
                        "test_n": int(len(test)),
                    }
                )
                continue
            model = fit_team_td_model(train, league_id, te.isoformat())
            metrics = evaluate_predictions(test, model)
            m3_cmp = _compare_m3_team_scoring(session, league_id, test, model)
            windows.append(
                {
                    "window": label,
                    "status": "ok",
                    "train_n": int(len(train)),
                    "test_n": int(len(test)),
                    "train_end": te.isoformat(),
                    "test_start": ts.isoformat(),
                    "test_end": tend.isoformat(),
                    "features": model.feature_names,
                    "metrics": metrics,
                    "m3_team_scoring_comparison": m3_cmp,
                }
            )

        # Final artifact: all usable history through train_end; last 45 days as shadow metrics.
        kick = pd.to_datetime(usable["kickoff_utc"], utc=True)
        shadow_start = kick.max() - pd.Timedelta(days=45)
        train_final = usable.loc[kick < shadow_start].copy()
        test_final = usable.loc[kick >= shadow_start].copy()
        if len(train_final) < 400:
            cut_i = int(len(usable) * 0.85)
            train_final = usable.iloc[:cut_i].copy()
            test_final = usable.iloc[cut_i:].copy()
        final_model = fit_team_td_model(train_final, league_id, train_end.isoformat())
        final_metrics = evaluate_predictions(test_final, final_model) if len(test_final) else {}

        if persist:
            path = artifact_path(league_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            blob = final_model.to_dict()
            blob["shadow_metrics"] = final_metrics
            blob["windows"] = [{"window": w["window"], "status": w["status"]} for w in windows]
            blob["play_enabled"] = False
            blob["promoted"] = False
            blob["research_only"] = True
            path.write_text(json.dumps(blob, indent=2, default=str), encoding="utf-8")

        report["leagues"][league_id] = {
            "status": "ok",
            "n_rows": int(len(frame)),
            "n_usable": int(len(usable)),
            "seasons": seasons,
            "train_end": train_end.isoformat(),
            "artifact_path": str(artifact_path(league_id)),
            "features": final_model.feature_names,
            "baseline_mean": final_model.baseline_mean,
            "shadow_metrics": final_metrics,
            "windows": windows,
            "play_enabled": False,
            "promoted": False,
        }
        print(
            f"[team_td] {league_id} usable={len(usable)} shadow_n={len(test_final)} "
            f"log_loss={final_metrics.get('model', {}).get('log_loss')}",
            flush=True,
        )

    # Safety: M3/M4 bytes unchanged.
    for league_id in leagues:
        m3_after = m3_path(league_id).read_bytes() if m3_path(league_id).exists() else None
        m4_after = m4_path(league_id).read_bytes() if m4_path(league_id).exists() else None
        if m3_after != m3_before[league_id]:
            raise RuntimeError(f"M3 artifact changed during team_td train: {m3_path(league_id)}")
        if m4_after != m4_before[league_id]:
            raise RuntimeError(f"M4 artifact changed during team_td train: {m4_path(league_id)}")

    if persist:
        report_path().parent.mkdir(parents=True, exist_ok=True)
        # Live scoring filled in by score.py / orchestrator.
        report_path().write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    return report


def load_team_td_model(league_id: str) -> TeamTdModel | None:
    path = artifact_path(league_id)
    if not path.exists():
        return None
    return TeamTdModel.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _compare_m3_team_scoring(
    session: Session,
    league_id: str,
    test: pd.DataFrame,
    model: TeamTdModel,
) -> dict[str, Any]:
    """Compare against M3's implied team scoring when an artifact exists.

    M3 is player-level; we approximate team expected TDs as the sum of skill-player
    anytime means when available. If the artifact is missing, report unavailable.
    """
    del session  # reserved for future player-sum lookups against stored rates
    path = m3_path(league_id)
    if not path.exists():
        return {"status": "m3_artifact_missing"}
    try:
        art = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "m3_unreadable", "error": str(exc)}
    # M3 anytime uses player rates; team-level assumption ≈ league rate * skill slots is weak.
    # Use team_rates from the anytime_td market if present as a coarse prior.
    markets = art.get("markets") or art
    anytime = markets.get("anytime_td") if isinstance(markets, dict) else None
    if anytime is None and isinstance(art, dict) and art.get("family"):
        anytime = art
    if not isinstance(anytime, dict):
        return {"status": "m3_no_anytime_market"}
    team_rates = anytime.get("team_rates") or {}
    if not team_rates:
        return {"status": "m3_no_team_rates", "note": "M3 stores player-level rates; no direct team TD total"}
    # Approximate: expected team offensive TDs ≈ sum of top-skill rates is unavailable without roster.
    # Fall back to mean team rate * 4 skill contributors as a documented coarse baseline.
    approx = []
    actual = []
    model_lam = []
    pred = predict_frame(model, test)
    for i, rec in enumerate(test.itertuples(index=False)):
        rate = team_rates.get(rec.team_id)
        if rate is None:
            continue
        approx.append(float(rate) * 4.0)
        actual.append(float(rec.off_td))
        model_lam.append(float(pred["lambda_off_td"].iloc[i]))
    if len(approx) < 30:
        return {"status": "too_few_overlapping_teams", "n": len(approx)}
    approx_a = np.asarray(approx)
    actual_a = np.asarray(actual)
    model_a = np.asarray(model_lam)
    return {
        "status": "ok",
        "n": len(approx),
        "note": "M3 team_rate * 4 skill slots is a coarse proxy, not an official team-TD model",
        "m3_proxy_mae": float(np.mean(np.abs(approx_a - actual_a))),
        "team_td_mae": float(np.mean(np.abs(model_a - actual_a))),
        "m3_proxy_mean": float(np.mean(approx_a)),
        "team_td_mean": float(np.mean(model_a)),
        "actual_mean": float(np.mean(actual_a)),
    }
