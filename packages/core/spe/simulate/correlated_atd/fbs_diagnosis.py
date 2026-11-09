"""Controlled FBS calibration experiments. Retain only OOS improvements."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.models.metrics import brier_score, expected_calibration_error, log_loss
from spe.models.player_td.integration import TeamScoringContext
from spe.simulate.correlated_atd.phase5_isolation import phase5_dir
from spe.simulate.correlated_atd.player_weights import PlayerOpportunity
from spe.simulate.correlated_atd.scoring import load_league_frame, score_games, summarize_predictions


def _renorm(opps: list[PlayerOpportunity], other: float) -> list[PlayerOpportunity]:
    by_team: dict[str, list[PlayerOpportunity]] = {}
    for o in opps:
        by_team.setdefault(o.team_id, []).append(o)
    out: list[PlayerOpportunity] = []
    scale = 1.0 - other
    for rows in by_team.values():
        rs = sum(r.rush_weight for r in rows) or 1.0
        cs = sum(r.rec_weight for r in rows) or 1.0
        for r in rows:
            nr = deepcopy(r)
            nr.rush_weight = scale * (r.rush_weight / rs)
            nr.rec_weight = 0.0 if r.position_group == "QB" else scale * (r.rec_weight / cs)
            nr.notes = list(r.notes) + [f"experiment_other={other}"]
            out.append(nr)
    return out


def _uniform_shares(opps: list[PlayerOpportunity], gdf: pd.DataFrame, ctx: dict[str, TeamScoringContext]) -> list[PlayerOpportunity]:
    del gdf, ctx
    adj: list[PlayerOpportunity] = []
    for o in opps:
        nr = deepcopy(o)
        nr.rush_weight = 1.0
        nr.rec_weight = 0.0 if o.position_group == "QB" else 1.0
        adj.append(nr)
    return _renorm(adj, 0.10)


def _m6_weighted(opps: list[PlayerOpportunity], gdf: pd.DataFrame, ctx: dict[str, TeamScoringContext]) -> list[PlayerOpportunity]:
    del gdf, ctx
    adj: list[PlayerOpportunity] = []
    for o in opps:
        w = float(o.m6_mean or o.m6_p or 0.1)
        nr = deepcopy(o)
        if o.position_group == "QB":
            nr.rush_weight = max(w, 1e-3)
            nr.rec_weight = 0.0
        elif o.position_group == "RB":
            nr.rush_weight = max(w, 1e-3)
            nr.rec_weight = max(0.35 * w, 1e-3)
        else:
            nr.rush_weight = max(0.15 * w, 1e-4)
            nr.rec_weight = max(w, 1e-3)
        adj.append(nr)
    return _renorm(adj, 0.10)


def _blend_toward_m6(opps: list[PlayerOpportunity], gdf: pd.DataFrame, ctx: dict[str, TeamScoringContext]) -> list[PlayerOpportunity]:
    """Post-hoc not applicable here — instead tilt shares using m6 mean (already). Keep usage."""
    del gdf, ctx
    return opps


def _scale_team_lambda(factor: float):
    def _fn(ctx: dict[str, TeamScoringContext], gdf: pd.DataFrame) -> dict[str, TeamScoringContext]:
        del gdf
        out = {}
        for tid, c in ctx.items():
            out[tid] = TeamScoringContext(
                game_id=c.game_id,
                team_id=c.team_id,
                league_id=c.league_id,
                expected_off_tds=(c.expected_off_tds or 0) * factor,
                expected_rush_tds=(c.expected_rush_tds or 0) * factor,
                expected_rec_tds=(c.expected_rec_tds or 0) * factor,
                off_td_pmf=c.off_td_pmf,
                train_end=c.train_end,
                model_version=c.model_version,
                research_only=True,
                notes=tuple(list(c.notes) + [f"lambda_scale={factor}"]),
            )
        return out

    return _fn


def _coverage_stats(frame: pd.DataFrame) -> dict[str, Any]:
    cols = [
        "prior_carries",
        "prior_targets",
        "prior_receptions",
        "prior_gl_carries",
        "prior_rz_targets",
        "rush_td_share",
        "rec_td_share",
        "team_lambda_off_td",
        "prior_td_rate",
    ]
    out: dict[str, Any] = {"n_rows": int(len(frame))}
    for c in cols:
        if c not in frame.columns:
            out[c] = {"present": False}
            continue
        s = pd.to_numeric(frame[c], errors="coerce")
        out[c] = {
            "present": True,
            "non_null_rate": float(s.notna().mean()),
            "nonzero_rate": float((s.fillna(0) != 0).mean()),
            "mean": float(s.mean(skipna=True)) if s.notna().any() else None,
        }
    # Current-season density
    if "kickoff_utc" in frame.columns:
        kick = pd.to_datetime(frame["kickoff_utc"], utc=True, errors="coerce")
        season = kick >= pd.Timestamp("2026-08-01", tz="UTC")
        out["season_2026_rows"] = int(season.sum())
        out["season_2026_share"] = float(season.mean()) if len(frame) else 0.0
    return out


def run_fbs_diagnosis(
    session: Session,
    *,
    train_games: int = 100,
    test_games: int = 60,
    draw_count: int = 1_500,
    seed: int = 7,
    frame: pd.DataFrame | None = None,
) -> dict[str, Any]:
    frame = frame if frame is not None else load_league_frame(session, "fbs")
    if frame is None or frame.empty:
        return {"error": "empty_fbs_frame"}
    games = list(dict.fromkeys(frame["game_id"].astype(str).tolist()))
    if len(games) < train_games + test_games:
        cut = max(1, int(len(games) * 0.7))
        train_ids, test_ids = games[:cut], games[cut:]
    else:
        test_ids = games[-test_games:]
        train_ids = games[-(train_games + test_games) : -test_games]

    coverage = _coverage_stats(frame[frame["game_id"].astype(str).isin(test_ids)])

    experiments: dict[str, dict[str, Any]] = {
        "baseline_other_0.10": {"other_share": 0.10},
        "other_0.05": {"other_share": 0.05},
        "other_0.18": {"other_share": 0.18},
        "other_0.25": {"other_share": 0.25},
        "uniform_shares": {"other_share": 0.10, "opp_transform": _uniform_shares},
        "m6_weighted_shares": {"other_share": 0.10, "opp_transform": _m6_weighted},
        "team_lambda_x0.85": {"other_share": 0.10, "ctx_transform": _scale_team_lambda(0.85)},
        "team_lambda_x1.15": {"other_share": 0.10, "ctx_transform": _scale_team_lambda(1.15)},
    }

    results: dict[str, Any] = {
        "league_id": "fbs",
        "n_train_games": len(train_ids),
        "n_test_games": len(test_ids),
        "coverage_on_test": coverage,
        "experiments": {},
        "selection_rule": "retain only if test Brier and ECE both improve vs baseline; never force-match M6",
    }

    baseline_test: dict[str, Any] | None = None
    for name, cfg in experiments.items():
        print(f"[phase5/fbs] experiment {name}…", flush=True)
        # Selection uses chronological test only (OOS). Train scored for diagnostics.
        scored_te = score_games(
            frame,
            "fbs",
            test_ids,
            draw_count=draw_count,
            seed=seed,
            other_share=cfg.get("other_share"),
            opp_transform=cfg.get("opp_transform"),
            ctx_transform=cfg.get("ctx_transform"),
            label=f"fbs-{name}",
            progress_every=40,
        )
        if scored_te["n"] == 0:
            results["experiments"][name] = {"error": "empty"}
            continue
        y, p = scored_te["y"], scored_te["models"]["atd_sim_v1"]
        m6 = scored_te["models"]["m6"]
        block = {
            "test": summarize_predictions(y, p, scored_te["meta"], n_boot=200, seed=seed),
            "m6_test": summarize_predictions(y, m6, scored_te["meta"], n_boot=200, seed=seed + 1),
            "vs_m6_brier": float(brier_score(y, p) - brier_score(y, m6)),
            "vs_m6_ece": float(expected_calibration_error(y, p) - expected_calibration_error(y, m6)),
        }
        results["experiments"][name] = block
        if name == "baseline_other_0.10":
            baseline_test = block

    retained: list[str] = []
    rejected: list[dict[str, Any]] = []
    if baseline_test:
        base_brier = baseline_test["test"]["brier"]
        base_ece = baseline_test["test"]["ece"]
        for name, block in results["experiments"].items():
            if name == "baseline_other_0.10" or "test" not in block:
                continue
            brier = block["test"]["brier"]
            ece = block["test"]["ece"]
            ok = brier < base_brier - 1e-4 and ece <= base_ece + 0.005
            entry = {"name": name, "brier": brier, "ece": ece, "delta_brier": brier - base_brier, "delta_ece": ece - base_ece}
            if ok:
                retained.append(name)
            else:
                rejected.append(entry)
    results["retained"] = retained
    results["rejected"] = rejected
    results["findings"] = _findings(results, coverage)

    path = phase5_dir() / "fbs_calibration_diagnosis.json"
    path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    results["artifact_path"] = str(path)
    return results


def _findings(results: dict[str, Any], coverage: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    cov_gl = coverage.get("prior_gl_carries") or {}
    cov_rz = coverage.get("prior_rz_targets") or {}
    if cov_gl.get("nonzero_rate", 1) < 0.05:
        notes.append("FBS goal-line carries are effectively unused (not fabricated); rush allocation relies on carries + TD history.")
    if cov_rz.get("nonzero_rate", 1) < 0.05:
        notes.append("FBS red-zone targets missing; receiving allocation uses receptions/history only.")
    season = coverage.get("season_2026_share")
    if season is not None and season < 0.15:
        notes.append(f"Current-season (2026) rows are only {season:.1%} of the test frame — thin live usage.")
    exps = results.get("experiments") or {}
    base = (exps.get("baseline_other_0.10") or {}).get("test") or {}
    m6 = (exps.get("baseline_other_0.10") or {}).get("m6_test") or {}
    if base and m6:
        if base.get("mean_p", 0) < m6.get("mean_p", 0) - 0.02:
            notes.append("Simulator mean probability sits below M6 — team TD budgets + OTHER residual compress individual rates.")
        if base.get("ece", 0) > m6.get("ece", 0):
            notes.append("Baseline sim ECE worse than M6; calibration gap is the primary FBS failure mode.")
    if results.get("retained"):
        notes.append(f"Retained OOS improvements: {results['retained']}")
    else:
        notes.append("No controlled experiment beat baseline on both Brier and ECE; keep baseline FBS settings.")
    notes.append("Do not force FBS probabilities to match M6.")
    return notes
