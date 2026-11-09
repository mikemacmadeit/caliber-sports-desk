"""Serialize / load M6 HistGradientBoosting for research inference.

Does NOT replace the served shrink-rate ``*_m6_player_td.json`` artifact.
Writes isolated research files under ``models/player_td/``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.config import settings
from spe.models.metrics import apply_platt, platt_calibrate
from spe.models.player_td.candidates import select_usable_features
from spe.models.player_td.isolation import snapshot_protected_artifacts, assert_protected_unchanged
from spe.models.temporal_integrity import training_cutoff
from spe.research.atd_features import build_canonical_opportunity_frame, feature_matrix, opportunity_feature_candidates

VERSION = "m6_hist_gbm_v1"
PLAY_ENABLED = False
PROMOTED = False
RESEARCH_ONLY = True

GBM_CONFIG = {
    "max_depth": 4,
    "learning_rate": 0.05,
    "max_iter": 120,
    "l2_regularization": 0.1,
    "early_stopping": True,
    "validation_fraction": 0.1,
    "random_state": 7,
}


def artifact_dir() -> Path:
    path = Path(settings.parquet_dir) / "models" / "player_td"
    path.mkdir(parents=True, exist_ok=True)
    return path


def meta_path(league_id: str) -> Path:
    return artifact_dir() / f"{league_id}_{VERSION}.json"


def model_path(league_id: str) -> Path:
    return artifact_dir() / f"{league_id}_{VERSION}.joblib"


def served_m6_path(league_id: str) -> Path:
    """Existing shrink-rate artifact — never overwritten by this module."""
    return artifact_dir() / f"{league_id}_m6_player_td.json"


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _chronological_shadow_split(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    kick = pd.to_datetime(frame["kickoff_utc"], utc=True)
    shadow_start = kick.max() - pd.Timedelta(days=45)
    train = frame[kick < shadow_start].copy()
    test = frame[kick >= shadow_start].copy()
    if len(train) < 400:
        cut = int(len(frame) * 0.85)
        train = frame.iloc[:cut].copy()
        test = frame.iloc[cut:].copy()
    return train, test


def train_and_serialize(
    session: Session,
    league_id: str,
    *,
    now: datetime | None = None,
    feature_list: list[str] | None = None,
) -> dict[str, Any]:
    """Fit HistGBM, write joblib+meta, verify reload. Leaves served M6 JSON untouched."""
    moment = now or datetime.now(UTC)
    before = snapshot_protected_artifacts()
    served_before = served_m6_path(league_id).read_bytes() if served_m6_path(league_id).exists() else None

    cut = training_cutoff(session, now=moment)["leagues"].get(league_id, {})
    train_end = datetime.fromisoformat(cut["train_cutoff"]) if cut.get("train_cutoff") else moment

    frame, frame_meta = build_canonical_opportunity_frame(session, league_id, year_max=2026)
    if frame.empty:
        return {"status": "no_data", "league_id": league_id}

    kick = pd.to_datetime(frame["kickoff_utc"], utc=True)
    usable = frame.loc[kick < pd.Timestamp(train_end)].copy()
    train, test = _chronological_shadow_split(usable)
    candidates = feature_list or opportunity_feature_candidates(league_id)
    # Prefer features listed on the existing study artifact when present.
    study = served_m6_path(league_id)
    if study.exists() and feature_list is None:
        try:
            art = json.loads(study.read_text(encoding="utf-8"))
            if art.get("features"):
                candidates = list(art["features"])
        except Exception:
            pass
    features = select_usable_features(train, candidates, min_coverage=0.15)
    if len(train) < 400 or not features:
        return {
            "status": "insufficient",
            "league_id": league_id,
            "train_n": int(len(train)),
            "features": features,
        }

    try:
        from sklearn.ensemble import HistGradientBoostingClassifier
        import joblib
    except ImportError as exc:
        return {"status": "sklearn_unavailable", "error": str(exc)}

    y_tr = (train["anytime_tds"].fillna(0).to_numpy(dtype=float) > 0).astype(int)
    y_te = (test["anytime_tds"].fillna(0).to_numpy(dtype=float) > 0).astype(float)
    x_tr, feats, _ = feature_matrix(train, features, strict=False)
    x_te, _, _ = feature_matrix(test, feats, strict=False)

    model = HistGradientBoostingClassifier(**GBM_CONFIG)
    model.fit(x_tr, y_tr)
    p_tr = model.predict_proba(x_tr)[:, 1]
    p_te_raw = model.predict_proba(x_te)[:, 1]
    a, b = platt_calibrate(p_tr, y_tr.astype(float))
    p_te = apply_platt(p_te_raw, a, b)

    # Persist model blob then meta with hash.
    mpath = model_path(league_id)
    joblib.dump(
        {
            "model": model,
            "features": feats,
            "platt": [float(a), float(b)],
            "config": GBM_CONFIG,
            "version": VERSION,
            "league_id": league_id,
        },
        mpath,
    )
    blob_hash = _file_sha256(mpath)
    meta = {
        "league_id": league_id,
        "version": VERSION,
        "family": "hist_gbm",
        "market_id": "anytime_td",
        "train_end": train_end.isoformat(),
        "frozen_at": moment.isoformat(),
        "play_enabled": PLAY_ENABLED,
        "promoted": PROMOTED,
        "research_only": RESEARCH_ONLY,
        "does_not_replace_served_m6_shrink": True,
        "served_m6_path": str(served_m6_path(league_id)),
        "model_path": str(mpath),
        "model_sha256": blob_hash,
        "feature_schema": feats,
        "candidate_features": candidates,
        "training_config": GBM_CONFIG,
        "calibration": {"platt_a": float(a), "platt_b": float(b), "method": "platt_global_train_only"},
        "train_n": int(len(train)),
        "test_n": int(len(test)),
        "shadow_metrics": {
            "n": float(len(y_te)),
            "brier": float(np.mean((p_te - y_te) ** 2)) if len(y_te) else None,
            "mean_p": float(p_te.mean()) if len(p_te) else None,
            "base_rate": float(y_te.mean()) if len(y_te) else None,
        },
        "frame_meta": {k: frame_meta.get(k) for k in ("universe", "rows", "players", "td_rate", "feature_pipeline")},
        "notes": [
            "research_serialized_hist_gbm",
            "served_shrink_m6_untouched",
            "native_nan_support",
        ],
    }
    meta_path(league_id).write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")

    verify = verify_reload(league_id, test.head(min(200, len(test))))
    assert_protected_unchanged(before)
    served_after = served_m6_path(league_id).read_bytes() if served_m6_path(league_id).exists() else None
    if served_before != served_after:
        raise RuntimeError("Served M6 shrink artifact changed during hist_gbm serialization")

    return {
        "status": "ok",
        "league_id": league_id,
        "meta_path": str(meta_path(league_id)),
        "model_path": str(mpath),
        "model_sha256": blob_hash,
        "features": feats,
        "train_n": int(len(train)),
        "test_n": int(len(test)),
        "shadow_brier": meta["shadow_metrics"]["brier"],
        "reload_verify": verify,
        "served_m6_untouched": True,
        "protected_unchanged": True,
    }


def load_m6_hist_gbm(league_id: str) -> dict[str, Any] | None:
    mpath = model_path(league_id)
    meta_file = meta_path(league_id)
    if not mpath.exists() or not meta_file.exists():
        return None
    import joblib

    blob = joblib.load(mpath)
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    expected = meta.get("model_sha256")
    actual = _file_sha256(mpath)
    if expected and actual and expected != actual:
        raise RuntimeError(f"M6 hist_gbm hash mismatch for {league_id}: {actual} != {expected}")
    return {"meta": meta, "blob": blob, "model_sha256": actual}


def predict_proba_frame(league_id: str, frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Return (p_calibrated, valid_mask, info). Invalid rows get NaN (never silently filled)."""
    loaded = load_m6_hist_gbm(league_id)
    if loaded is None:
        n = len(frame)
        return np.full(n, np.nan), np.zeros(n, dtype=bool), {"status": "missing_artifact"}
    blob = loaded["blob"]
    model = blob["model"]
    feats = list(blob["features"])
    a, b = blob["platt"]
    x, present, valid = feature_matrix(frame, feats, strict=False, align_schema=True)
    if not present:
        n = len(frame)
        return np.full(n, np.nan), np.zeros(n, dtype=bool), {"status": "no_features"}
    raw = model.predict_proba(x)[:, 1]
    p = apply_platt(raw, float(a), float(b))
    out = np.where(valid, p, np.nan)
    missing_cols = [c for c in feats if c not in frame.columns]
    return out, valid, {
        "status": "ok",
        "features": present,
        "missing_columns": missing_cols,
        "model_sha256": loaded["model_sha256"],
        "version": VERSION,
        "n_valid": int(valid.sum()),
        "n_rejected": int((~valid).sum()),
    }


def verify_reload(league_id: str, frame: pd.DataFrame) -> dict[str, Any]:
    """Confirm saved and reloaded models produce the same probabilities."""
    loaded = load_m6_hist_gbm(league_id)
    if loaded is None or frame.empty:
        return {"ok": False, "reason": "missing_or_empty"}
    import joblib

    blob = loaded["blob"]
    model = blob["model"]
    feats = list(blob["features"])
    a, b = blob["platt"]
    x, _, _ = feature_matrix(frame, feats, strict=False, align_schema=True)
    p1 = apply_platt(model.predict_proba(x)[:, 1], float(a), float(b))
    # Reload from disk again
    blob2 = joblib.load(model_path(league_id))
    p2 = apply_platt(blob2["model"].predict_proba(x)[:, 1], float(blob2["platt"][0]), float(blob2["platt"][1]))
    max_abs = float(np.nanmax(np.abs(p1 - p2))) if len(p1) else 0.0
    return {
        "ok": max_abs < 1e-12,
        "max_abs_diff": max_abs,
        "n": int(len(p1)),
        "model_sha256": loaded["model_sha256"],
        "features": feats,
        "train_end": loaded["meta"].get("train_end"),
        "calibration": loaded["meta"].get("calibration"),
    }
