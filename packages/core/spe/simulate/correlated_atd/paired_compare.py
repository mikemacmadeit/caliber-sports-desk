"""Fair paired model comparison with game-clustered bootstrap CIs.

Uses identical players/games/eligibility/outcomes. Does not invent new models.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.models.metrics import brier_score, expected_calibration_error, log_loss
from spe.simulate.correlated_atd.proof_isolation import proof_dir
from spe.simulate.correlated_atd.sample_audit import LABEL_HISTORICAL_REPLAY
from spe.simulate.correlated_atd.scoring import load_league_frame, score_games, summarize_predictions


def game_clustered_paired_delta(
    y: np.ndarray,
    p_a: np.ndarray,
    p_b: np.ndarray,
    game_ids: np.ndarray,
    *,
    metric: str = "brier",
    n_boot: int = 800,
    seed: int = 0,
) -> dict[str, float]:
    """Paired Δ = metric(A) - metric(B). Negative means A better for Brier/log_loss.

    Bootstrap resamples games (clusters), keeping all player-rows within selected games.
    """
    games = np.unique(game_ids)
    rng = np.random.default_rng(seed)

    def _metric(yy: np.ndarray, pp: np.ndarray) -> float:
        if metric == "brier":
            return brier_score(yy, pp)
        if metric == "log_loss":
            return log_loss(yy, pp)
        if metric == "ece":
            return expected_calibration_error(yy, pp)
        raise ValueError(metric)

    point = float(_metric(y, p_a) - _metric(y, p_b))
    samples = np.empty(n_boot, dtype=float)
    # Pre-index rows by game for speed.
    index: dict[str, np.ndarray] = {}
    for g in games:
        index[str(g)] = np.flatnonzero(game_ids == g)

    for i in range(n_boot):
        draw = rng.choice(games, size=len(games), replace=True)
        idx = np.concatenate([index[str(g)] for g in draw]) if len(draw) else np.array([], dtype=int)
        if len(idx) == 0:
            samples[i] = np.nan
            continue
        samples[i] = _metric(y[idx], p_a[idx]) - _metric(y[idx], p_b[idx])

    samples = samples[np.isfinite(samples)]
    return {
        "delta": point,
        "lo": float(np.quantile(samples, 0.025)) if len(samples) else float("nan"),
        "hi": float(np.quantile(samples, 0.975)) if len(samples) else float("nan"),
        "n_boot": float(len(samples)),
        "n_games": float(len(games)),
        "metric": metric,
        "sign_note": "For brier/log_loss/ece: delta<0 means model A better than B",
        "excludes_zero": bool(len(samples) and (np.quantile(samples, 0.025) > 0 or np.quantile(samples, 0.975) < 0)),
    }


def _rank_models(block: dict[str, Any]) -> list[dict[str, Any]]:
    models = block.get("models") or {}
    ranked = sorted(
        (
            {
                "model": name,
                "brier": body["brier"],
                "log_loss": body["log_loss"],
                "ece": body["ece"],
                "mean_p": body["mean_p"],
            }
            for name, body in models.items()
        ),
        key=lambda r: (r["brier"], r["log_loss"]),
    )
    for i, row in enumerate(ranked, start=1):
        row["rank"] = i
    return ranked


def compare_league(
    session: Session,
    league_id: str,
    game_ids: list[str],
    *,
    sample_label: str,
    evidence_class: str,
    draw_count: int = 1_200,
    n_boot: int = 800,
    seed: int = 42,
    frame: pd.DataFrame | None = None,
) -> dict[str, Any]:
    frame = frame if frame is not None else load_league_frame(session, league_id)
    if frame is None or frame.empty or not game_ids:
        return {"league_id": league_id, "error": "empty", "sample_label": sample_label}

    print(f"[proof] {league_id} paired compare n_games={len(game_ids)} label={sample_label}…", flush=True)
    scored = score_games(
        frame,
        league_id,
        game_ids,
        draw_count=draw_count,
        seed=seed,
        label=f"proof-{league_id}",
    )
    if scored["n"] == 0:
        return {"league_id": league_id, "error": "no_rows", "sample_label": sample_label}

    y = scored["y"]
    meta = scored["meta"]
    gids = meta["game_id"].astype(str).to_numpy()
    models = scored["models"]

    out: dict[str, Any] = {
        "league_id": league_id,
        "sample_label": sample_label,
        "evidence_class": evidence_class,
        "evidence_warning": (
            "Not live prospective validation."
            if evidence_class == LABEL_HISTORICAL_REPLAY
            else None
        ),
        "n": scored["n"],
        "n_games": scored["n_games"],
        "base_rate": float(np.mean(y)),
        "models": {
            name: summarize_predictions(y, p, meta, n_boot=min(400, n_boot), seed=seed + i)
            for i, (name, p) in enumerate(models.items())
        },
        "ranking_by_brier": [],
        "paired_deltas": {},
        "joint": None,
    }
    out["ranking_by_brier"] = _rank_models(out)

    pairs = [
        ("atd_sim_v1", "m6"),
        ("atd_sim_v1", "m5"),
        ("m6", "m5"),
        ("m6", "m3"),
        ("m5", "m3"),
        ("atd_sim_v1", "m3"),
    ]
    for a, b in pairs:
        if a not in models or b not in models:
            continue
        key = f"{a}__minus__{b}"
        out["paired_deltas"][key] = {
            "brier": game_clustered_paired_delta(y, models[a], models[b], gids, metric="brier", n_boot=n_boot, seed=seed),
            "log_loss": game_clustered_paired_delta(
                y, models[a], models[b], gids, metric="log_loss", n_boot=n_boot, seed=seed + 1
            ),
            "ece": game_clustered_paired_delta(y, models[a], models[b], gids, metric="ece", n_boot=n_boot, seed=seed + 2),
        }

    # Joint paired: sim vs independence on same pairs
    joints = scored.get("joint_rows") or []
    if joints:
        jy = np.array([r["y"] for r in joints], dtype=float)
        jp_sim = np.array([r["p_sim"] for r in joints], dtype=float)
        jp_ind = np.array([r["p_indep"] for r in joints], dtype=float)
        jg = np.array([r["game_id"] for r in joints], dtype=str)
        out["joint"] = {
            "n_pairs": len(joints),
            "base_rate": float(jy.mean()),
            "sim_brier": brier_score(jy, jp_sim),
            "indep_brier": brier_score(jy, jp_ind),
            "sim_log_loss": log_loss(jy, jp_sim),
            "indep_log_loss": log_loss(jy, jp_ind),
            "paired_brier_sim_minus_indep": game_clustered_paired_delta(
                jy, jp_sim, jp_ind, jg, metric="brier", n_boot=n_boot, seed=seed + 9
            ),
            "note": "Separate from individual ATD. Do not assume simulator improves parlays.",
        }

    path = proof_dir() / f"{league_id}_{sample_label}_paired.json"
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    out["artifact_path"] = str(path)
    return out


def league_selection(nfl: dict[str, Any], fbs: dict[str, Any]) -> dict[str, Any]:
    """Recommend league-specific research leaders without forcing one architecture."""

    def _leader(block: dict[str, Any]) -> dict[str, Any]:
        ranks = block.get("ranking_by_brier") or []
        if not ranks:
            return {"leader": None, "reason": "no_ranking"}
        top = ranks[0]["model"]
        deltas = block.get("paired_deltas") or {}
        notes = []
        if top == "atd_sim_v1":
            d = (deltas.get("atd_sim_v1__minus__m6") or {}).get("brier") or {}
            if d.get("excludes_zero"):
                notes.append("paired Brier CI vs M6 excludes 0")
            else:
                notes.append("point edge vs M6 but paired CI includes 0 — not yet decisive")
        if top == "m5":
            notes.append("M5 leads on Brier in this sample; preserve as FBS research candidate unless overturned")
        return {
            "leader": top,
            "ranking": ranks,
            "notes": notes,
            "evidence_class": block.get("evidence_class"),
            "promote": False,
        }

    return {
        "nfl": _leader(nfl),
        "fbs": _leader(fbs),
        "policy": "League-specific model selection is allowed. Newest ≠ best. Do not promote.",
        "fbs_guidance": "Preserve M5 as leading FBS research candidate unless stronger evidence supports another model.",
        "nfl_guidance": "Evaluate whether atd_sim_v1 repeatably beats M6 on paired game-clustered tests.",
    }
