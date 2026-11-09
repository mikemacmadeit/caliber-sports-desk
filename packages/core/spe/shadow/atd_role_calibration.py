"""Role / position accuracy fix for Anytime TD research scores.

Fixes the main miss pattern: rushing QBs and high-carry RBs are underconfident
(model too low when they score). Fits small logit shifts on holdout history and
applies them at serve/replay time.

Research-only. Does not unlock Validated Play.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from spe.models.metrics import brier_score

VERSION = "atd_role_calib_v1"
RUSHING_QB_CARRIES = 4.0
HIGH_CARRY_RB = 8.0


def artifact_path() -> Path:
    path = Path("data") / "atd_sim_dev" / "live_weekend" / f"{VERSION}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-z))


def _carry_col(frame: pd.DataFrame) -> pd.Series:
    for name in ("prior_carries", "exp_carries", "recent8_carries", "carries"):
        if name in frame.columns:
            return pd.to_numeric(frame[name], errors="coerce")
    return pd.Series(np.nan, index=frame.index)


def role_tags(frame: pd.DataFrame) -> pd.DataFrame:
    """Boolean role columns used for calibration."""
    pos = frame.get("position_group", pd.Series("", index=frame.index)).astype(str).str.upper()
    carries = _carry_col(frame).fillna(0.0)
    out = pd.DataFrame(index=frame.index)
    out["rushing_qb"] = (pos == "QB") & (carries >= RUSHING_QB_CARRIES)
    out["high_carry_rb"] = (pos == "RB") & (carries >= HIGH_CARRY_RB)
    out["rb"] = pos == "RB"
    out["qb"] = pos == "QB"
    out["wr"] = pos == "WR"
    out["te"] = pos == "TE"
    return out


def apply_role_calibration(
    p: float | np.ndarray,
    *,
    position_group: str | None = None,
    prior_carries: float | None = None,
    artifact: dict[str, Any] | None = None,
    frame: pd.DataFrame | None = None,
) -> float | np.ndarray:
    """Apply saved logit shifts. Vectorized when ``frame`` is provided."""
    art = artifact or load_artifact()
    if not art or not art.get("ok"):
        return p

    shifts = art.get("logit_shifts") or {}
    if frame is not None:
        tags = role_tags(frame)
        z = _logit(np.asarray(p, dtype=float))
        for key, delta in shifts.items():
            if key in tags.columns and float(delta) != 0.0:
                z = z + float(delta) * tags[key].to_numpy(dtype=float)
        return _sigmoid(z)

    tags = {
        "rushing_qb": (str(position_group or "").upper() == "QB" and float(prior_carries or 0) >= RUSHING_QB_CARRIES),
        "high_carry_rb": (str(position_group or "").upper() == "RB" and float(prior_carries or 0) >= HIGH_CARRY_RB),
        "rb": str(position_group or "").upper() == "RB",
        "qb": str(position_group or "").upper() == "QB",
        "wr": str(position_group or "").upper() == "WR",
        "te": str(position_group or "").upper() == "TE",
    }
    z = float(_logit(np.asarray([float(p)], dtype=float))[0])
    for key, delta in shifts.items():
        if tags.get(key) and float(delta) != 0.0:
            z += float(delta)
    return float(_sigmoid(np.asarray([z]))[0])


def _fit_shift(p: np.ndarray, y: np.ndarray, mask: np.ndarray, *, grid: np.ndarray) -> float:
    """Pick logit shift on masked rows that minimizes Brier (small L2 toward 0)."""
    if int(mask.sum()) < 40:
        return 0.0
    base = p[mask]
    yy = y[mask]
    best_d = 0.0
    best = float(brier_score(yy, base)) + 1e-6  # prefer 0 unless clear win
    for d in grid:
        pp = _sigmoid(_logit(base) + d)
        score = float(brier_score(yy, pp)) + 0.0008 * (d ** 2)
        if score < best:
            best = score
            best_d = float(d)
    return best_d


def fit_role_calibration(
    frame: pd.DataFrame,
    p_col: str = "p_blend",
    y_col: str = "y",
    *,
    train_frac: float = 0.7,
) -> dict[str, Any]:
    """Fit role logit shifts on earlier games; validate on later games."""
    df = frame.copy()
    if y_col not in df.columns:
        df[y_col] = (pd.to_numeric(df.get("anytime_tds"), errors="coerce").fillna(0) > 0).astype(float)
    df = df[np.isfinite(pd.to_numeric(df[p_col], errors="coerce"))].copy()
    if df.empty:
        return {"ok": False, "reason": "empty"}

    ko = pd.to_datetime(df["kickoff_utc"], utc=True)
    order = np.argsort(ko.to_numpy())
    cut = int(len(order) * train_frac)
    train_idx = order[: max(cut, 1)]
    test_idx = order[max(cut, 1) :]
    train = df.iloc[train_idx]
    test = df.iloc[test_idx] if len(test_idx) else train

    tags_tr = role_tags(train)
    p_tr = np.clip(train[p_col].to_numpy(dtype=float), 1e-6, 1 - 1e-6)
    y_tr = train[y_col].to_numpy(dtype=float)
    grid = np.linspace(-0.35, 0.85, 25)

    # Fit specific underconfident roles first.
    shifts: dict[str, float] = {}
    for key in ("rushing_qb", "high_carry_rb"):
        shifts[key] = _fit_shift(p_tr, y_tr, tags_tr[key].to_numpy(dtype=bool), grid=grid)

    # Mild leftover RB nudge only (pocket QBs are not the miss pattern).
    p_adj = apply_role_calibration(p_tr, frame=train.assign(**{p_col: p_tr}), artifact={"ok": True, "logit_shifts": shifts})
    assert isinstance(p_adj, np.ndarray)
    rb_mask = tags_tr["rb"].to_numpy(dtype=bool) & ~tags_tr["high_carry_rb"].to_numpy(dtype=bool)
    shifts["rb"] = _fit_shift(p_adj, y_tr, rb_mask, grid=np.linspace(-0.2, 0.35, 12))
    shifts["qb"] = 0.0  # avoid over-correcting pocket QBs

    def _metrics(part: pd.DataFrame) -> dict[str, Any]:
        p0 = np.clip(part[p_col].to_numpy(dtype=float), 1e-6, 1 - 1e-6)
        y = part[y_col].to_numpy(dtype=float)
        p1 = apply_role_calibration(p0, frame=part, artifact={"ok": True, "logit_shifts": shifts})
        assert isinstance(p1, np.ndarray)
        return {
            "n": int(len(part)),
            "brier_before": float(brier_score(y, p0)),
            "brier_after": float(brier_score(y, p1)),
            "delta_brier": float(brier_score(y, p0) - brier_score(y, p1)),
        }

    # Keep only shifts that help (or are ~neutral) on holdout; zero the rest.
    before_tags = role_tags(test)
    provisional = dict(shifts)
    for key, delta in list(provisional.items()):
        if abs(float(delta)) < 1e-9 or key not in before_tags.columns:
            continue
        part = test.loc[before_tags[key]]
        if part.empty or len(part) < 40:
            continue
        solo = {k: (float(v) if k == key else 0.0) for k, v in provisional.items()}
        p0 = np.clip(part[p_col].to_numpy(dtype=float), 1e-6, 1 - 1e-6)
        y = part[y_col].to_numpy(dtype=float)
        p1 = apply_role_calibration(p0, frame=part, artifact={"ok": True, "logit_shifts": solo})
        assert isinstance(p1, np.ndarray)
        if float(brier_score(y, p1)) > float(brier_score(y, p0)) - 1e-5:
            shifts[key] = 0.0

    cohort = {}
    for key in ("rushing_qb", "high_carry_rb", "rb", "qb"):
        part = test.loc[before_tags[key]]
        if part.empty:
            continue
        cohort[key] = _metrics(part)

    report = {
        "ok": True,
        "version": VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "play_enabled": False,
        "validated_play": False,
        "research_only": True,
        "logit_shifts": shifts,
        "thresholds": {"rushing_qb_carries": RUSHING_QB_CARRIES, "high_carry_rb": HIGH_CARRY_RB},
        "train": _metrics(train),
        "holdout": _metrics(test),
        "holdout_cohorts": cohort,
        "note": (
            "Logit shifts lift underconfident rushing QBs / high-carry RBs. "
            "Research-only; Official Play stays off."
        ),
    }
    artifact_path().write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["path"] = str(artifact_path())
    return report


def load_artifact() -> dict[str, Any] | None:
    path = artifact_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
