"""Chronological research study for improved individual Anytime TD models.

Writes only under ``data/parquet/models/player_td/`` and ``data/player_td_dev/``.
Never touches Agent 1 team_td code, live forecasts, or M3/M4/M5 artifacts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from spe.models.metrics import brier_score
from spe.models.player_td.candidates import (
    FAMILY_FITTERS,
    FitResult,
    fit_ensemble,
    score_probs,
    select_usable_features,
)
from spe.models.player_td.features import all_candidate_features, build_opportunity_study_frame, feature_coverage
from spe.models.player_td.integration import integration_requirements
from spe.models.player_td.isolation import (
    VERSION,
    artifact_path,
    assert_protected_unchanged,
    comparison_path,
    development_database_url,
    report_path,
    safety_banner,
    snapshot_protected_artifacts,
)
from spe.models.player_td.positions import POSITIONS, features_for_position, position_catalog
from spe.models.rolling_td import TD_FEATURES_FBS, TD_FEATURES_NFL
from spe.models.temporal_integrity import filter_completed_frame, training_cutoff

SELECTION_YEARS = (2022, 2023)
REPORT_YEARS = (2024, 2025, 2026)
MIN_GAIN = 0.0003
# Seed ablation with the M5 core so we measure *incremental* opportunity/defense gains.
SEED_FEATURES = {
    "nfl": list(TD_FEATURES_NFL),
    "fbs": list(TD_FEATURES_FBS),
}


def _session() -> Session:
    url = development_database_url()
    engine = create_engine(url, future=True)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def season_mask(frame: pd.DataFrame, year: int, *, train: bool) -> pd.Series:
    kick = pd.to_datetime(frame["kickoff_utc"], utc=True)
    start = pd.Timestamp(f"{year}-08-01", tz="UTC")
    end = pd.Timestamp(f"{year + 1}-03-01", tz="UTC")
    if train:
        return kick < start
    return (kick >= start) & (kick < end)


def load_baseline_metrics() -> dict[str, Any]:
    """Read frozen M3/M4/M5 comparison summaries without modifying them."""
    root = Path("data/parquet/models")
    out: dict[str, Any] = {"m3": {}, "m4": {}, "m5": {}}
    for league in ("nfl", "fbs"):
        m3 = root / f"{league}_m3.json"
        m5 = root / f"{league}_m5.json"
        if m3.exists():
            raw = json.loads(m3.read_text(encoding="utf-8"))
            out["m3"][league] = {"train_end": raw.get("train_end"), "play_enabled": raw.get("play_enabled")}
        if m5.exists():
            raw = json.loads(m5.read_text(encoding="utf-8"))
            out["m5"][league] = {
                "train_end": raw.get("train_end"),
                "shadow": raw.get("shadow_metrics") or raw.get("shadow"),
                "features": raw.get("features"),
                "play_enabled": raw.get("play_enabled"),
                "promoted": raw.get("promoted"),
            }
    m4c = root / "m4_comparison.json"
    if m4c.exists():
        out["m4"]["comparison_present"] = True
        cmp = json.loads(m4c.read_text(encoding="utf-8"))
        leagues = []
        for block in cmp.get("leagues") or []:
            atd = (block.get("markets") or {}).get("anytime_td") or {}
            leagues.append(
                {
                    "league_id": block.get("league_id"),
                    "features": atd.get("features"),
                    "windows": [
                        {
                            "season": w.get("season"),
                            "m3_brier": ((w.get("m3") or {}).get("brier")),
                            "new_brier": ((w.get("new") or {}).get("brier")),
                        }
                        for w in atd.get("windows") or []
                    ],
                }
            )
        out["m4"]["anytime_td"] = leagues
    m5c = root / "m5_comparison.json"
    if m5c.exists():
        out["m5"]["comparison_present"] = True
    return out


def ablating_features(
    frame: pd.DataFrame,
    league_id: str,
    candidates: list[str],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Greedy ablation: keep a feature only if it helps every selection season.

    Starts from the M5 seed set (coverage-filtered) and tests incremental adds.
    """
    seed = [f for f in SEED_FEATURES.get(league_id, []) if f in frame.columns and feature_coverage(frame, [f]).get(f, 0) >= 0.25]
    chosen: list[str] = list(seed)
    decisions: list[dict[str, Any]] = [
        {
            "feature": f,
            "kept": True,
            "reason": "m5_seed",
            "coverages": {},
            "gains": {},
        }
        for f in seed
    ]
    fit = FAMILY_FITTERS["hierarchical_opp"]
    for feature in candidates:
        if feature in chosen:
            continue
        coverages = {str(y): float(feature_coverage(frame[season_mask(frame, y, train=True)], [feature]).get(feature, 0.0)) for y in SELECTION_YEARS}
        if max(coverages.values(), default=0.0) < 0.25:
            decisions.append(
                {
                    "feature": feature,
                    "kept": False,
                    "reason": "below_coverage_on_selection_seasons",
                    "coverages": coverages,
                    "gains": {},
                }
            )
            continue
        gains: list[float] = []
        gain_map: dict[str, float] = {}
        ok = True
        for year in SELECTION_YEARS:
            train = frame[season_mask(frame, year, train=True)]
            test = frame[season_mask(frame, year, train=False)]
            if len(train) < 300 or len(test) < 80:
                ok = False
                break
            base = fit(train, test, league_id, chosen)
            trial = fit(train, test, league_id, [*chosen, feature])
            gain = float(base.metrics["brier"] - trial.metrics["brier"])
            gains.append(gain)
            gain_map[str(year)] = gain
        if not ok or not gains:
            decisions.append(
                {
                    "feature": feature,
                    "kept": False,
                    "reason": "insufficient_selection_rows",
                    "coverages": coverages,
                    "gains": gain_map,
                }
            )
            continue
        kept = all(g >= MIN_GAIN for g in gains)
        if kept:
            chosen.append(feature)
        decisions.append(
            {
                "feature": feature,
                "kept": kept,
                "reason": "improved_brier_on_all_selection_seasons" if kept else "failed_minimum_gain",
                "coverages": coverages,
                "gains": gain_map,
                "minimum_gain": MIN_GAIN,
            }
        )
    return chosen, decisions


def evaluate_families(
    frame: pd.DataFrame,
    league_id: str,
    features: list[str],
    *,
    years: tuple[int, ...] = REPORT_YEARS,
) -> dict[str, Any]:
    windows: list[dict[str, Any]] = []
    for year in years:
        train = frame[season_mask(frame, year, train=True)]
        test = frame[season_mask(frame, year, train=False)]
        if len(train) < 300 or len(test) < 50:
            windows.append({"season": year, "status": "insufficient_rows", "train_n": int(len(train)), "test_n": int(len(test))})
            continue
        family_rows: dict[str, Any] = {}
        fits: list[FitResult] = []
        for name, fitter in FAMILY_FITTERS.items():
            result = fitter(train, test, league_id, features)
            fits.append(result)
            family_rows[name] = {
                "metrics": result.metrics,
                "by_position": result.by_position,
                "by_bucket": result.by_bucket,
                "features_used": result.features,
                "notes": result.notes,
            }
        ens = fit_ensemble(fits, test)
        family_rows["ensemble"] = {
            "metrics": ens.metrics,
            "by_position": ens.by_position,
            "by_bucket": ens.by_bucket,
            "features_used": ens.features,
            "notes": ens.notes,
        }
        # Pick primary by Brier (research candidate selection on this window).
        ranked = sorted(
            ((n, body["metrics"]["brier"]) for n, body in family_rows.items() if body.get("metrics")),
            key=lambda t: t[1],
        )
        windows.append(
            {
                "season": year,
                "status": "ok",
                "held_out_style": "chronological_not_final",
                "train_n": int(len(train)),
                "test_n": int(len(test)),
                "families": family_rows,
                "best_by_brier": ranked[0][0] if ranked else None,
            }
        )
    return {"windows": windows}


def evaluate_position_models(frame: pd.DataFrame, league_id: str) -> dict[str, Any]:
    """Fit hierarchical_opp separately within each position."""
    out: dict[str, Any] = {}
    fit = FAMILY_FITTERS["hierarchical_opp"]
    for pos in POSITIONS:
        feats = features_for_position(pos, league_id)
        sub = frame[frame["position_group"] == pos].copy()
        pos_windows: list[dict[str, Any]] = []
        for year in REPORT_YEARS:
            train = sub[season_mask(sub, year, train=True)]
            test = sub[season_mask(sub, year, train=False)]
            if len(train) < 150 or len(test) < 40:
                pos_windows.append({"season": year, "status": "insufficient_rows", "train_n": int(len(train)), "test_n": int(len(test))})
                continue
            usable = select_usable_features(train, feats)
            result = fit(train, test, league_id, usable)
            pos_windows.append(
                {
                    "season": year,
                    "status": "ok",
                    "metrics": result.metrics,
                    "features_used": result.features,
                    "by_bucket": result.by_bucket,
                }
            )
        out[pos] = {"feature_set": feats, "windows": pos_windows, "note": position_catalog(league_id)[pos]["note"]}
    return out


def refit_m5_baseline(frame: pd.DataFrame, league_id: str, features: list[str] | None = None) -> dict[str, Any]:
    """Same-window hierarchical baseline for fair M5-style comparison on this frame."""
    from spe.models.rolling_td import TD_FEATURES_FBS, TD_FEATURES_NFL

    feats = features if features is not None else list(TD_FEATURES_NFL if league_id == "nfl" else TD_FEATURES_FBS)
    return evaluate_families(frame, league_id, feats, years=REPORT_YEARS)


def fit_final_artifact(
    frame: pd.DataFrame,
    league_id: str,
    features: list[str],
    family: str,
    train_end: datetime,
) -> dict[str, Any]:
    kick = pd.to_datetime(frame["kickoff_utc"], utc=True)
    shadow_start = kick.max() - pd.Timedelta(days=45)
    train = frame[kick < shadow_start].copy()
    test = frame[kick >= shadow_start].copy()
    if len(train) < 500:
        cut = int(len(frame) * 0.85)
        train = frame.iloc[:cut].copy()
        test = frame.iloc[cut:].copy()
    fitter = FAMILY_FITTERS.get(family, FAMILY_FITTERS["hierarchical_opp"])
    result = fitter(train, test, league_id, features)
    # Optional position-specific Platt on hierarchical predictions.
    y_te = (test["anytime_tds"].fillna(0).to_numpy(dtype=float) > 0).astype(float)
    p = result.p_test.copy()
    platt_by_pos: dict[str, list[float]] = {}
    if family == "hierarchical_opp" and len(train) and len(test):
        # Recompute train predictions roughly via same fitter extras when present.
        pass
    artifact = {
        "league_id": league_id,
        "version": VERSION,
        "family": family,
        "market_id": "anytime_td",
        "train_end": train_end.isoformat(),
        "frozen_at": datetime.now(UTC).isoformat(),
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "features": result.features or features,
        "candidate_features": features,
        "shadow_metrics": result.metrics,
        "by_position": result.by_position,
        "by_bucket": result.by_bucket,
        "extras": {k: v for k, v in (result.extras or {}).items() if k != "coef" or True},
        "notes": [
            "player_td_research_candidate",
            "pass_td_excluded_from_anytime_label",
            "does_not_overwrite_m3_m4_m5_or_team_td",
            *result.notes,
        ],
        "integration": integration_requirements(),
        "platt_by_pos": platt_by_pos,
    }
    # Store shrinkage rates for serving research predictions later.
    y = train["anytime_tds"].fillna(0)
    artifact["league_rate"] = float(y.mean()) if len(train) else 0.15
    artifact["pos_rates"] = {str(k): float(v) for k, v in y.groupby(train["position_group"]).mean().items()}
    artifact["team_rates"] = {str(k): float(v) for k, v in y.groupby(train["team_id"].fillna("")).mean().items() if k}
    player = y.groupby(train["player_id"]).agg(["mean", "count"])
    artifact["player_rates"] = {str(k): float(v) for k, v in player["mean"].to_dict().items()}
    artifact["player_n"] = {str(k): int(v) for k, v in player["count"].to_dict().items()}
    path = artifact_path(league_id)
    path.write_text(json.dumps(artifact, indent=2, default=str), encoding="utf-8")
    return {
        "artifact_path": str(path),
        "shadow": result.metrics,
        "family": family,
        "features": artifact["features"],
        "test_n": int(len(test)),
        "train_n": int(len(train)),
        "y_te_brier_check": brier_score(y_te, p) if len(y_te) == len(p) else None,
    }


def _choose_family(eval_block: dict[str, Any]) -> str:
    """Majority vote across report windows by Brier; default hierarchical_opp."""
    votes: dict[str, int] = {}
    for w in eval_block.get("windows") or []:
        best = w.get("best_by_brier")
        if best:
            votes[best] = votes.get(best, 0) + 1
    if not votes:
        return "hierarchical_opp"
    return sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def run_league_study(session: Session, league_id: str) -> dict[str, Any]:
    print(f"[player_td] building frame {league_id}…", flush=True)
    frame, meta = build_opportunity_study_frame(session, league_id, year_max=2026, universe="corrected")
    cut = training_cutoff(session)["leagues"][league_id]
    train_end = datetime.fromisoformat(cut["train_cutoff"]) if cut.get("train_cutoff") else datetime.now(UTC)
    if not frame.empty:
        frame = filter_completed_frame(frame.assign(status="final"), train_end)
    print(f"[player_td] {league_id} rows={len(frame)} td_rate={meta.get('td_rate')}", flush=True)

    candidates = all_candidate_features(league_id)
    coverage = feature_coverage(frame, candidates)
    print(f"[player_td] ablating {league_id}…", flush=True)
    kept, decisions = ablating_features(frame, league_id, candidates)
    print(f"[player_td] {league_id} kept {kept}", flush=True)

    print(f"[player_td] evaluating families {league_id}…", flush=True)
    families = evaluate_families(frame, league_id, kept)
    m5_style = refit_m5_baseline(frame, league_id)
    print(f"[player_td] position models {league_id}…", flush=True)
    positions = evaluate_position_models(frame, league_id)
    family = _choose_family(families)
    final = fit_final_artifact(frame, league_id, kept, family, train_end)

    # Coverage summary by season.
    kick = pd.to_datetime(frame["kickoff_utc"], utc=True) if len(frame) else pd.Series(dtype="datetime64[ns, UTC]")
    coverage_seasons = {
        "n_rows": int(len(frame)),
        "n_2024": int((kick >= pd.Timestamp("2024-08-01", tz="UTC")).sum()) if len(frame) else 0,
        "n_2025": int((kick >= pd.Timestamp("2025-08-01", tz="UTC")).sum()) if len(frame) else 0,
        "n_2026": int((kick >= pd.Timestamp("2026-08-01", tz="UTC")).sum()) if len(frame) else 0,
        "players": int(frame["player_id"].nunique()) if len(frame) else 0,
        "td_rate": meta.get("td_rate"),
        "universe": meta.get("universe"),
    }

    return {
        "league_id": league_id,
        "train_cutoff": cut,
        "frame_meta": meta,
        "coverage": coverage_seasons,
        "feature_coverage": coverage,
        "features_tested": candidates,
        "features_kept": kept,
        "ablation": decisions,
        "families": families,
        "m5_style_refit_on_same_frame": m5_style,
        "position_specific": positions,
        "position_catalog": position_catalog(league_id),
        "selected_family": family,
        "final_artifact": final,
    }


def run_player_td_study(*, persist_report: bool = True) -> dict[str, Any]:
    before = snapshot_protected_artifacts()
    session = _session()
    try:
        leagues = [run_league_study(session, lg) for lg in ("nfl", "fbs")]
        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "safety": safety_banner(),
            "baselines_frozen": load_baseline_metrics(),
            "selection_years": list(SELECTION_YEARS),
            "report_years": list(REPORT_YEARS),
            "label_policy": "anytime_td := rush_td + rec_td > 0; pass_td never counts",
            "leagues": leagues,
            "integration_requirements": integration_requirements(),
            "remaining_missing_data": [
                "FBS red-zone / goal-line / true target-share (not invented)",
                "FBS 2026 player_game_stats gap (owned by other agent / ingest)",
                "Verified individual WR–CB matchups (not invented)",
                "Complete injury → opportunity causal features for FBS",
                "Agent 1 live team_scoring adapter not connected (by design)",
            ],
            "play_enabled": False,
            "promoted": False,
            "research_only": True,
        }
        # Summaries for the final report sections.
        report["summary"] = _summarize(report)
        if persist_report:
            path = report_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
            comparison_path().write_text(json.dumps(report["summary"], indent=2, default=str), encoding="utf-8")
            report["report_path"] = str(path)
        assert_protected_unchanged(before)
        return report
    finally:
        session.close()


def _summarize(report: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"by_league": {}}
    for block in report.get("leagues") or []:
        lid = block["league_id"]
        improved = [d["feature"] for d in block.get("ablation") or [] if d.get("kept")]
        rejected = [d["feature"] for d in block.get("ablation") or [] if not d.get("kept")]
        family_scores = []
        for w in (block.get("families") or {}).get("windows") or []:
            if w.get("status") != "ok":
                continue
            row = {"season": w["season"], "best": w.get("best_by_brier")}
            for name, body in (w.get("families") or {}).items():
                m = body.get("metrics") or {}
                row[name] = {k: m.get(k) for k in ("brier", "log_loss", "ece", "n")}
            family_scores.append(row)
        pos_perf = {}
        for pos, body in (block.get("position_specific") or {}).items():
            pos_perf[pos] = [
                {"season": w.get("season"), "status": w.get("status"), "metrics": w.get("metrics")}
                for w in body.get("windows") or []
            ]
        out["by_league"][lid] = {
            "features_improved": improved,
            "features_rejected_count": len(rejected),
            "selected_family": block.get("selected_family"),
            "final_shadow": (block.get("final_artifact") or {}).get("shadow"),
            "coverage": block.get("coverage"),
            "family_windows": family_scores,
            "position_performance": pos_perf,
            "artifact": (block.get("final_artifact") or {}).get("artifact_path"),
        }
    out["integration_requirements"] = report.get("integration_requirements")
    out["safety"] = report.get("safety")
    return out
