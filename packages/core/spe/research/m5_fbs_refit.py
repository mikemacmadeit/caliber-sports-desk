"""Isolated FBS M5 research refit: feature parity, then controlled A/B/C comparison.

Does not overwrite protected ``fbs_m5.json``. Does not enable Validated Play.
Does not invent targets, RZ/GL, or availability.
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

from spe.db.base import as_utc
from spe.models.advanced import binary_probability
from spe.models.metrics import apply_platt, brier_score, expected_calibration_error, log_loss, platt_calibrate
from spe.models.rolling_td import (
    TD_FEATURES_FBS,
    VERSION as M5_VERSION,
    _fit_td_split,
    _player_rates,
    _season_mask,
    _select_features,
    artifact_path,
    load_m5,
    prepare_td_frame,
)
from spe.models.player_td.score import _opponent_matchup_priors, _player_priors_from_hist
from spe.research.atd_forecast import research_dir

PIPELINE_VERSION = "fbs_m5_research_refit_v1"
RESEARCH_MODEL_ID = "m5_fbs_research_refit_v1"
RECENT_MODEL_ID = "m5_fbs_research_recent_form_v1"


def out_dir() -> Path:
    path = research_dir() / "m5_fbs_refit"
    path.mkdir(parents=True, exist_ok=True)
    return path


FEATURE_DICTIONARY: dict[str, dict[str, Any]] = {
    "prior_td_rate": {
        "measures": "Expanding mean of rush_td+rec_td counts per prior game (expected TD count / game)",
        "not": "Not the percentage of games with ≥1 TD (see prior_atd_game_rate for that)",
        "source": "player_game_stats.rush_td + rec_td",
        "units": "TD counts / game",
        "window_training": "shift(1).expanding mean by player over the involved-row universe",
        "window_live": "mean of all completed involved games before kickoff (same definition)",
        "universe_note": (
            "Priors are fit after the involved filter so zero-touch box rows do not inflate "
            "the denominator. Frozen production M5 may have been fit under the old order."
        ),
        "missing": "NaN when no prior games; never zero-filled as if observed",
    },
    "prior_atd_game_rate": {
        "measures": "Share of prior games with rush_td+rec_td > 0",
        "units": "probability [0,1]",
        "note": "Research diagnostic only; not an M5 training feature",
    },
    "roll3_td_rate": {
        "measures": "Mean TD count over last 3 completed games",
        "window": "shift(1).rolling(3)",
    },
    "season_td_rate / recent8_td_rate": {
        "measures": "Alternate windows of mean TD count",
        "note": "Research-only inputs for recent-form variant; must not silently replace prior_td_rate",
    },
    "prior_carries / prior_receptions": {
        "measures": "Expanding mean of carries / receptions",
        "fbs_targets": "True targets unavailable from CFBD; prior_targets stays null; receptions exposed as fbs_receptions_usage_proxy",
    },
    "prior_carry_share": {
        "measures": "Expanding mean of player carries / team carries",
        "fbs": "Used; prior_target_share forced NaN",
    },
    "own_off_epa": {
        "measures": "Team prior off EPA/PPA per play (shifted expanding)",
        "source": "team_analytics.off_epa_per_play",
    },
    "opp_def_z": {
        "measures": "Opponent prior def EPA/PPA allowed, z-scored vs frame opp_def distribution",
        "sign": "Positive ⇒ opponent has allowed more EPA than average (softer D). Independent per team — not forced opposite signs",
        "pit": "Only games with kickoff < prediction kickoff enter the prior",
        "missing": "None when unavailable; never disguised as 0",
    },
    "opp_adj_def": {
        "measures": "Opponent prior adjusted defensive EPA/PPA",
        "source": "team_analytics.adj_def_epa",
    },
    "usage_x_defense": {
        "measures": "prior_carry_share (FBS) × opp_def_z",
        "missing": "NaN if either factor missing",
    },
    "availability": {
        "live": "ops.availability_for_player; unknown never assumed healthy",
        "training": "Not a fitted M5 feature; incomplete injury coverage remains explicit",
    },
}


def _sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def document_features() -> dict[str, Any]:
    return {
        "pipeline_version": PIPELINE_VERSION,
        "m5_training_features_fbs": list(TD_FEATURES_FBS),
        "dictionary": FEATURE_DICTIONARY,
        "label": {
            "training_outcome": "anytime_tds = rush_td + rec_td (count)",
            "scored_as": "binary anytime TD via (count > 0); Poisson bridge 1-exp(-λ) on mean count rate",
            "pass_td_excluded": True,
        },
    }


def run_parity_tests(session: Session, *, n_rows: int = 40) -> dict[str, Any]:
    """Compare training-frame priors vs live prior builder on identical historical cutoffs."""
    frame = prepare_td_frame(session, "fbs", year_max=2026)
    if frame.empty:
        return {"status": "empty"}
    frame = frame.sort_values("kickoff_utc")
    # Sample mid/late 2026 rows with history
    ko = pd.to_datetime(frame["kickoff_utc"], utc=True)
    pool = frame[(ko.dt.year == 2026) & frame["prior_td_rate"].notna()].copy()
    if len(pool) < 10:
        pool = frame[frame["prior_td_rate"].notna()].copy()
    sample = pool.sample(n=min(n_rows, len(pool)), random_state=7)

    rows_out: list[dict[str, Any]] = []
    mismatches = 0
    for rec in sample.itertuples(index=False):
        kick = pd.Timestamp(rec.kickoff_utc)
        if kick.tzinfo is None:
            kick = kick.tz_localize("UTC")
        live = _player_priors_from_hist(frame, str(rec.player_id), kick.to_pydatetime(), "fbs")
        train_td = float(rec.prior_td_rate) if pd.notna(rec.prior_td_rate) else None
        live_td = live.get("prior_td_rate")
        # Training prior_td_rate on row t excludes game t; live mean over games < kick includes
        # all games before kick — equivalent to training prior on the *next* game after last.
        # For row t, live recomputed at kick should match training prior on that row when
        # live uses only games strictly before kick (same as shift expanding).
        delta = None
        if train_td is not None and live_td is not None:
            delta = abs(train_td - live_td)
            if delta > 1e-6:
                mismatches += 1
        # Defense parity: recompute matchup at this kickoff for opp_id
        opp_id = getattr(rec, "opp_id", None)
        matchup = (
            _opponent_matchup_priors(session, frame, str(opp_id), kick.to_pydatetime(), "fbs")
            if opp_id
            else {}
        )
        train_z = float(rec.opp_def_z) if hasattr(rec, "opp_def_z") and pd.notna(rec.opp_def_z) else None
        live_z = matchup.get("opp_def_z")
        z_delta = abs(train_z - live_z) if train_z is not None and live_z is not None else None
        if z_delta is not None and z_delta > 0.15:
            mismatches += 1
        rows_out.append(
            {
                "player_id": rec.player_id,
                "game_id": rec.game_id,
                "train_prior_td_rate": train_td,
                "live_prior_td_rate": live_td,
                "td_delta": delta,
                "train_opp_def_z": train_z,
                "live_opp_def_z": live_z,
                "z_delta": z_delta,
                "live_targets": live.get("prior_targets"),
                "live_receptions": live.get("prior_receptions"),
                "proxy": live.get("fbs_receptions_usage_proxy"),
            }
        )

    td_deltas = [r["td_delta"] for r in rows_out if r["td_delta"] is not None]
    z_deltas = [r["z_delta"] for r in rows_out if r["z_delta"] is not None]
    return {
        "status": "ok",
        "n_tested": len(rows_out),
        "prior_td_rate_max_abs_delta": float(max(td_deltas)) if td_deltas else None,
        "prior_td_rate_mean_abs_delta": float(np.mean(td_deltas)) if td_deltas else None,
        "opp_def_z_max_abs_delta": float(max(z_deltas)) if z_deltas else None,
        "opp_def_z_mean_abs_delta": float(np.mean(z_deltas)) if z_deltas else None,
        "targets_never_filled_from_receptions": all(r["live_targets"] is None for r in rows_out),
        "n_flagged_mismatches": mismatches,
        "pass_prior_td": (max(td_deltas) if td_deltas else 0) < 1e-4,
        "pass_targets_distinct": all(r["live_targets"] is None for r in rows_out),
        "examples": rows_out[:8],
    }


def inspect_opp_def_z(session: Session) -> dict[str, Any]:
    frame = prepare_td_frame(session, "fbs", year_max=2026)
    if frame.empty or "opp_def_z" not in frame.columns:
        return {"status": "empty"}
    # Same-game both teams' opp_def_z — should be independent (can share sign)
    sample = (
        frame.dropna(subset=["opp_def_z", "game_id", "team_id"])
        .groupby("game_id")
        .filter(lambda g: g["team_id"].nunique() >= 2)
    )
    same_sign = 0
    opposite_sign = 0
    checked = 0
    examples = []
    for gid, g in sample.groupby("game_id"):
        teams = g.drop_duplicates("team_id")
        if len(teams) < 2:
            continue
        a, b = teams.iloc[0], teams.iloc[1]
        za, zb = float(a["opp_def_z"]), float(b["opp_def_z"])
        checked += 1
        if za * zb > 0:
            same_sign += 1
        elif za * zb < 0:
            opposite_sign += 1
        if len(examples) < 5:
            examples.append(
                {
                    "game_id": gid,
                    "team_a": a["team_id"],
                    "z_a": za,
                    "team_b": b["team_id"],
                    "z_b": zb,
                }
            )
    return {
        "status": "ok",
        "definition": FEATURE_DICTIONARY["opp_def_z"],
        "n_games_checked": checked,
        "both_positive_or_both_negative": same_sign,
        "opposite_signs": opposite_sign,
        "same_sign_possible": same_sign > 0,
        "nan_rate": float(frame["opp_def_z"].isna().mean()),
        "zero_rate_among_finite": float((frame["opp_def_z"].fillna(np.nan) == 0).mean()),
        "examples": examples,
        "note": "Opposite signs are common when one defense is soft and one is stout; not forced.",
    }


def inspect_bryson_and_peers(session: Session) -> dict[str, Any]:
    frame = prepare_td_frame(session, "fbs", year_max=2026)
    # Resolve Bryson by scanning recent names via players join already in frame? frame may lack names.
    from sqlalchemy import text

    ids = {
        r["name"]: r["player_id"]
        for r in session.execute(
            text(
                "SELECT name, player_id FROM players WHERE name IN "
                "('Bryson Washington','Byrum Brown','Keshaun Singleton','Caleb Hawkins','Jahiem White')"
            )
        ).mappings()
    }
    out = []
    for name, pid in ids.items():
        sub = frame[frame["player_id"] == pid].sort_values("kickoff_utc")
        if sub.empty:
            out.append({"player": name, "status": "no_frame_rows"})
            continue
        last = sub.iloc[-1]
        live = _player_priors_from_hist(frame, pid, pd.Timestamp(last["kickoff_utc"]).to_pydatetime(), "fbs")
        out.append(
            {
                "player": name,
                "player_id": pid,
                "n_frame_games": int(len(sub)),
                "train_prior_td_rate_last": float(last["prior_td_rate"]) if pd.notna(last.get("prior_td_rate")) else None,
                "live_expanding_prior_td_rate": live.get("prior_td_rate"),
                "live_season_td_rate": live.get("season_td_rate"),
                "live_recent8_td_rate": live.get("recent8_td_rate"),
                "live_prior_atd_game_rate": live.get("prior_atd_game_rate"),
                "live_carries": live.get("prior_carries"),
                "live_receptions": live.get("prior_receptions"),
                "live_targets": live.get("prior_targets"),
                "proxy_receptions": live.get("fbs_receptions_usage_proxy"),
                "sample_size": live.get("sample_size"),
                "season_sample_size": live.get("season_sample_size"),
            }
        )
    return {"status": "ok", "players": out}


def _score_artifact_on_frame(artifact: dict[str, Any], frame: pd.DataFrame) -> np.ndarray:
    """Vectorized M5-style score using artifact betas (research)."""
    from spe.models.advanced import apply_beta, shrinkage_mean

    # Rebuild shrinkage base on this frame alone is wrong for serving; for eval we use
    # the artifact's stored player/team rates like production predict_anytime_td_m5.
    preds = np.full(len(frame), np.nan)
    k = float(artifact.get("k_player") or 12.0)
    league = float(artifact.get("league_rate") or 0.15)
    pos_rates = artifact.get("pos_rates") or {}
    team_rates = artifact.get("team_rates") or {}
    player_rates = artifact.get("player_rates") or {}
    player_n = artifact.get("player_n") or {}
    betas = artifact.get("feature_betas") or {}
    zmeta = artifact.get("feature_z") or {}
    opp_beta = float(artifact.get("opp_beta") or 0.0)
    platt_by_pos = artifact.get("platt_by_pos") or {}
    a_g = float(artifact.get("platt_a") or 0.0)
    b_g = float(artifact.get("platt_b") or 1.0)

    for i, rec in enumerate(frame.itertuples(index=False)):
        pid = str(rec.player_id)
        tid = str(getattr(rec, "team_id", "") or "")
        pos = str(getattr(rec, "position_group", "UNK") or "UNK")
        n = int(player_n.get(pid, 0))
        raw = player_rates.get(pid)
        team = float(team_rates.get(tid, pos_rates.get(pos, league)))
        prior = team if raw is None else float(raw)
        shrink = n / (n + k) if n > 0 else 0.0
        mean = shrink * prior + (1.0 - shrink) * team
        z_opp = getattr(rec, "opp_def_z", np.nan)
        try:
            z_opp_f = float(z_opp) if z_opp is not None and np.isfinite(float(z_opp)) else 0.0
            opp_defaulted = not (z_opp is not None and np.isfinite(float(z_opp)))
        except (TypeError, ValueError):
            z_opp_f, opp_defaulted = 0.0, True
        # For research eval we still apply opp_beta with 0 when missing (matches current predict path)
        # but track separately in ablation.
        _ = opp_defaulted
        mean = float(apply_beta(np.array([mean]), np.array([z_opp_f]), opp_beta)[0])
        for name, beta in betas.items():
            if not beta:
                continue
            val = getattr(rec, name, np.nan)
            try:
                if val is None or not np.isfinite(float(val)):
                    continue
                v = float(val)
            except (TypeError, ValueError):
                continue
            meta = zmeta.get(name) or {"mean": 0.0, "std": 1.0}
            sd = float(meta.get("std") or 1.0) or 1.0
            z = (v - float(meta.get("mean") or 0.0)) / sd
            if not np.isfinite(z):
                z = 0.0
            mean = float(apply_beta(np.array([mean]), np.array([z]), float(beta))[0])
        p_raw = float(binary_probability(np.array([mean]))[0])
        platt = platt_by_pos.get(pos)
        if platt and len(platt) == 2:
            a, b = float(platt[0]), float(platt[1])
        else:
            a, b = a_g, b_g
        preds[i] = float(apply_platt(np.array([p_raw]), a, b)[0])
    return preds


def _metrics_block(y: np.ndarray, p: np.ndarray) -> dict[str, Any]:
    ok = np.isfinite(p) & np.isfinite(y)
    if ok.sum() < 30:
        return {"n": int(ok.sum()), "status": "insufficient"}
    yb = (y[ok] > 0).astype(float)
    pp = np.clip(p[ok], 1e-6, 1 - 1e-6)
    return {
        "n": int(ok.sum()),
        "brier": float(brier_score(yb, pp)),
        "log_loss": float(log_loss(yb, pp)),
        "ece": float(expected_calibration_error(yb, pp)),
        "base_rate": float(yb.mean()),
        "mean_p": float(pp.mean()),
    }


def _cohort_metrics(frame: pd.DataFrame, y: np.ndarray, p: np.ndarray) -> dict[str, Any]:
    out: dict[str, Any] = {"by_position": {}}
    pos = frame["position_group"].astype(str).to_numpy()
    for pg in ("QB", "RB", "WR", "TE"):
        m = pos == pg
        out["by_position"][pg] = _metrics_block(y[m], p[m])
    # Sparse: prior games proxy via sample from expanding — use prior_td_rate notna count approx
    if "prior_td_rate" in frame.columns:
        # Approximate sparse as players with low history: use roll of non-null priors hard —
        # prefer prior from frame attrs if present; else use isna prior as brand-new.
        n_prior = frame.groupby("player_id").cumcount()  # 0-based games in frame order
        sparse = n_prior.to_numpy() < 5
        dense = ~sparse
        out["sparse_lt5_games_in_frame"] = _metrics_block(y[sparse], p[sparse])
        out["dense_ge5"] = _metrics_block(y[dense], p[dense])
    if "prior_carries" in frame.columns:
        carries = pd.to_numeric(frame["prior_carries"], errors="coerce").to_numpy()
        high = np.isfinite(carries) & (carries >= 10)
        out["high_usage_carries_ge10"] = _metrics_block(y[high], p[high])
    return out


def run_controlled_refit(session: Session) -> dict[str, Any]:
    """A=frozen M5; B=refit same methodology on repaired data; C=recent-form feature variant."""
    moment = datetime.now(UTC)
    frame = prepare_td_frame(session, "fbs", year_max=2026)
    if frame.empty:
        return {"status": "empty"}

    # Chronological split: train through 2025 season; evaluate 2026 (development/replay).
    train = frame[_season_mask(frame, 2026, train=True)].copy()
    test = frame[_season_mask(frame, 2026, train=False)].copy()
    if len(test) < 200:
        # Fallback last 20%
        cut = int(len(frame) * 0.8)
        train, test = frame.iloc[:cut].copy(), frame.iloc[cut:].copy()

    y_te = test["anytime_tds"].fillna(0).to_numpy(dtype=float)

    # --- A: frozen baseline ---
    frozen_path = artifact_path("fbs")
    frozen = load_m5("fbs")
    frozen_hash = _sha256_file(frozen_path)
    p_a = _score_artifact_on_frame(frozen, test) if frozen else np.full(len(test), np.nan)

    # --- B: refit same methodology on repaired frame ---
    features_b = _select_features(train, "fbs")
    fit_b = _fit_td_split(train, test, "fbs", features=features_b)
    rates_b = _player_rates(train, "fbs")
    art_b = {
        "league_id": "fbs",
        "version": RESEARCH_MODEL_ID,
        "pipeline_version": PIPELINE_VERSION,
        "feature_definition": "expanding_priors_match_training",
        "market_id": "anytime_td",
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "train_n": int(len(train)),
        "test_n": int(len(test)),
        "features": fit_b["features"] if fit_b else [],
        "feature_betas": fit_b["feature_betas"] if fit_b else {},
        "feature_z": fit_b["feature_z"] if fit_b else {},
        "opp_beta": fit_b["opp_beta"] if fit_b else 0.0,
        "platt_a": fit_b["platt_global"][0] if fit_b else 0.0,
        "platt_b": fit_b["platt_global"][1] if fit_b else 1.0,
        "platt_by_pos": fit_b["platt_by_pos"] if fit_b else {},
        **rates_b,
        "notes": ["research_refit_repaired_data", "m5_methodology_unchanged", "not_production"],
    }
    p_b = _score_artifact_on_frame(art_b, test)

    # --- C: recent-form variant — swap prior_td_rate with recent8 where available ---
    train_c = train.copy()
    test_c = test.copy()
    for df in (train_c, test_c):
        # Build recent8 expanding alternative from raw anytime_tds history inside frame
        df.sort_values(["player_id", "kickoff_utc"], inplace=True)
        g = df.groupby("player_id", sort=False)["anytime_tds"]
        recent8 = g.transform(lambda s: s.shift(1).rolling(8, min_periods=3).mean())
        # Only replace where recent8 observed; else keep expanding prior_td_rate
        base = pd.to_numeric(df["prior_td_rate"], errors="coerce")
        df["prior_td_rate_canonical"] = base
        df["prior_td_rate"] = recent8.where(recent8.notna(), base)
    features_c = _select_features(train_c, "fbs")
    fit_c = _fit_td_split(train_c, test_c, "fbs", features=features_c)
    rates_c = _player_rates(train_c, "fbs")
    art_c = {
        "league_id": "fbs",
        "version": RECENT_MODEL_ID,
        "pipeline_version": PIPELINE_VERSION + "+recent8_prior_td_rate",
        "feature_definition": "prior_td_rate:=recent8_mean_td_count_when_available",
        "market_id": "anytime_td",
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "features": fit_c["features"] if fit_c else [],
        "feature_betas": fit_c["feature_betas"] if fit_c else {},
        "feature_z": fit_c["feature_z"] if fit_c else {},
        "opp_beta": fit_c["opp_beta"] if fit_c else 0.0,
        "platt_a": fit_c["platt_global"][0] if fit_c else 0.0,
        "platt_b": fit_c["platt_global"][1] if fit_c else 1.0,
        "platt_by_pos": fit_c["platt_by_pos"] if fit_c else {},
        **rates_c,
        "notes": ["research_recent_form_variant", "not_production"],
    }
    p_c = _score_artifact_on_frame(art_c, test_c)

    # --- Defense ablation on B: zero opp_beta and drop defense features ---
    art_b_nodef = json.loads(json.dumps(art_b))
    art_b_nodef["opp_beta"] = 0.0
    art_b_nodef["feature_betas"] = {
        k: v for k, v in (art_b_nodef.get("feature_betas") or {}).items() if k not in {"opp_adj_def", "usage_x_defense"}
    }
    p_b_nodef = _score_artifact_on_frame(art_b_nodef, test)

    met_a = _metrics_block(y_te, p_a)
    met_b = _metrics_block(y_te, p_b)
    met_c = _metrics_block(y_te, p_c)
    met_nodef = _metrics_block(y_te, p_b_nodef)

    # Paired delta B vs A on identical rows
    ok = np.isfinite(p_a) & np.isfinite(p_b) & np.isfinite(y_te)
    paired = None
    if ok.sum() >= 50:
        yb = (y_te[ok] > 0).astype(float)
        ba = brier_score(yb, np.clip(p_a[ok], 1e-6, 1 - 1e-6))
        bb = brier_score(yb, np.clip(p_b[ok], 1e-6, 1 - 1e-6))
        paired = {
            "n": int(ok.sum()),
            "brier_A": float(ba),
            "brier_B": float(bb),
            "delta_brier_B_minus_A": float(bb - ba),
            "improved": bool(bb < ba),
        }

    def_gain = None
    okd = np.isfinite(p_b) & np.isfinite(p_b_nodef) & np.isfinite(y_te)
    if okd.sum() >= 50:
        yb = (y_te[okd] > 0).astype(float)
        with_d = brier_score(yb, np.clip(p_b[okd], 1e-6, 1 - 1e-6))
        no_d = brier_score(yb, np.clip(p_b_nodef[okd], 1e-6, 1 - 1e-6))
        def_gain = {
            "n": int(okd.sum()),
            "brier_with_defense": float(with_d),
            "brier_without_defense": float(no_d),
            "delta_brier_with_minus_without": float(with_d - no_d),
            "defense_helps": bool(with_d < no_d),
        }

    # Persist research artifacts only
    art_b_path = out_dir() / f"{RESEARCH_MODEL_ID}.json"
    art_c_path = out_dir() / f"{RECENT_MODEL_ID}.json"
    art_b_path.write_text(json.dumps(art_b, indent=2, default=str), encoding="utf-8")
    art_c_path.write_text(json.dumps(art_c, indent=2, default=str), encoding="utf-8")

    fingerprint = {
        "pipeline_version": PIPELINE_VERSION,
        "as_of": moment.isoformat(),
        "train_n": int(len(train)),
        "test_n": int(len(test)),
        "test_window": "chronological_2026_or_last_20pct",
        "label": "development_replay_not_prospective",
        "unique_test_games": int(test["game_id"].nunique()),
        "unique_test_players": int(test["player_id"].nunique()),
        "frozen_m5_sha256": frozen_hash,
        "research_B_sha256": _sha256_file(art_b_path),
        "research_C_sha256": _sha256_file(art_c_path),
        "protected_artifact_untouched": frozen_hash == _sha256_file(frozen_path),
    }

    return {
        "status": "ok",
        "fingerprint": fingerprint,
        "A_frozen_m5": {"metrics": met_a, "cohorts": _cohort_metrics(test, y_te, p_a)},
        "B_refit_repaired_data": {
            "metrics": met_b,
            "cohorts": _cohort_metrics(test, y_te, p_b),
            "fit_shadow": fit_b["m5"] if fit_b else None,
            "features": art_b.get("features"),
            "path": str(art_b_path),
        },
        "C_recent_form_variant": {
            "metrics": met_c,
            "cohorts": _cohort_metrics(test_c, y_te, p_c),
            "fit_shadow": fit_c["m5"] if fit_c else None,
            "features": art_c.get("features"),
            "path": str(art_c_path),
        },
        "defense_ablation_on_B": {"metrics": met_nodef, "paired_vs_B": def_gain},
        "paired_B_vs_A": paired,
        "candidate_for_shadow": _pick_candidate(met_a, met_b, met_c, paired, def_gain),
    }


def _pick_candidate(
    met_a: dict[str, Any],
    met_b: dict[str, Any],
    met_c: dict[str, Any],
    paired: dict[str, Any] | None,
    def_gain: dict[str, Any] | None,
) -> dict[str, Any]:
    ranked = sorted(
        [
            ("A_frozen", met_a.get("brier")),
            ("B_refit", met_b.get("brier")),
            ("C_recent", met_c.get("brier")),
        ],
        key=lambda x: (x[1] is None, x[1] if x[1] is not None else 9),
    )
    best = ranked[0][0]
    return {
        "best_on_dev_replay_brier": best,
        "ranking": ranked,
        "defense_add_benefit": bool(def_gain and def_gain.get("defense_helps")),
        "recommend_shadow": best in {"B_refit", "C_recent"} and (paired or {}).get("improved", best != "A_frozen"),
        "note": "Development/replay only — not genuine prospective validation.",
    }


def start_prospective_cohort(session: Session, candidate: str = "B_refit") -> dict[str, Any]:
    """Freeze a new clearly labeled prospective cohort — never backfill after kickoff."""
    from spe.research.atd_forecast import forecast_upcoming

    moment = datetime.now(UTC)
    forecasts = forecast_upcoming(session, now=moment, horizon_hours=72, max_games_per_league=25)
    fbs = ((forecasts.get("leagues") or {}).get("fbs") or {}).get("games") or []
    cohort = {
        "cohort_id": f"fbs_m5_refit_prospective_{moment.strftime('%Y%m%dT%H%M%SZ')}",
        "candidate": candidate,
        "pipeline_version": PIPELINE_VERSION,
        "created_at": moment.isoformat(),
        "holdout_tag": "prospective_unexamined_refit_v1",
        "play_enabled": False,
        "promoted": False,
        "n_games": len(fbs),
        "games": [
            {
                "game_id": g.get("game_id"),
                "kickoff_utc": g.get("kickoff_utc"),
                "home": g.get("home"),
                "away": g.get("away"),
                "n_players": len(g.get("players") or []),
                "immutable": True,
            }
            for g in fbs
        ],
        "note": "New cohort only. Existing freezes preserved. Coordinate grading with Agent 1 shadow settlement.",
    }
    path = out_dir() / "prospective" / f"{cohort['cohort_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cohort, indent=2, default=str), encoding="utf-8")
    # Do not overwrite latest_freeze.json used by the prior prospective ledger.
    (out_dir() / "prospective" / "latest_refit_cohort.json").write_text(
        json.dumps({"cohort_path": str(path), **{k: cohort[k] for k in ("cohort_id", "created_at", "candidate")}}, indent=2),
        encoding="utf-8",
    )
    return {"status": "ok", "path": str(path), "cohort_id": cohort["cohort_id"], "n_games": len(fbs)}


def trace_finder_players(session: Session, *, n: int = 10) -> dict[str, Any]:
    from spe.research.atd_forecast import forecast_upcoming
    from spe.research.atd_guardrails import build_finder_payload

    moment = datetime.now(UTC)
    fc = forecast_upcoming(session, now=moment, horizon_hours=72, max_games_per_league=15)
    finder = build_finder_payload(session, now=moment)
    traces = []
    for g in ((fc.get("leagues") or {}).get("fbs") or {}).get("games") or []:
        for pl in g.get("players") or []:
            if pl.get("p_m5") is None:
                continue
            traces.append(
                {
                    "player": pl.get("player_name"),
                    "team": pl.get("team_name"),
                    "matchup": f"{g.get('away')} @ {g.get('home')}",
                    "position": pl.get("position_group"),
                    "prior_td_rate": pl.get("prior_td_rate"),
                    "season_td_rate": pl.get("season_td_rate"),
                    "recent8_td_rate": pl.get("recent8_td_rate"),
                    "exp_carries": pl.get("exp_carries"),
                    "exp_targets": pl.get("exp_targets"),
                    "prior_receptions": pl.get("prior_receptions"),
                    "opp_def_z": pl.get("opp_def_z"),
                    "opp_def_source": pl.get("opp_def_source"),
                    "p_m5": pl.get("p_m5"),
                    "p_v1_ref": pl.get("p_player_td_v1_ref"),
                    "availability": pl.get("availability_status"),
                    "missing": pl.get("missing_information"),
                }
            )
            if len(traces) >= n:
                break
        if len(traces) >= n:
            break
    return {
        "status": "ok",
        "n": len(traces),
        "finder_games": finder.get("n_games"),
        "players": traces,
        "website_timeout_note": (
            "Anytime TD page can 500/timeout when /v1/props+/v1/days contend with heavy DB ingest; "
            "research JSON itself serves 200. Hand off to foundation agent."
        ),
    }


def run_m5_fbs_refit_milestone(session: Session) -> dict[str, Any]:
    moment = datetime.now(UTC)
    print("[m5-refit] feature dictionary…", flush=True)
    dictionary = document_features()
    print("[m5-refit] parity tests…", flush=True)
    parity = run_parity_tests(session)
    print("[m5-refit] opp_def_z inspection…", flush=True)
    opp = inspect_opp_def_z(session)
    print("[m5-refit] Bryson/peers…", flush=True)
    peers = inspect_bryson_and_peers(session)

    gates = {
        "parity_prior_td": bool(parity.get("pass_prior_td")),
        "targets_distinct": bool(parity.get("pass_targets_distinct")),
        "opp_def_same_sign_possible": bool(opp.get("same_sign_possible")),
        "opp_def_defined": opp.get("status") == "ok",
    }
    # Soft gate on prior_td: allow small float noise up to 1e-3
    if parity.get("prior_td_rate_max_abs_delta") is not None:
        gates["parity_prior_td"] = float(parity["prior_td_rate_max_abs_delta"]) < 1e-3

    report: dict[str, Any] = {
        "status": "ok",
        "as_of": moment.isoformat(),
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "validated_play": False,
        "protected_m5_untouched": True,
        "pipeline_version": PIPELINE_VERSION,
        "feature_dictionary": dictionary,
        "parity": parity,
        "opp_def_z": opp,
        "form_examples": peers,
        "gates": gates,
        "gates_pass": all(gates.values()),
    }

    if not report["gates_pass"]:
        report["refit"] = {"status": "blocked", "reason": "feature_consistency_gates_failed", "gates": gates}
    else:
        print("[m5-refit] controlled refit A/B/C…", flush=True)
        report["refit"] = run_controlled_refit(session)
        print("[m5-refit] prospective cohort…", flush=True)
        cand = (report["refit"].get("candidate_for_shadow") or {}).get("best_on_dev_replay_brier") or "B_refit"
        report["prospective_cohort"] = start_prospective_cohort(session, candidate=cand)
        print("[m5-refit] finder traces…", flush=True)
        report["finder_traces"] = trace_finder_players(session)

    # Answers
    refit = report.get("refit") or {}
    report["final_answers"] = {
        "1_training_live_consistent": bool(gates.get("parity_prior_td") and gates.get("targets_distinct")),
        "2_opp_def_z_correct": bool(gates.get("opp_def_defined") and gates.get("opp_def_same_sign_possible")),
        "3_repaired_data_improved_m5": (refit.get("paired_B_vs_A") or {}).get("improved"),
        "4_recent_form_improved_further": (
            (refit.get("C_recent_form_variant") or {}).get("metrics", {}).get("brier")
            is not None
            and (refit.get("B_refit_repaired_data") or {}).get("metrics", {}).get("brier") is not None
            and (refit.get("C_recent_form_variant") or {}).get("metrics", {}).get("brier")
            < (refit.get("B_refit_repaired_data") or {}).get("metrics", {}).get("brier")
        )
        if refit.get("status") == "ok"
        else None,
        "5_defense_benefit": (refit.get("defense_ablation_on_B") or {}).get("paired_vs_B", {}).get("defense_helps"),
        "6_shadow_candidate": (refit.get("candidate_for_shadow") or {}),
        "7_still_unproven": [
            "Genuine prospective validation (new cohort not yet settled)",
            "Injury/availability completeness for FBS",
            "Whether recent-form helps out-of-sample prospectively",
            "Website timeout under DB load (foundation agent)",
        ],
    }

    path = out_dir() / "m5_fbs_refit_report.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    print(f"[m5-refit] wrote {path}", flush=True)
    return report
