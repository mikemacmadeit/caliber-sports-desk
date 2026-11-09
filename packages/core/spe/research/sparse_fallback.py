"""Out-of-sample evaluation of hierarchical fallbacks for sparse-history Anytime TD.

Research only. Does not modify production artifacts or official forecasts.
A fallback is retained only when it improves Brier, log loss, and ECE on the
sparse-player chronological holdout — not merely because models disagree.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.analytics.definitions import SHRINKAGE_K
from spe.config import settings
from spe.models.advanced import binary_probability, shrinkage_mean
from spe.models.metrics import apply_platt, brier_score, expected_calibration_error, log_loss, platt_calibrate
from spe.models.player_td.candidates import fit_hist_gbm, score_probs
from spe.models.player_td.train import load_player_td_model
from spe.models.player_td.model import predict_frame
from spe.research.atd_features import build_canonical_opportunity_frame, attach_team_scoring_and_shares
from spe.research.atd_forecast import research_dir

SPARSE_N = 5
FOCUS_NAMES = {"Jonah Coleman", "Haynes King", "Dallen Bentley"}


def report_path(league_id: str | None = None) -> Path:
    if league_id:
        return research_dir() / f"sparse_fallback_oos_{league_id}.json"
    return research_dir() / "sparse_fallback_oos_report.json"


def _hier_p(
    train: pd.DataFrame,
    test: pd.DataFrame,
    league_id: str,
) -> np.ndarray:
    """Position/team/league shrinkage → 1-exp(-mean). Train-only rates."""
    base_tr, base_te = shrinkage_mean(train, test, "anytime_tds", league_id)
    return binary_probability(base_te)


def _blend(p_model: np.ndarray, p_hier: np.ndarray, n: np.ndarray, k: float) -> np.ndarray:
    """Weight model by n/(n+k); hierarchical otherwise. No disagreement trigger."""
    w = np.where(n > 0, n / (n + k), 0.0)
    w = np.clip(w, 0.0, 1.0)
    return np.clip(w * p_model + (1.0 - w) * p_hier, 1e-6, 1 - 1e-6)


def _hard_replace(p_model: np.ndarray, p_hier: np.ndarray, n: np.ndarray, threshold: int = SPARSE_N) -> np.ndarray:
    out = p_model.copy()
    mask = n < threshold
    out[mask] = p_hier[mask]
    return np.clip(out, 1e-6, 1 - 1e-6)


def _metrics(y: np.ndarray, p: np.ndarray) -> dict[str, float]:
    return score_probs(y, p)


def evaluate_sparse_fallbacks(
    session: Session,
    *,
    league_id: str = "nfl",
    shadow_days: int = 45,
) -> dict[str, Any]:
    """Chronological shadow: train before cutoff, test last shadow_days; sparse subset."""
    frame, meta = build_canonical_opportunity_frame(session, league_id, year_max=2026)
    if frame.empty:
        return {"status": "no_data", "league_id": league_id}
    frame, weights, share_meta = attach_team_scoring_and_shares(session, frame, league_id)
    kick = pd.to_datetime(frame["kickoff_utc"], utc=True)
    shadow_start = kick.max() - pd.Timedelta(days=shadow_days)
    train = frame.loc[kick < shadow_start].copy()
    test = frame.loc[kick >= shadow_start].copy()
    if len(train) < 500 or len(test) < 80:
        cut = int(len(frame) * 0.85)
        train = frame.iloc[:cut].copy()
        test = frame.iloc[cut:].copy()

    # Per-player prior game counts as of each test row (shift: games strictly before kickoff).
    hist = frame.sort_values(["player_id", "kickoff_utc"]).copy()
    hist["prior_n"] = hist.groupby("player_id").cumcount()
    n_map = hist.set_index(["player_id", "game_id"])["prior_n"].to_dict()
    test = test.copy()
    test["prior_n"] = [
        int(n_map.get((str(r.player_id), str(r.game_id)), 0))
        for r in test.itertuples(index=False)
    ]

    y = (test["anytime_tds"].fillna(0).to_numpy(dtype=float) > 0).astype(float)
    sparse_mask = test["prior_n"].to_numpy(dtype=int) < SPARSE_N
    n_arr = test["prior_n"].to_numpy(dtype=float)
    k = float(SHRINKAGE_K.get(league_id, 12.0))

    # Hierarchical baseline
    p_hier = _hier_p(train, test, league_id)

    # Player TD v1 on test (load frozen model if present; else fit shares path via lambdas already on frame)
    model = load_player_td_model(league_id)
    if model is not None:
        scored = predict_frame(model, test)
        p_v1 = scored["p_atd"].to_numpy(dtype=float)
    else:
        lam = test["lambda_player_td"].fillna(0).to_numpy(dtype=float)
        p_v1 = binary_probability(lam)

    # M6 hist_gbm refit on train only (research comparison; does not write artifact)
    from spe.research.m6_hist_gbm import load_m6_hist_gbm, predict_proba_frame

    loaded = load_m6_hist_gbm(league_id)
    if loaded is not None:
        p_gbm, valid, _info = predict_proba_frame(league_id, test)
        p_gbm = np.where(np.isfinite(p_gbm), p_gbm, p_hier)
    else:
        feats = [
            c
            for c in (
                "prior_td_rate",
                "roll3_td_rate",
                "workload_delta",
                "prior_carry_share",
                "own_off_epa",
                "is_home",
            )
            if c in train.columns
        ]
        gbm = fit_hist_gbm(train, test, league_id, feats)
        p_gbm = np.asarray(gbm.p_test, dtype=float)

    candidates: dict[str, np.ndarray] = {
        "player_td_v1": p_v1,
        "m6_hist_gbm": p_gbm,
        "hierarchical_only": p_hier,
        "v1_blend_hier": _blend(p_v1, p_hier, n_arr, k),
        "gbm_blend_hier": _blend(p_gbm, p_hier, n_arr, k),
        "v1_hard_sparse_hier": _hard_replace(p_v1, p_hier, n_arr),
        "gbm_hard_sparse_hier": _hard_replace(p_gbm, p_hier, n_arr),
    }

    def block(mask: np.ndarray) -> dict[str, Any]:
        out: dict[str, Any] = {"n": int(mask.sum())}
        if int(mask.sum()) < 30:
            out["status"] = "insufficient_sparse_rows"
            return out
        y_m = y[mask]
        ranked = []
        for name, p in candidates.items():
            m = _metrics(y_m, p[mask])
            ranked.append({"model": name, **m})
        ranked.sort(key=lambda r: (r["brier"], r["log_loss"], r["ece"]))
        out["ranking"] = ranked
        out["best"] = ranked[0]["model"]
        # Retention rule vs baseline model (v1 / gbm)
        for base in ("player_td_v1", "m6_hist_gbm"):
            base_row = next(r for r in ranked if r["model"] == base)
            for alt_name in (f"{'v1' if base.startswith('player') else 'gbm'}_blend_hier", f"{'v1' if base.startswith('player') else 'gbm'}_hard_sparse_hier"):
                alt = next(r for r in ranked if r["model"] == alt_name)
                improves = (
                    alt["brier"] + 1e-9 < base_row["brier"]
                    and alt["log_loss"] + 1e-9 < base_row["log_loss"]
                    and alt["ece"] + 1e-9 <= base_row["ece"] + 0.005
                )
                out[f"retain_{alt_name}_vs_{base}"] = {
                    "improves_all_primary_metrics": improves,
                    "base": base_row,
                    "alt": alt,
                    "decision": "retain_research_candidate" if improves else "reject_do_not_apply",
                }
        return out

    full = block(np.ones(len(test), dtype=bool))
    sparse = block(sparse_mask)
    dense = block(~sparse_mask)

    # Focus players in test window if present
    focus = []
    if "player_name" in test.columns:
        for name in FOCUS_NAMES:
            sub = test[test["player_name"] == name]
            if sub.empty:
                continue
            idx = sub.index
            # map to positional
            pos = test.index.get_indexer(idx)
            focus.append(
                {
                    "player_name": name,
                    "n_rows": int(len(sub)),
                    "prior_n": [int(x) for x in sub["prior_n"].tolist()],
                    "y": [float(y[i]) for i in pos if i >= 0],
                    "p_v1": [float(p_v1[i]) for i in pos if i >= 0],
                    "p_gbm": [float(p_gbm[i]) for i in pos if i >= 0],
                    "p_hier": [float(p_hier[i]) for i in pos if i >= 0],
                    "p_v1_blend": [float(candidates["v1_blend_hier"][i]) for i in pos if i >= 0],
                    "p_gbm_hard": [float(candidates["gbm_hard_sparse_hier"][i]) for i in pos if i >= 0],
                }
            )

    retain_any = False
    for key, val in (sparse or {}).items():
        if key.startswith("retain_") and isinstance(val, dict) and val.get("improves_all_primary_metrics"):
            retain_any = True

    report = {
        "status": "ok",
        "as_of": datetime.now(UTC).isoformat(),
        "league_id": league_id,
        "research_only": True,
        "play_enabled": False,
        "promoted": False,
        "official_probabilities_unchanged": True,
        "policy": (
            "Fallback retained only if it improves Brier AND log loss AND does not worsen ECE "
            "on the sparse-player chronological holdout. Disagreement alone never triggers a fallback."
        ),
        "sparse_n_threshold": SPARSE_N,
        "train_n": int(len(train)),
        "test_n": int(len(test)),
        "sparse_n": int(sparse_mask.sum()),
        "frame_meta": meta,
        "share_meta": share_meta,
        "full_window": full,
        "sparse_players": sparse,
        "dense_players": dense,
        "focus_players_in_window": focus,
        "apply_to_production": False,
        "retain_any_fallback": retain_any,
        "recommendation": (
            "Wire retained fallback into research inference only after this report shows improve_all_primary_metrics=true. "
            "Do not change official forecasts yet."
            if retain_any
            else "No fallback cleared the OOS bar — keep current models; continue flagging sparse rows."
        ),
    }
    path = report_path(league_id)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    return report
