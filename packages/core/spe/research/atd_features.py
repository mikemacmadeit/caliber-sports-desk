"""Canonical Anytime TD player-opportunity feature pipeline.

Consolidates duplicate enrichment previously spread across M5 (rolling_td),
M6 (player_td.features), and Player TD v1 (dataset). All prior-only; no leakage.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.models.player_td.features import (
    DEFENSE_FEATURES,
    INJURY_FEATURES,
    OPPORTUNITY_FEATURES,
    SKILL,
    all_candidate_features,
    build_opportunity_study_frame,
    enrich_opportunity_features,
    feature_coverage,
)
from spe.models.player_td.shares import ShareWeights, attach_shares, fit_share_weights
from spe.models.rolling_td import TD_FEATURES_FBS, TD_FEATURES_NFL

IDENTITY_KEYS = (
    "game_id",
    "player_id",
    "team_id",
    "opp_id",
    "league_id",
    "kickoff_utc",
    "position_group",
)

VERSION = "atd_features_v1"


def m5_feature_candidates(league_id: str) -> list[str]:
    return list(TD_FEATURES_NFL if league_id == "nfl" else TD_FEATURES_FBS)


def opportunity_feature_candidates(league_id: str) -> list[str]:
    return all_candidate_features(league_id)


def build_canonical_opportunity_frame(
    session: Session,
    league_id: str,
    *,
    year_max: int = 2026,
    universe: str = "corrected",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Single source for player opportunity features used by M5 / M6 / Player TD research."""
    frame, meta = build_opportunity_study_frame(
        session, league_id, year_max=year_max, universe=universe
    )
    meta = {
        **meta,
        "feature_pipeline": VERSION,
        "identity_keys": list(IDENTITY_KEYS),
        "leakage_policy": "all priors shift(1); injury reported_at < kickoff; defense from completed games only",
        "pass_td_excluded": True,
        "opportunity_features": list(OPPORTUNITY_FEATURES),
        "defense_features": list(DEFENSE_FEATURES),
        "injury_features": list(INJURY_FEATURES),
        "m5_candidates": m5_feature_candidates(league_id),
        "opportunity_candidates": opportunity_feature_candidates(league_id),
    }
    if frame.empty:
        return frame, meta
    if "league_id" not in frame.columns:
        frame = frame.copy()
        frame["league_id"] = league_id
    if "opp_id" not in frame.columns and {"team_id", "home_team_id", "away_team_id"}.issubset(frame.columns):
        frame = frame.copy()
        frame["opp_id"] = np.where(
            frame["team_id"] == frame["home_team_id"],
            frame["away_team_id"],
            frame["home_team_id"],
        )
    meta["coverage"] = feature_coverage(frame, opportunity_feature_candidates(league_id))
    return frame.reset_index(drop=True), meta


def attach_team_scoring_and_shares(
    session: Session,
    frame: pd.DataFrame,
    league_id: str,
) -> tuple[pd.DataFrame, ShareWeights, dict[str, Any]]:
    """Layer team_td_v1 λ and opportunity shares onto the canonical opportunity frame."""
    # Import lazily to avoid cycles with player_td.dataset during migration.
    from spe.models.player_td import dataset as player_td_dataset

    if frame.empty:
        from spe.models.player_td.shares import default_weights

        return frame, default_weights(league_id), {"status": "empty"}

    out = player_td_dataset.attach_team_td_lambdas(session, frame, league_id)
    kick = pd.to_datetime(out["kickoff_utc"], utc=True)
    share_train = out.loc[kick < pd.Timestamp("2024-08-01", tz="UTC")]
    if len(share_train) < 500:
        share_train = out.iloc[: max(int(len(out) * 0.6), 1)]
    weights = fit_share_weights(share_train, league_id)
    out = attach_shares(out, weights)
    out["lambda_rush_td"] = out["team_lambda_rush_td"] * out["rush_td_share"]
    out["lambda_rec_td"] = out["team_lambda_pass_td"] * out["rec_td_share"]
    out["lambda_player_td"] = out["lambda_rush_td"] + out["lambda_rec_td"]
    out["y_atd"] = (out["anytime_tds"].fillna(0) > 0).astype(float)
    out["league_id"] = league_id
    meta = {
        "team_source": str(out["team_td_source"].iloc[0]) if "team_td_source" in out.columns else None,
        "share_train_n": int(len(share_train)),
        "rows": int(len(out)),
    }
    return out.reset_index(drop=True), weights, meta


def build_player_td_research_frame(
    session: Session,
    league_id: str,
    *,
    year_max: int = 2026,
    universe: str = "corrected",
) -> tuple[pd.DataFrame, ShareWeights, dict[str, Any]]:
    """Canonical opportunity features + team_td_v1 λ + shares (Player TD v1 research path)."""
    frame, opp_meta = build_canonical_opportunity_frame(
        session, league_id, year_max=year_max, universe=universe
    )
    frame, weights, share_meta = attach_team_scoring_and_shares(session, frame, league_id)
    return frame, weights, {**opp_meta, **share_meta}


def feature_matrix(
    frame: pd.DataFrame,
    feature_names: list[str],
    *,
    strict: bool = False,
    align_schema: bool = False,
) -> tuple[np.ndarray, list[str], np.ndarray]:
    """Build X matrix.

    When ``align_schema=True``, columns match ``feature_names`` exactly (missing → NaN)
    so a serialized model always sees the training width. HistGBM handles NaN natively.
    When ``strict=True``, rows missing any listed feature are marked invalid.
    """
    if align_schema:
        cols: list[np.ndarray] = []
        for c in feature_names:
            if c in frame.columns:
                cols.append(pd.to_numeric(frame[c], errors="coerce").to_numpy(dtype=float))
            else:
                cols.append(np.full(len(frame), np.nan, dtype=float))
        if not feature_names:
            x = np.empty((len(frame), 0), dtype=float)
        else:
            x = np.column_stack(cols)
        if strict:
            valid = np.isfinite(x).all(axis=1) if x.size else np.zeros(len(frame), dtype=bool)
        else:
            # Invalid only when every schema feature is NaN for the row.
            valid = np.isfinite(x).any(axis=1) if x.size else np.zeros(len(frame), dtype=bool)
        return x, list(feature_names), valid

    present = [c for c in feature_names if c in frame.columns]
    missing_cols = [c for c in feature_names if c not in frame.columns]
    if not present:
        empty = np.empty((len(frame), 0), dtype=float)
        valid = np.zeros(len(frame), dtype=bool) if strict else np.ones(len(frame), dtype=bool)
        return empty, present, valid

    cols = [pd.to_numeric(frame[c], errors="coerce").to_numpy(dtype=float) for c in present]
    x = np.column_stack(cols)
    if strict:
        if missing_cols:
            valid = np.zeros(len(frame), dtype=bool)
        else:
            valid = np.isfinite(x).all(axis=1)
    else:
        valid = np.ones(len(frame), dtype=bool)
    return x, present, valid


def extract_feature_dict(
    row: pd.Series | dict[str, Any],
    feature_names: list[str],
    *,
    strict: bool = False,
) -> tuple[dict[str, float] | None, list[str]]:
    """Extract finite feature values. Strict mode returns None when any required value is missing."""
    feats: dict[str, float] = {}
    problems: list[str] = []
    getter = row.get if hasattr(row, "get") else lambda k, default=None: row[k] if k in row else default
    for name in feature_names:
        if name not in (row.index if hasattr(row, "index") else row):
            problems.append(f"missing_col:{name}")
            continue
        val = getter(name)
        try:
            fval = float(val)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            problems.append(f"non_numeric:{name}")
            continue
        if not np.isfinite(fval):
            problems.append(f"nan:{name}")
            continue
        feats[name] = fval
    if strict and problems:
        return None, problems
    return feats, problems


def row_feature_fingerprint(frame: pd.DataFrame, feature_names: list[str], n: int = 200) -> str:
    """Stable hash of a sample of feature rows for reproducibility checks."""
    cols = [c for c in list(IDENTITY_KEYS) + feature_names if c in frame.columns]
    sample = frame.sort_values([c for c in ("kickoff_utc", "game_id", "player_id") if c in frame.columns]).head(n)
    payload = sample[cols].astype(str).to_csv(index=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_feature_reproducibility(
    session: Session,
    league_id: str,
    *,
    year_max: int = 2026,
) -> dict[str, Any]:
    """Build the frame twice; identical inputs must yield identical feature fingerprints."""
    f1, m1 = build_canonical_opportunity_frame(session, league_id, year_max=year_max)
    f2, m2 = build_canonical_opportunity_frame(session, league_id, year_max=year_max)
    feats = opportunity_feature_candidates(league_id)
    h1 = row_feature_fingerprint(f1, feats) if not f1.empty else "empty"
    h2 = row_feature_fingerprint(f2, feats) if not f2.empty else "empty"
    return {
        "league_id": league_id,
        "identical": h1 == h2,
        "fingerprint": h1,
        "rows": int(len(f1)),
        "players": int(f1["player_id"].nunique()) if len(f1) else 0,
        "coverage_m5": feature_coverage(f1, m5_feature_candidates(league_id)) if len(f1) else {},
        "meta_keys": sorted(m1.keys()),
        "second_pass_rows": int(len(f2)),
        "second_pass_meta_universe": m2.get("universe"),
    }


def enrich_existing_frame(frame: pd.DataFrame, league_id: str) -> pd.DataFrame:
    """Apply canonical opportunity enrichment to an already-loaded model frame."""
    return enrich_opportunity_features(frame, league_id)


def dump_feature_schema(league_id: str) -> dict[str, Any]:
    return {
        "version": VERSION,
        "league_id": league_id,
        "identity_keys": list(IDENTITY_KEYS),
        "skill_positions": sorted(SKILL),
        "m5_candidates": m5_feature_candidates(league_id),
        "opportunity_candidates": opportunity_feature_candidates(league_id),
        "schema_json": json.dumps(
            {
                "identity": list(IDENTITY_KEYS),
                "m5": m5_feature_candidates(league_id),
                "opportunity": opportunity_feature_candidates(league_id),
            },
            sort_keys=True,
        ),
    }
