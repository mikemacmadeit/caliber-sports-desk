"""Phase 2 rolling Anytime Touchdown models (M5).

M3 and M4 artifacts are never overwritten. New forecasts stay research-only.
Validated Play stays off. FBS opportunity inventing is forbidden.
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
from spe.db.base import as_utc, new_id
from spe.db.models import BacktestRun, ModelHealthSnapshot, ModelVersion
from spe.models.advanced import (
    apply_beta,
    binary_probability,
    by_position,
    fit_log_beta,
    load_model_frame,
    opponent_adjustment,
    score_block,
    shrinkage_mean,
)
from spe.models.metrics import apply_platt, platt_calibrate
from spe.models.temporal_integrity import filter_completed_frame, training_cutoff

VERSION = "m5"
MARKET_ID = "anytime_td"
SKILL = {"WR", "TE", "RB", "QB"}
# Chronological evaluation windows — not treated as untouched final holdouts.
EVAL_YEARS = (2024, 2025, 2026)
# Features used when coverage clears the floor. FBS RZ/GL stay NaN by design.
TD_FEATURES_NFL = [
    "prior_td_rate",
    "roll3_td_rate",
    "workload_delta",
    "prior_target_share",
    "prior_carry_share",
    "prior_rz_targets",
    "prior_gl_carries",
    "own_off_epa",
    "opp_adj_def",
    "team_rz_td",
    "team_points_per_opp",
    "snap_roll3",
    "usage_x_defense",
    "is_home",
]
TD_FEATURES_FBS = [
    "prior_td_rate",
    "roll3_td_rate",
    "workload_delta",
    "prior_carry_share",
    "own_off_epa",
    "opp_adj_def",
    "team_rz_td",
    "team_points_per_opp",
    "usage_x_defense",
    "is_home",
]
MIN_FEATURE_COVERAGE = 0.25


def artifact_path(league_id: str) -> Path:
    return Path(settings.parquet_dir) / "models" / f"{league_id}_{VERSION}.json"


def m3_path(league_id: str) -> Path:
    return Path(settings.parquet_dir) / "models" / f"{league_id}_m3.json"


def m4_path(league_id: str) -> Path:
    return Path(settings.parquet_dir) / "models" / f"{league_id}_m4.json"


def comparison_path() -> Path:
    return Path(settings.parquet_dir) / "models" / "m5_comparison.json"


def _add_td_recency(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.sort_values(["player_id", "kickoff_utc"]).copy()
    if "anytime_tds" not in out.columns:
        out["anytime_tds"] = out["rush_td"].fillna(0) + out["rec_td"].fillna(0)
    group = out.groupby("player_id", sort=False)["anytime_tds"]
    out["roll3_td_rate"] = group.transform(lambda s: s.shift(1).rolling(3, min_periods=1).mean())
    # Expanding prior TD rate already exists as prior_td_rate from add_player_priors;
    # keep an alias that is always prior-only.
    if "prior_td_rate" not in out.columns:
        out["prior_td_rate"] = group.transform(lambda s: s.shift(1).expanding(min_periods=1).mean())
    return out


def prepare_td_frame(session: Session, league_id: str, *, year_max: int = 2026) -> pd.DataFrame:
    """M5 training frame via the canonical opportunity feature pipeline (no invented FBS RZ/GL)."""
    from spe.research.atd_features import build_canonical_opportunity_frame

    frame, _meta = build_canonical_opportunity_frame(
        session, league_id, year_max=year_max, universe="involved"
    )
    if frame.empty:
        # Fallback for environments where the research facade is unavailable mid-migrate.
        frame = load_model_frame(session, league_id, year_max=year_max, universe="involved")
        if frame.empty:
            return frame
        frame = _add_td_recency(frame)
        frame = frame[frame["position_group"].isin(SKILL)].copy()
        if league_id == "fbs":
            for col in ("prior_rz_targets", "prior_gl_carries", "prior_target_share", "snap_roll3"):
                if col in frame.columns:
                    frame[col] = np.nan
    return frame.reset_index(drop=True)


def _feature_list(league_id: str) -> list[str]:
    return list(TD_FEATURES_NFL if league_id == "nfl" else TD_FEATURES_FBS)


def _coverage(series: pd.Series) -> float:
    arr = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
    if len(arr) == 0:
        return 0.0
    return float(np.isfinite(arr).mean())


def _z_fit(train: pd.Series) -> tuple[float, float]:
    observed = pd.to_numeric(train, errors="coerce")
    observed = observed[np.isfinite(observed.to_numpy(dtype=float))]
    if observed.empty:
        return 0.0, 1.0
    mu = float(observed.mean())
    sd = float(observed.std(ddof=0))
    return mu, 1.0 if sd < 1e-8 else sd


def _z_apply(values: pd.Series, mu: float, sd: float) -> np.ndarray:
    raw = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
    z = (raw - mu) / sd
    return np.where(np.isfinite(z), z, 0.0)


def _season_mask(frame: pd.DataFrame, year: int, *, train: bool) -> pd.Series:
    kick = pd.to_datetime(frame["kickoff_utc"], utc=True)
    start = pd.Timestamp(f"{year}-08-01", tz="UTC")
    end = pd.Timestamp(f"{year + 1}-03-01", tz="UTC")
    if train:
        return kick < start
    return (kick >= start) & (kick < end)


def _select_features(train: pd.DataFrame, league_id: str) -> list[str]:
    kept: list[str] = []
    for name in _feature_list(league_id):
        if name not in train.columns:
            continue
        if _coverage(train[name]) < MIN_FEATURE_COVERAGE:
            continue
        kept.append(name)
    return kept


def _fit_td_split(
    train: pd.DataFrame,
    test: pd.DataFrame,
    league_id: str,
    features: list[str] | None = None,
) -> dict[str, Any] | None:
    if len(train) < 200 or len(test) < 40:
        return None
    train = train.copy()
    test = test.copy()
    if "opp_def_z" not in train.columns:
        train["opp_def_z"] = 0.0
        test["opp_def_z"] = 0.0
    features = features if features is not None else _select_features(train, league_id)
    base_tr, base_te = shrinkage_mean(train, test, "anytime_tds", league_id)
    m3_tr, m3_te, opp_beta = opponent_adjustment(train, test, base_tr, base_te, "anytime_tds")
    y_tr = train["anytime_tds"].fillna(0).to_numpy(dtype=float)
    y_te = test["anytime_tds"].fillna(0).to_numpy(dtype=float)

    z_meta: dict[str, dict[str, float]] = {}
    betas: dict[str, float] = {}
    new_tr, new_te = m3_tr.copy(), m3_te.copy()
    for name in features:
        mu, sd = _z_fit(train[name])
        z_meta[name] = {"mean": mu, "std": sd}
        x_tr = _z_apply(train[name], mu, sd)
        x_te = _z_apply(test[name], mu, sd)
        beta = fit_log_beta(new_tr, x_tr, y_tr)
        betas[name] = beta
        if beta == 0.0:
            continue
        new_tr = apply_beta(new_tr, x_tr, beta)
        new_te = apply_beta(new_te, x_te, beta)

    p_m3_raw = binary_probability(m3_tr)
    p_m5_raw = binary_probability(new_tr)
    y_bin_tr = (y_tr > 0).astype(float)
    a_m3, b_m3 = platt_calibrate(p_m3_raw, y_bin_tr)
    a_m5, b_m5 = platt_calibrate(p_m5_raw, y_bin_tr)
    p_m3 = apply_platt(binary_probability(m3_te), a_m3, b_m3)
    p_m5 = apply_platt(binary_probability(new_te), a_m5, b_m5)

    # Position-specific Platt on M5 (train only).
    platt_by_pos: dict[str, list[float]] = {}
    p_m5_pos = p_m5.copy()
    for pos in sorted(SKILL):
        mask_tr = train["position_group"].to_numpy() == pos
        mask_te = test["position_group"].to_numpy() == pos
        if int(mask_tr.sum()) < 80 or int(mask_te.sum()) < 15:
            continue
        a, b = platt_calibrate(binary_probability(new_tr[mask_tr]), y_bin_tr[mask_tr])
        platt_by_pos[pos] = [float(a), float(b)]
        p_m5_pos[mask_te] = apply_platt(binary_probability(new_te[mask_te]), a, b)

    return {
        "features": [n for n, beta in betas.items() if beta != 0.0],
        "feature_betas": betas,
        "feature_z": z_meta,
        "opp_beta": float(opp_beta),
        "platt_global": [float(a_m5), float(b_m5)],
        "platt_by_pos": platt_by_pos,
        "m3_refit": score_block(y_te, m3_te, p_m3, binary=True, line=0.5),
        "m5": score_block(y_te, new_te, p_m5_pos, binary=True, line=0.5),
        "m3_by_position": by_position(test, y_te, m3_te, p_m3, binary=True, line=0.5),
        "m5_by_position": by_position(test, y_te, new_te, p_m5_pos, binary=True, line=0.5),
        "train_n": int(len(train)),
        "test_n": int(len(test)),
    }


def _player_rates(train: pd.DataFrame, league_id: str) -> dict[str, Any]:
    y = train["anytime_tds"].fillna(0)
    league = float(y.mean()) if len(train) else 0.15
    pos = y.groupby(train["position_group"]).mean().to_dict()
    team = y.groupby(train["team_id"].fillna("")).mean().to_dict()
    player = y.groupby(train["player_id"]).agg(["mean", "count"])
    return {
        "k_player": float(SHRINKAGE_K[league_id]),
        "league_rate": league,
        "pos_rates": {str(k): float(v) for k, v in pos.items()},
        "team_rates": {str(k): float(v) for k, v in team.items() if k},
        "player_rates": {str(k): float(v) for k, v in player["mean"].to_dict().items()},
        "player_n": {str(k): int(v) for k, v in player["count"].to_dict().items()},
    }


def train_league_anytime_td(
    session: Session,
    league_id: str,
    *,
    now: datetime | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Train rolling M5 Anytime TD. Leaves M3/M4 bytes unchanged."""
    moment = as_utc(now) or datetime.now(UTC)
    m3_before = m3_path(league_id).read_bytes() if m3_path(league_id).exists() else None
    m4_before = m4_path(league_id).read_bytes() if m4_path(league_id).exists() else None

    cut = training_cutoff(session, now=moment)["leagues"][league_id]
    train_end = datetime.fromisoformat(cut["train_cutoff"]) if cut.get("train_cutoff") else moment
    frame = prepare_td_frame(session, league_id, year_max=2026)
    if frame.empty:
        return {"status": "no_stats", "league_id": league_id, "version": VERSION}
    frame = filter_completed_frame(frame, train_end)
    if "status" not in frame.columns:
        # load_model_frame only returns finals already.
        pass

    windows: list[dict[str, Any]] = []
    for year in EVAL_YEARS:
        train = frame[_season_mask(frame, year, train=True)]
        test = frame[_season_mask(frame, year, train=False)]
        # Prospective shadow: also score the latest partial season using all prior completed games.
        scored = _fit_td_split(train, test, league_id)
        if scored is None:
            windows.append({"season": year, "status": "insufficient_rows", "train_n": int(len(train)), "test_n": int(len(test))})
            continue
        windows.append({"season": year, "status": "ok", "held_out_style": "chronological_not_final", **scored})

    # Final artifact fit: all completed games through train_end; evaluate on last ~6 weeks.
    kick = pd.to_datetime(frame["kickoff_utc"], utc=True)
    if len(frame) == 0:
        return {"status": "empty_after_cutoff", "league_id": league_id}
    shadow_start = kick.max() - pd.Timedelta(days=45)
    train_final = frame[kick < shadow_start].copy()
    test_final = frame[kick >= shadow_start].copy()
    if len(train_final) < 500:
        train_final = frame.iloc[: int(len(frame) * 0.85)].copy()
        test_final = frame.iloc[int(len(frame) * 0.85) :].copy()
    features = _select_features(train_final, league_id)
    final = _fit_td_split(train_final, test_final, league_id, features=features)
    if final is None:
        return {"status": "fit_failed", "league_id": league_id, "train_n": int(len(train_final)), "test_n": int(len(test_final))}

    rates = _player_rates(frame, league_id)
    artifact = {
        "league_id": league_id,
        "version": VERSION,
        "market_id": MARKET_ID,
        "train_end": train_end.isoformat(),
        "frozen_at": moment.isoformat(),
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "epa_scale": "epa" if league_id == "nfl" else "ppa",
        "family": "poisson_anytime_td",
        "features": final["features"],
        "feature_betas": final["feature_betas"],
        "feature_z": final["feature_z"],
        "opp_beta": final["opp_beta"],
        "platt_a": final["platt_global"][0],
        "platt_b": final["platt_global"][1],
        "platt_by_pos": final["platt_by_pos"],
        "default_line": 0.5,
        "notes": [
            "phase2_rolling_anytime_td",
            "completed_finals_only",
            "m3_m4_untouched",
            "fbs_rz_gl_not_invented" if league_id == "fbs" else "nfl_rz_gl_priors",
        ],
        **rates,
        "shadow_metrics": final["m5"],
        "m3_refit_shadow": final["m3_refit"],
        "by_position": final["m5_by_position"],
        "current_season_coverage": {
            "n_rows": int(len(frame)),
            "n_2025": int((pd.to_datetime(frame["kickoff_utc"], utc=True) >= pd.Timestamp("2025-08-01", tz="UTC")).sum()),
            "n_2026": int((pd.to_datetime(frame["kickoff_utc"], utc=True) >= pd.Timestamp("2026-08-01", tz="UTC")).sum()),
            "players": int(frame["player_id"].nunique()),
        },
    }

    if persist:
        path = artifact_path(league_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(artifact, indent=2, default=str), encoding="utf-8")
        _persist_model_meta(session, league_id, artifact, train_end, final)
        session.flush()

    # Safety: M3/M4 bytes must be identical.
    m3_after = m3_path(league_id).read_bytes() if m3_path(league_id).exists() else None
    m4_after = m4_path(league_id).read_bytes() if m4_path(league_id).exists() else None
    if m3_after != m3_before:
        raise RuntimeError(f"M3 artifact changed during M5 train: {m3_path(league_id)}")
    if m4_after != m4_before:
        raise RuntimeError(f"M4 artifact changed during M5 train: {m4_path(league_id)}")

    return {
        "status": "ok",
        "league_id": league_id,
        "version": VERSION,
        "train_end": train_end.isoformat(),
        "artifact_path": str(artifact_path(league_id)),
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "features": final["features"],
        "shadow": final["m5"],
        "m3_refit_shadow": final["m3_refit"],
        "by_position": final["m5_by_position"],
        "windows": [
            {
                "season": w.get("season"),
                "status": w.get("status"),
                "m5": w.get("m5"),
                "m3_refit": w.get("m3_refit"),
                "m5_by_position": w.get("m5_by_position"),
                "features": w.get("features"),
                "train_n": w.get("train_n"),
                "test_n": w.get("test_n"),
            }
            for w in windows
        ],
        "coverage": artifact["current_season_coverage"],
        "m3_untouched": True,
        "m4_untouched": True,
    }


def _persist_model_meta(
    session: Session,
    league_id: str,
    artifact: dict[str, Any],
    train_end: datetime,
    final: dict[str, Any],
) -> None:
    vid = f"{VERSION}-{league_id}-{MARKET_ID}"
    existing = session.get(ModelVersion, vid)
    payload = {
        "features": artifact["features"],
        "feature_betas": artifact["feature_betas"],
        "platt_by_pos": artifact["platt_by_pos"],
        "research_only": True,
        "play_enabled": False,
        "promoted": False,
    }
    if existing is None:
        session.add(
            ModelVersion(
                model_version_id=vid,
                code_sha="m5-rolling-anytime-td-v1",
                training_cutoff=train_end,
                data_snapshot="historical_final_completed_only",
                hyperparameters=payload,
                calibration_version="platt_pos_v1",
                league_id=league_id,
                family="anytime_td_rolling",
            )
        )
    else:
        existing.training_cutoff = train_end
        existing.hyperparameters = payload
        existing.calibration_version = "platt_pos_v1"
    metrics = final["m5"]
    session.add(
        BacktestRun(
            run_id=new_id(),
            league_id=league_id,
            market_id=MARKET_ID,
            model_family=f"{VERSION}_anytime_td",
            train_end=train_end,
            test_start=train_end,
            test_end=datetime.now(UTC),
            n=int(metrics.get("n") or 0),
            brier=metrics.get("brier"),
            log_loss=metrics.get("log_loss"),
            ece=metrics.get("ece"),
            coverage=1.0,
            notes=json.dumps({"shadow": metrics, "by_position": final.get("m5_by_position")}, default=str)[:4000],
        )
    )
    session.add(
        ModelHealthSnapshot(
            league_id=league_id,
            market_id=MARKET_ID,
            as_of=datetime.now(UTC),
            brier=float(metrics.get("brier") or 0),
            log_loss=float(metrics.get("log_loss") or 0),
            ece=float(metrics.get("ece") or 0),
            skill_vs_novig=None,
            n=int(metrics.get("n") or 0),
            coverage=1.0,
            freshness_minutes=0,
            notes=f"{VERSION} {league_id} anytime_td; play_enabled=false; research_only",
        )
    )


def load_m5(league_id: str) -> dict[str, Any] | None:
    path = artifact_path(league_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def predict_anytime_td_m5(
    artifact: dict[str, Any],
    *,
    player_id: str,
    team_id: str,
    position_group: str,
    opp_def_z: float = 0.0,
    features: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Serve research-only Anytime TD probability from an M5 artifact."""
    k = float(artifact.get("k_player") or SHRINKAGE_K.get(artifact["league_id"], 12.0))
    league = float(artifact.get("league_rate") or 0.15)
    pos = float(artifact.get("pos_rates", {}).get(position_group, league))
    team = float(artifact.get("team_rates", {}).get(team_id or "", pos))
    n = int(artifact.get("player_n", {}).get(player_id, 0))
    raw = artifact.get("player_rates", {}).get(player_id)
    prior = team if raw is None else float(raw)
    shrink = n / (n + k) if n > 0 else 0.0
    mean = shrink * prior + (1.0 - shrink) * team
    mean = float(np.clip(mean, 1e-6, None))
    mean = float(apply_beta(np.array([mean]), np.array([opp_def_z]), float(artifact.get("opp_beta") or 0.0))[0])
    feats = features or {}
    betas = artifact.get("feature_betas") or {}
    zmeta = artifact.get("feature_z") or {}
    for name, beta in betas.items():
        if not beta or name not in feats:
            continue
        meta = zmeta.get(name) or {"mean": 0.0, "std": 1.0}
        sd = float(meta.get("std") or 1.0) or 1.0
        z = (float(feats[name]) - float(meta.get("mean") or 0.0)) / sd
        if not np.isfinite(z):
            z = 0.0
        mean = float(apply_beta(np.array([mean]), np.array([z]), float(beta))[0])
    p_raw = float(binary_probability(np.array([mean]))[0])
    platt_pos = (artifact.get("platt_by_pos") or {}).get(position_group)
    if platt_pos and len(platt_pos) == 2:
        a, b = float(platt_pos[0]), float(platt_pos[1])
    else:
        a, b = float(artifact.get("platt_a") or 0.0), float(artifact.get("platt_b") or 1.0)
    p = float(apply_platt(np.array([p_raw]), a, b)[0])
    return {
        "mean": mean,
        "p": p,
        "p_low": max(0.01, p - 0.05),
        "p_high": min(0.99, p + 0.05),
        "player_n": n,
        "prior_rate": prior,
        "model_version": VERSION,
        "research_only": True,
        "play_enabled": False,
    }


def compare_m5_vs_baselines(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Chronological backtests for M5 vs refit-M3 baseline; load frozen M4 metrics when present."""
    moment = as_utc(now) or datetime.now(UTC)
    leagues: list[dict[str, Any]] = []
    m4_cmp_path = Path(settings.parquet_dir) / "models" / "m4_comparison.json"
    m4_cmp = json.loads(m4_cmp_path.read_text(encoding="utf-8")) if m4_cmp_path.exists() else None
    for league_id in ("nfl", "fbs"):
        trained = train_league_anytime_td(session, league_id, now=moment, persist=True)
        m4_art = None
        if m4_path(league_id).exists():
            m4_art = json.loads(m4_path(league_id).read_text(encoding="utf-8"))
        m4_anytime = None
        if m4_cmp:
            for block in m4_cmp.get("leagues") or []:
                if block.get("league_id") == league_id:
                    m4_anytime = (block.get("markets") or {}).get("anytime_td")
        leagues.append(
            {
                "league_id": league_id,
                "m5": trained,
                "m4_artifact_present": m4_art is not None,
                "m4_research_only": bool((m4_art or {}).get("research_only", True)),
                "m4_anytime_td_windows": m4_anytime.get("windows") if isinstance(m4_anytime, dict) else None,
                "m3_artifact_train_end": _m3_train_end(league_id),
            }
        )
    report = {
        "generated_at": moment.isoformat(),
        "version": VERSION,
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "baseline_policy": "M3 and M4 kept as comparison baselines; M5 not auto-promoted",
        "leagues": leagues,
    }
    comparison_path().parent.mkdir(parents=True, exist_ok=True)
    comparison_path().write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def _m3_train_end(league_id: str) -> str | None:
    path = m3_path(league_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("train_end")
