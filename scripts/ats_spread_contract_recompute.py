"""Recompute NFL ATS holdout under the proven nflverse L contract.

Does NOT retune ridge weights, overwrite model_nfl.json, rewrite freezes,
or touch Anytime TD / Validated Play.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "research" / "ats_audit"
EXPORT = OUT / "nfl_holdout_export.csv"
RAW_CSV = OUT / "nflverse_games_raw.csv"
ARTIFACT = ROOT / "data" / "parquet" / "models" / "ats" / "model_nfl.json"
OLD_METRICS = OUT / "metrics_reproduction.json"

HOLDOUT_START = pd.Timestamp("2024-09-01", tz="UTC")


def _brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def _log_loss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _ece(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    total = 0.0
    n = len(y)
    for i in range(bins):
        mask = (p >= edges[i]) & (p < edges[i + 1] if i < bins - 1 else p <= edges[i + 1])
        if not np.any(mask):
            continue
        total += abs(float(y[mask].mean()) - float(p[mask].mean())) * (mask.sum() / n)
    return float(total)


def _normal_cover_p(mu: float, h: float, sigma: float) -> dict[str, float]:
    from math import erf, sqrt

    sigma = max(float(sigma), 1.0)
    threshold = -float(h)
    z = (threshold - mu) / (sigma * sqrt(2.0))
    p_home_strict = 0.5 * (1.0 - erf(z))
    line_is_whole = abs(h - round(h)) < 1e-9
    if line_is_whole:
        half = 0.5 / (sigma * sqrt(2.0))
        z0 = (0.0 - (mu + h)) / (sigma * sqrt(2.0))
        p_push = float(np.clip(0.5 * (erf(z0 + half) - erf(z0 - half)), 0.0, 0.15))
        remainder = max(0.0, 1.0 - p_push)
        p_home = float(np.clip(p_home_strict, 1e-4, 1 - 1e-4))
        p_away_strict = 1.0 - p_home
        total = p_home + p_away_strict
        p_home = remainder * (p_home / total)
        p_away = remainder * (p_away_strict / total)
    else:
        p_push = 0.0
        p_home = float(np.clip(p_home_strict, 1e-4, 1 - 1e-4))
        p_away = 1.0 - p_home
    return {"p_home_cover": p_home, "p_away_cover": p_away, "p_push": p_push}


def _apply_platt(p: np.ndarray, a: float, b: float) -> np.ndarray:
    logits = np.log(np.clip(p, 1e-6, 1 - 1e-6) / np.clip(1 - p, 1e-6, 1 - 1e-6))
    z = a + b * logits
    return 1.0 / (1.0 + np.exp(-z))


def _fit_platt(p: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    # Simple 1-D Newton on logistic link; mirrors spe.models.metrics.platt_calibrate spirit.
    from spe.models.metrics import platt_calibrate

    return platt_calibrate(p, y)


def _american_implied(price: float) -> float:
    am = int(price)
    if am > 0:
        return 100.0 / (am + 100.0)
    return abs(am) / (abs(am) + 100.0)


def grade_correct(m: float, L: float) -> str:
    edge = m - L
    if abs(edge) < 1e-12:
        return "push"
    return "home_cover" if edge > 0 else "away_cover"


def grade_wrong(m: float, L: float) -> str:
    """Previous mistaken convention: treated L as sportsbook handicap."""
    edge = m + L
    if abs(edge) < 1e-12:
        return "push"
    return "home_cover" if edge > 0 else "away_cover"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    export = pd.read_csv(EXPORT)
    assert len(export) == 692, f"expected 692 holdout rows, got {len(export)}"

    raw_sha = hashlib.sha256(RAW_CSV.read_bytes()).hexdigest() if RAW_CSV.exists() else None
    art = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    old = json.loads(OLD_METRICS.read_text(encoding="utf-8")) if OLD_METRICS.exists() else {}

    # Existing margin predictions stay fixed; only orientation of L changes.
    rows = []
    label_changes = []
    push_changes = []
    for r in export.itertuples(index=False):
        L = float(r.spread_line)
        M = float(r.margin)
        h = -L
        old_label = grade_wrong(M, L)
        new_label = grade_correct(M, L)
        old_push = old_label == "push"
        new_push = new_label == "push"
        if old_label != new_label:
            label_changes.append(
                {
                    "game_id": r.game_id,
                    "margin": M,
                    "L": L,
                    "h": h,
                    "old_label": old_label,
                    "new_label": new_label,
                }
            )
        if old_push != new_push:
            push_changes.append(
                {
                    "game_id": r.game_id,
                    "margin": M,
                    "L": L,
                    "old_push": old_push,
                    "new_push": new_push,
                }
            )
        probs = _normal_cover_p(float(r.predicted_margin), h, float(r.margin_sigma))
        rows.append(
            {
                "game_id": r.game_id,
                "margin": M,
                "spread_line_L": L,
                "home_handicap_h": h,
                "predicted_margin": float(r.predicted_margin),
                "margin_sigma": float(r.margin_sigma),
                "home_spread_price": r.home_spread_price,
                "away_spread_price": r.away_spread_price,
                "old_label": old_label,
                "new_label": new_label,
                "old_y_home": None if old_push else (1.0 if old_label == "home_cover" else 0.0),
                "new_y_home": None if new_push else (1.0 if new_label == "home_cover" else 0.0),
                "p_home_raw_corrected": probs["p_home_cover"],
                "p_away_raw_corrected": probs["p_away_cover"],
                "p_push_corrected": probs["p_push"],
                "p_home_old_stored": float(r.p_home_cover),
                "p_home_old_raw": float(r.p_home_cover_raw),
                "model_vs_market_corrected": float(r.predicted_margin) - L,
                "model_vs_market_old_wrong": float(r.predicted_margin) - (-L),
                "close_abs_err_vs_L": abs(M - L),
                "close_abs_err_vs_neg_L": abs(M - (-L)),
            }
        )

    scored = pd.DataFrame(rows)

    # Matched non-push sets under each convention.
    old_graded = scored[scored["old_y_home"].notna()].copy()
    new_graded = scored[scored["new_y_home"].notna()].copy()
    both = scored[scored["old_y_home"].notna() & scored["new_y_home"].notna()].copy()

    # Old calibrated metrics on old labels (reproduce prior claim).
    old_y = old_graded["old_y_home"].to_numpy(dtype=float)
    old_p_cal = old_graded["p_home_old_stored"].to_numpy(dtype=float)
    old_p_raw = old_graded["p_home_old_raw"].to_numpy(dtype=float)

    # Corrected raw probs on corrected labels (legacy Platt invalidated).
    new_y = new_graded["new_y_home"].to_numpy(dtype=float)
    new_p_raw = new_graded["p_home_raw_corrected"].to_numpy(dtype=float)

    # Research-only corrected Platt on TRAINING partition only (pre-holdout).
    # Rebuild train cover targets from export is impossible (export is holdout-only),
    # so fit Platt on a synthetic train proxy is forbidden. Instead: load DB features
    # for pre-holdout games if available; otherwise fit Platt on nothing and leave
    # uncalibrated, documenting the invalidation.
    research_platt = _fit_research_platt_train_only(art)
    if research_platt is not None:
        a, b, train_n = research_platt
        new_p_cal = _apply_platt(new_p_raw, a, b)
        platt_meta = {
            "status": "fitted_train_only",
            "platt_a": a,
            "platt_b": b,
            "train_n": train_n,
            "artifact_path": str(OUT / "model_nfl_spread_contract_research.json"),
            "note": "Separate research artifact. Did not overwrite model_nfl.json.",
        }
        # Write research artifact copy with corrected platt; ridge coefs unchanged.
        research_art = dict(art)
        research_art["platt_a"] = a
        research_art["platt_b"] = b
        research_art["model_version_id"] = art.get("model_version_id", "") + "-spread-contract-research"
        research_art["metrics"] = {
            **(art.get("metrics") or {}),
            "spread_contract": "nflverse_L_v1",
            "platt_fit_n": train_n,
            "research_only": True,
            "invalidates_legacy_platt": True,
        }
        (OUT / "model_nfl_spread_contract_research.json").write_text(
            json.dumps(research_art, indent=2), encoding="utf-8"
        )
    else:
        new_p_cal = new_p_raw
        a = b = None
        platt_meta = {
            "status": "unfit_or_unavailable",
            "note": "Legacy Platt invalidated; corrected train fit unavailable in this run. Reporting raw probs.",
        }

    def pack(y, p, label):
        return {
            "label": label,
            "n": int(len(y)),
            "brier": _brier(y, p) if len(y) else None,
            "log_loss": _log_loss(y, p) if len(y) else None,
            "ece": _ece(y, p) if len(y) else None,
            "accuracy_at_50": float(np.mean((p >= 0.5) == (y >= 0.5))) if len(y) else None,
            "base_rate": float(np.mean(y)) if len(y) else None,
            "mean_p": float(np.mean(p)) if len(y) else None,
        }

    # Market baseline on corrected labels with real ATS prices only.
    mkt_y, mkt_p, mkt_skip = [], [], 0
    for r in new_graded.itertuples(index=False):
        price = r.home_spread_price
        if price is None or (isinstance(price, float) and math.isnan(price)):
            mkt_skip += 1
            continue
        mkt_y.append(float(r.new_y_home))
        mkt_p.append(_american_implied(float(price)))
    market = pack(np.asarray(mkt_y), np.asarray(mkt_p), "market_ats_price_corrected_labels")
    market["skipped_no_price"] = mkt_skip
    market["note"] = "Uses nflverse home_spread_odds only. No invented -110. Consensus non-actionable."

    baseline_50 = pack(new_y, np.full_like(new_y, 0.5), "constant_50pct_diagnostic_baseline")

    close_mae_correct = float(scored["close_abs_err_vs_L"].mean())
    close_mae_wrong = float(scored["close_abs_err_vs_neg_L"].mean())
    model_mae = float(np.mean(np.abs(scored["margin"] - scored["predicted_margin"])))

    # Matched-game comparison: same game_ids under both non-push filters.
    matched_ids = set(both["game_id"])
    old_on_matched = both
    # For matched rows, compare old stored p vs old labels AND corrected p vs new labels.
    matched_old = pack(
        old_on_matched["old_y_home"].to_numpy(dtype=float),
        old_on_matched["p_home_old_stored"].to_numpy(dtype=float),
        "old_calibrated_on_old_labels_matched",
    )
    matched_new_raw = pack(
        old_on_matched["new_y_home"].to_numpy(dtype=float),
        old_on_matched["p_home_raw_corrected"].to_numpy(dtype=float),
        "corrected_raw_on_new_labels_matched",
    )
    if research_platt is not None:
        matched_new_cal = pack(
            old_on_matched["new_y_home"].to_numpy(dtype=float),
            _apply_platt(old_on_matched["p_home_raw_corrected"].to_numpy(dtype=float), a, b),
            "corrected_research_platt_on_new_labels_matched",
        )
    else:
        matched_new_cal = None

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_contract": {
            "raw_csv": str(RAW_CSV),
            "raw_sha256": raw_sha,
            "docs": "Positive spread_line ⇒ home favored; L = expected home margin; h = -L",
            "home_covers": "M - L > 0",
            "storage": "closing_lines.home_line stores raw L (unchanged)",
            "defect": "Downstream graded/prob'd L as if it were sportsbook handicap h",
        },
        "sample": {
            "n_holdout_export": int(len(export)),
            "n_old_push": int((scored["old_label"] == "push").sum()),
            "n_new_push": int((scored["new_label"] == "push").sum()),
            "n_old_graded": int(len(old_graded)),
            "n_new_graded": int(len(new_graded)),
            "n_label_changes": int(len(label_changes)),
            "n_push_classification_changes": int(len(push_changes)),
            "n_matched_both_non_push": int(len(both)),
            "do_not_assume_685": True,
        },
        "label_change_examples": label_changes[:25],
        "push_change_examples": push_changes[:25],
        "old_convention_reproduction": {
            "calibrated": pack(old_y, old_p_cal, "old_calibrated_wrong_convention"),
            "raw": pack(old_y, old_p_raw, "old_raw_wrong_convention"),
            "prior_reported_brier": old.get("cover_brier_calibrated", {}).get("brier"),
            "note": "Reproducing 0.1568 under the mistaken convention is not validation.",
        },
        "corrected": {
            "raw_uncalibrated": pack(new_y, new_p_raw, "corrected_raw"),
            "research_platt": pack(new_y, new_p_cal, "corrected_research_platt_or_raw"),
            "baseline_50pct": baseline_50,
            "market_ats_prices": market,
            "closing_line_margin_mae": {
                "vs_L_correct": close_mae_correct,
                "vs_neg_L_wrong": close_mae_wrong,
                "model_margin_mae_unchanged": model_mae,
                "n": int(len(scored)),
            },
            "legacy_platt_status": "INVALIDATED — fitted on inverted cover targets",
            "research_platt_meta": platt_meta,
        },
        "matched_comparison": {
            "n": int(len(both)),
            "old": matched_old,
            "corrected_raw": matched_new_raw,
            "corrected_calibrated": matched_new_cal,
        },
        "advantage_assessment": None,  # filled below
        "preserved": {
            "atd_untouched": True,
            "validated_play_off": True,
            "ridge_weights_unchanged": True,
            "model_nfl_json_not_overwritten": True,
            "original_freezes_not_rewritten": True,
        },
    }

    corr_brier = report["corrected"]["research_platt"]["brier"]
    mkt_brier = market["brier"]
    base_brier = baseline_50["brier"]
    old_brier = report["old_convention_reproduction"]["calibrated"]["brier"]
    survives = (
        corr_brier is not None
        and mkt_brier is not None
        and corr_brier < mkt_brier
        and corr_brier < base_brier
    )
    report["advantage_assessment"] = {
        "does_historical_ats_advantage_survive": bool(survives),
        "corrected_brier": corr_brier,
        "market_brier": mkt_brier,
        "baseline_50_brier": base_brier,
        "old_mistaken_brier": old_brier,
        "interpretation": (
            "Examined historical research result under corrected nflverse L contract — "
            "not a proven live edge."
            if survives
            else "Earlier advantage claim does not survive correct spread interpretation; retract."
        ),
    }

    scored.to_csv(OUT / "holdout_recompute_corrected.csv", index=False)
    (OUT / "spread_contract_recompute.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(report["advantage_assessment"], indent=2))
    print(
        json.dumps(
            {
                "n_label_changes": report["sample"]["n_label_changes"],
                "n_new_graded": report["sample"]["n_new_graded"],
                "n_old_graded": report["sample"]["n_old_graded"],
                "close_mae_L": close_mae_correct,
                "close_mae_neg_L": close_mae_wrong,
                "corrected_brier": corr_brier,
                "old_brier": old_brier,
            },
            indent=2,
        )
    )


def _fit_research_platt_train_only(art: dict) -> tuple[float, float, int] | None:
    """Fit corrected Platt on pre-holdout games only; never on examined holdout."""
    import os
    import sys

    sys.path.insert(0, str(ROOT / "packages" / "core"))
    os.chdir(ROOT)
    try:
        from spe.db.session import session_factory
        from spe.models.ats.model import AtsModelArtifact, predict_margin
        from spe.models.ats.ratings import build_team_strength_frame
        from spe.models.ats.spread_contract import attach_spread_fields
        from spe.models.ats.labels import normal_cover_probs
        from spe.models.metrics import platt_calibrate
    except Exception as exc:  # noqa: BLE001
        print(f"train platt import failed: {exc}")
        return None

    try:
        with session_factory() as session:
            frame = build_team_strength_frame(session, "nfl")
        if frame.empty:
            return None
        frame["kickoff_utc"] = pd.to_datetime(frame["kickoff_utc"], utc=True)
        train = frame[frame["kickoff_utc"] < HOLDOUT_START].dropna(subset=["margin", "spread_line"]).copy()
        if len(train) < 40:
            return None
        # Use existing ridge coefs from artifact (no retune).
        artifact = AtsModelArtifact.from_dict(art)
        train = attach_spread_fields(train)
        margins = predict_margin(artifact, train)
        p_raw, y = [], []
        for mu, h, m in zip(margins, train["home_handicap"], train["margin"], strict=True):
            if h is None or (isinstance(h, float) and np.isnan(h)):
                continue
            probs = normal_cover_probs(float(mu), float(h), artifact.sigma)
            edge = float(m) + float(h)
            if abs(edge) < 1e-9:
                continue
            p_raw.append(probs["p_home_cover"])
            y.append(1.0 if edge > 0 else 0.0)
        if len(y) < 40:
            return None
        a, b = platt_calibrate(np.asarray(p_raw), np.asarray(y))
        return float(a), float(b), int(len(y))
    except Exception as exc:  # noqa: BLE001
        print(f"train platt fit failed: {exc}")
        return None


if __name__ == "__main__":
    main()
