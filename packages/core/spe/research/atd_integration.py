"""Fair Anytime TD model inventory, comparison, and ensemble research.

Audit/research only. Does not promote models, rebuild the simulator, modify
the live database, or overwrite protected artifacts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.config import settings
from spe.db.base import as_utc
from spe.models.advanced import apply_beta, binary_probability, opponent_adjustment, shrinkage_mean
from spe.models.metrics import apply_platt, brier_score, expected_calibration_error, log_loss, platt_calibrate
from spe.models.player_td.candidates import fit_hist_gbm
from spe.models.player_td.dataset import build_player_td_frame
from spe.models.player_td.model import predict_frame
from spe.models.player_td.shares import attach_shares, fit_share_weights
from spe.models.player_td.train import load_player_td_model
from spe.models.rolling_td import load_m5, predict_anytime_td_m5
from spe.shadow.m4_artifact import adjust_mean, load_m4_artifacts, probability_from_mean
from spe.simulate.correlated_atd.isolation import snapshot_protected
from spe.simulate.correlated_atd.player_weights import load_m6, _m6_predict_simple
from spe.simulate.correlated_atd.scoring import score_games, summarize_predictions

REPORT_DIR = Path(settings.parquet_dir).parent / "research"
REPORT_PATH = REPORT_DIR / "atd_integration_report.json"

# Games examined in prior Phase 2/3/4/5 reports — not treated as untouched.
PREVIOUSLY_EXAMINED_NOTE = (
    "Shadow and 2025 windows overlap prior Phase 2/3/M6/atd_sim examinations. "
    "Metrics are comparative research scores, not untouched holdouts."
)


def report_path() -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return REPORT_PATH


def inventory_artifacts() -> dict[str, Any]:
    root = Path(settings.parquet_dir) / "models"
    specs = [
        ("m3", "{lg}_m3.json", "Hierarchical shrinkage Anytime TD (multi-market)"),
        ("m4", "{lg}_m4.json", "Frozen research feature betas on M3 base"),
        ("m5", "{lg}_m5.json", "Rolling Anytime TD Poisson + feature betas"),
        ("team_td_v1", "{lg}_team_td_v1.json", "Phase 2 team offensive TD distributions"),
        ("player_td_v1", "{lg}_player_td_v1.json", "Phase 3 team-λ × opportunity shares"),
        ("m6", "player_td/{lg}_m6_player_td.json", "Parallel opportunity/GBM Anytime TD study"),
        ("atd_sim_v1", "atd_sim/{lg}_atd_sim_backtest.json", "Correlated Team TD × M6 simulator backtest"),
    ]
    out: dict[str, Any] = {"as_of": datetime.now(UTC).isoformat(), "leagues": {}}
    for lg in ("nfl", "fbs"):
        models: dict[str, Any] = {}
        for key, pattern, role in specs:
            rel = pattern.format(lg=lg)
            path = root / rel
            if not path.exists():
                models[key] = {"status": "missing", "path": str(path), "role": role}
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
            entry: dict[str, Any] = {
                "status": "ok",
                "path": str(path),
                "role": role,
                "bytes": path.stat().st_size,
                "train_end": raw.get("train_end") or raw.get("beta_train_end"),
                "frozen_at": raw.get("frozen_at"),
                "version": raw.get("version") or key,
                "family": raw.get("family") or raw.get("selected_family"),
                "play_enabled": raw.get("play_enabled", False),
                "promoted": raw.get("promoted", False),
                "research_only": raw.get("research_only", True),
            }
            if key == "m3":
                at = (raw.get("markets") or {}).get("anytime_td") or {}
                entry["inputs"] = ["player_id", "team_id", "position_group", "opp_def_z"]
                entry["family"] = at.get("family")
                entry["train_end"] = at.get("train_end") or raw.get("train_end")
            elif key == "m4":
                at = (raw.get("markets") or {}).get("anytime_td") or {}
                entry["inputs"] = at.get("features") or []
                entry["depends_on"] = ["m3_shrinkage_base"]
            elif key == "m5":
                entry["inputs"] = raw.get("features") or []
                entry["depends_on"] = ["player priors", "opp_def_z"]
            elif key == "team_td_v1":
                entry["inputs"] = raw.get("feature_names") or []
                entry["outputs"] = ["lambda_off/pass/rush", "p_off_td buckets"]
            elif key == "player_td_v1":
                entry["inputs"] = list((raw.get("feature_betas") or {}).keys())
                entry["depends_on"] = ["team_td_v1 lambdas", "opportunity shares"]
            elif key == "m6":
                entry["inputs"] = raw.get("features") or []
                entry["serving_note"] = (
                    "Artifact family is hist_gbm for offline metrics; live/sim serving uses "
                    "player_rates shrinkage + Platt (_m6_predict_simple). GBM weights are not serialized."
                )
                entry["shadow_brier"] = (raw.get("shadow_metrics") or {}).get("brier")
            elif key == "atd_sim_v1":
                entry["depends_on"] = ["team_td_v1", "m6 rates", "simulate_correlated_atd"]
                entry["n"] = raw.get("n")
                entry["n_games"] = raw.get("n_games")
                entry["sim_brier"] = (raw.get("atd_sim_v1") or {}).get("brier")
                entry["m6_brier"] = (raw.get("m6") or {}).get("brier")
            models[key] = entry
        out["leagues"][lg] = models
    out["dependencies"] = {
        "team_scoring": "team_td_v1",
        "player_opportunities": ["player_td_v1 shares", "m6 features/rates"],
        "player_td_probability": ["m3", "m4", "m5", "m6", "player_td_v1"],
        "correlated_simulation": "atd_sim_v1 (uses team_td_v1 + m6-weighted shares)",
        "calibration": "Platt (global / by position) across families",
    }
    out["overlap"] = {
        "player_probability_lanes": [
            "M3 hierarchical",
            "M4 feature betas",
            "M5 rolling Poisson",
            "M6 hist_gbm study / shrink serve",
            "Player TD v1 team-λ allocator",
        ],
        "team_scoring": ["team_td_v1 (canonical)", "implied M3 team_rate×skill (legacy proxy)"],
        "simulation": ["atd_sim_v1 correlated engine (canonical for joints)"],
    }
    return out


def _m5_predict_row(art: dict[str, Any], rec: Any) -> float:
    feats = {}
    for name in art.get("features") or []:
        if hasattr(rec, name):
            val = getattr(rec, name)
            if val is not None and np.isfinite(float(val) if not isinstance(val, (str, bool)) else np.nan):
                try:
                    feats[name] = float(val)
                except (TypeError, ValueError):
                    pass
    pred = predict_anytime_td_m5(
        art,
        player_id=str(rec.player_id),
        team_id=str(rec.team_id or ""),
        position_group=str(rec.position_group),
        opp_def_z=float(getattr(rec, "opp_def_z", 0.0) or 0.0),
        features=feats,
    )
    return float(pred["p"])


def _m4_predict_frame(train: pd.DataFrame, test: pd.DataFrame, league_id: str) -> np.ndarray:
    m4 = load_m4_artifacts(league_id)
    market = (m4.get("markets") or {}).get("anytime_td")
    if not market:
        return np.full(len(test), float((train["anytime_tds"] > 0).mean() if len(train) else 0.15))
    base_tr, base_te = shrinkage_mean(train, test, "anytime_tds", league_id)
    _, m3_te, _ = opponent_adjustment(train, test, base_tr, base_te, "anytime_tds")
    ps = []
    for i, rec in enumerate(test.itertuples(index=False)):
        feats: dict[str, float | None] = {}
        for name in market.get("features") or []:
            feats[name] = getattr(rec, name, None) if hasattr(rec, name) else None
            if feats[name] is not None:
                try:
                    feats[name] = float(feats[name])  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    feats[name] = None
        mean, _missing = adjust_mean(float(m3_te[i]), feats, market)
        ps.append(probability_from_mean(mean, market, line=0.5, side="yes"))
    return np.asarray(ps, dtype=float)


def _player_td_v1_predict(test: pd.DataFrame, league_id: str, train: pd.DataFrame) -> np.ndarray:
    model = load_player_td_model(league_id)
    if model is None:
        return np.full(len(test), float((train["anytime_tds"] > 0).mean() if len(train) else 0.15))
    # Ensure shares/lambdas present.
    part = test.copy()
    if "rush_td_share" not in part.columns:
        w = fit_share_weights(train, league_id)
        part = attach_shares(part, w)
        part["lambda_rush_td"] = part["team_lambda_rush_td"] * part["rush_td_share"]
        part["lambda_rec_td"] = part["team_lambda_pass_td"] * part["rec_td_share"]
        part["lambda_player_td"] = part["lambda_rush_td"] + part["lambda_rec_td"]
    pred = predict_frame(model, part)
    return pred["p_atd"].to_numpy(dtype=float)


def _ensure_m6_roll_features(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.sort_values(["player_id", "kickoff_utc"]).copy()
    if "anytime_tds" not in out.columns:
        out["anytime_tds"] = out["rush_td"].fillna(0) + out["rec_td"].fillna(0)
    g = out.groupby("player_id", sort=False)
    if "roll5_td_rate" not in out.columns:
        out["roll5_td_rate"] = g["anytime_tds"].transform(lambda s: s.shift(1).rolling(5, min_periods=1).mean())
    if "roll3_carries" not in out.columns and "carries" in out.columns:
        out["roll3_carries"] = g["carries"].transform(lambda s: s.shift(1).rolling(3, min_periods=1).mean())
    if "roll3_td_rate" not in out.columns:
        out["roll3_td_rate"] = g["anytime_tds"].transform(lambda s: s.shift(1).rolling(3, min_periods=1).mean())
    return out


def compare_league(
    session: Session,
    league_id: str,
    *,
    shadow_days: int = 45,
    max_games: int = 80,
    draw_count: int = 1_500,
    seed: int = 42,
) -> dict[str, Any]:
    print(f"[integration] building frame {league_id}…", flush=True)
    frame, _weights = build_player_td_frame(session, league_id, year_max=2026, universe="corrected")
    if frame.empty:
        return {"league_id": league_id, "status": "no_data"}

    frame = frame.copy()
    frame["kickoff_utc"] = pd.to_datetime(frame["kickoff_utc"], utc=True)
    frame = _ensure_m6_roll_features(frame)
    kick = frame["kickoff_utc"]
    shadow_start = kick.max() - pd.Timedelta(days=shadow_days)
    train = frame.loc[kick < shadow_start].copy()
    test = frame.loc[kick >= shadow_start].copy()
    if len(test) < 80:
        cut = int(len(frame) * 0.9)
        train = frame.iloc[:cut].copy()
        test = frame.iloc[cut:].copy()

    game_ids = list(dict.fromkeys(test["game_id"].astype(str).tolist()))
    if max_games is not None:
        game_ids = game_ids[-max_games:]
    test = test[test["game_id"].astype(str).isin(game_ids)].copy().sort_values(["kickoff_utc", "game_id", "player_id"])

    print(f"[integration] scoring sim+baselines {league_id} games={len(game_ids)} rows={len(test)}…", flush=True)
    scored = score_games(
        frame,
        league_id,
        game_ids,
        draw_count=draw_count,
        seed=seed,
        progress_every=20,
        label="integration",
    )
    y = scored["y"]
    meta = scored["meta"]
    models_p: dict[str, np.ndarray] = {
        "atd_sim_v1": scored["models"]["atd_sim_v1"],
        "m3": scored["models"]["m3"],
        "m5_shrink": scored["models"]["m5"],
        "m6_shrink": scored["models"]["m6"],
    }

    # Align test frame to meta order for row-wise predictors.
    test_keyed = test.copy()
    test_keyed["game_id"] = test_keyed["game_id"].astype(str)
    test_keyed["player_id"] = test_keyed["player_id"].astype(str)
    ordered = meta.merge(
        test_keyed,
        on=["game_id", "player_id"],
        how="left",
        suffixes=("", "_dup"),
    )

    # M5 feature path (row dict avoids itertuples issues with merge duplicate cols)
    m5 = load_m5(league_id)
    if m5:
        m5_ps = []
        for i in range(len(ordered)):
            row = ordered.iloc[i]
            try:
                feats = {}
                for name in m5.get("features") or []:
                    if name not in ordered.columns:
                        continue
                    val = row.get(name)
                    try:
                        fval = float(val)
                    except (TypeError, ValueError):
                        continue
                    if np.isfinite(fval):
                        feats[name] = fval
                pred = predict_anytime_td_m5(
                    m5,
                    player_id=str(row.get("player_id")),
                    team_id=str(row.get("team_id") or ""),
                    position_group=str(row.get("position_group") or "UNK"),
                    opp_def_z=float(row.get("opp_def_z") or 0.0) if np.isfinite(float(row.get("opp_def_z") or 0.0)) else 0.0,
                    features=feats,
                )
                m5_ps.append(float(pred["p"]))
            except Exception:
                m5_ps.append(float("nan"))
        arr = np.asarray(m5_ps, dtype=float)
        if not np.isfinite(arr).all():
            # Fall back to shrink path where feature path failed.
            arr = np.where(np.isfinite(arr), arr, models_p["m5_shrink"])
        models_p["m5"] = arr
    else:
        models_p["m5"] = models_p["m5_shrink"]

    # M4 proper
    models_p["m4"] = _m4_predict_frame(train, ordered, league_id)

    # Player TD v1
    models_p["player_td_v1"] = _player_td_v1_predict(ordered, league_id, train)

    # M6 hist_gbm refit in-memory (matches study family; does not write artifact)
    m6_art = load_m6(league_id)
    feats = list((m6_art or {}).get("features") or [])
    notes_gbm: list[str] | None = None
    if feats:
        gbm = fit_hist_gbm(train, ordered, league_id, feats)
        models_p["m6_hist_gbm"] = np.asarray(gbm.p_test, dtype=float)
        notes_gbm = list(gbm.notes)

    # Served M6 path: shrinkage rates + Platt (what atd_sim uses today)
    m6_direct = []
    for r in ordered.itertuples(index=False):
        if m6_art:
            p6, _ = _m6_predict_simple(m6_art, str(r.player_id), str(r.position_group))
            m6_direct.append(float(p6 if p6 is not None else 0.15))
        else:
            m6_direct.append(0.15)
    models_p["m6"] = np.asarray(m6_direct, dtype=float)

    # Ensembles (equal-weight probability averages; no leakage from test-tuned weights)
    def avg(*names: str) -> np.ndarray:
        stack = np.column_stack([models_p[n] for n in names if n in models_p])
        return np.clip(stack.mean(axis=1), 1e-6, 1 - 1e-6)

    ensembles = {
        "ens_m6_player_td": avg("m6", "player_td_v1"),
        "ens_m6_gbm_player_td": avg("m6_hist_gbm", "player_td_v1") if "m6_hist_gbm" in models_p else avg("m6", "player_td_v1"),
        "ens_m6_sim": avg("m6", "atd_sim_v1"),
        "ens_player_td_sim": avg("player_td_v1", "atd_sim_v1"),
        "ens_m6_player_td_sim": avg("m6", "player_td_v1", "atd_sim_v1"),
        "ens_m5_m6_player_td": avg("m5", "m6", "player_td_v1"),
    }
    models_p.update(ensembles)

    summaries = {
        name: summarize_predictions(y, p, meta, n_boot=300, seed=seed + i)
        for i, (name, p) in enumerate(models_p.items())
    }

    # Rank by Brier then log loss
    ranked = sorted(
        [
            {
                "model": name,
                "brier": body["brier"],
                "log_loss": body["log_loss"],
                "ece": body["ece"],
                "brier_ci": body["brier_ci"],
                "n": body["n"],
            }
            for name, body in summaries.items()
        ],
        key=lambda r: (r["brier"], r["log_loss"]),
    )

    best = ranked[0]["model"]
    # Ensemble vs best single (non-ens)
    singles = [r for r in ranked if not r["model"].startswith("ens_")]
    best_single = singles[0]["model"] if singles else best
    best_ens = next((r for r in ranked if r["model"].startswith("ens_")), None)
    ens_helps = bool(
        best_ens
        and best_ens["brier"] + 1e-6 < next(r["brier"] for r in singles if r["model"] == best_single)
    )

    return {
        "league_id": league_id,
        "status": "ok",
        "window": {
            "type": "recent_shadow",
            "shadow_days": shadow_days,
            "n_games": scored["n_games"],
            "n_rows": int(len(y)),
            "max_kickoff": pd.to_datetime(test["kickoff_utc"], utc=True).max().isoformat(),
            "min_kickoff": pd.to_datetime(test["kickoff_utc"], utc=True).min().isoformat(),
            "previously_examined": True,
            "note": PREVIOUSLY_EXAMINED_NOTE,
        },
        "models": summaries,
        "ranking": ranked,
        "best_model": best,
        "best_single_model": best_single,
        "ensemble_improves_vs_best_single": ens_helps,
        "m6_hist_gbm_notes": notes_gbm,
        "draw_count": draw_count,
    }


def recommend_canonical(inventory: dict[str, Any], comparisons: dict[str, Any]) -> dict[str, Any]:
    nfl_best = (comparisons.get("nfl") or {}).get("best_single_model")
    fbs_best = (comparisons.get("fbs") or {}).get("best_single_model")
    return {
        "team_scoring": {
            "canonical": "team_td_v1",
            "retire_or_demote": ["M3 team_rate×4 proxy"],
            "reason": "Dedicated offensive TD distributions with rush/pass split; already consumed by Player TD v1 and atd_sim.",
        },
        "player_opportunities": {
            "canonical": "Unified opportunity feature layer (priors + NFL RZ/GL; FBS without invented RZ/GL)",
            "overlap": ["player_td_v1 shares.py", "m6 feature study", "rolling_td / advanced priors"],
            "reason": "Same prior columns are rebuilt in multiple lanes; keep one feature builder.",
        },
        "player_td_probability": {
            "nfl_research_leader": nfl_best,
            "fbs_research_leader": fbs_best,
            "canonical_path": (
                "Serve research forecasts from the fair-comparison winner per league; "
                "keep M3 as production baseline until promotion gates pass."
            ),
            "consolidate": ["Avoid parallel M5 + M6 + player_td_v1 production serving"],
        },
        "correlated_simulation": {
            "canonical": "atd_sim_v1 (simulate_correlated_atd)",
            "do_not_rebuild": True,
            "role": "Joint/team-consistent TD paths; use for parlays and team-budget coherence checks",
        },
        "calibration": {
            "canonical": "Train-only Platt (global + optional by position)",
            "uncertainty": "Poisson±√λ / availability mixture from Player TD v1; sim empirical quantiles for joints",
        },
    }


def run_integration_research(
    session: Session,
    *,
    now: datetime | None = None,
    max_games: int = 80,
    draw_count: int = 1_500,
) -> dict[str, Any]:
    moment = as_utc(now) or datetime.now(UTC)
    before = snapshot_protected()
    inventory = inventory_artifacts()
    comparisons: dict[str, Any] = {}
    for lg in ("nfl", "fbs"):
        comparisons[lg] = compare_league(session, lg, max_games=max_games, draw_count=draw_count)

    # After snapshots — ensure protected unchanged
    after = snapshot_protected()
    protected_ok = before == {k: after.get(k) for k in before}

    report = {
        "status": "ok",
        "phase": "integration_research",
        "as_of": moment.isoformat(),
        "play_enabled": False,
        "promoted": False,
        "validated_play": False,
        "research_only": True,
        "did_not": [
            "promote_models",
            "rebuild_simulator",
            "modify_live_database_forecasts",
            "overwrite_protected_artifacts",
        ],
        "protected_artifacts_unchanged": protected_ok,
        "1_existing_model_inventory": inventory,
        "2_fair_comparison_results": comparisons,
        "3_best_nfl_touchdown_model": (comparisons.get("nfl") or {}).get("best_model"),
        "3b_best_nfl_single": (comparisons.get("nfl") or {}).get("best_single_model"),
        "4_best_fbs_touchdown_model": (comparisons.get("fbs") or {}).get("best_model"),
        "4b_best_fbs_single": (comparisons.get("fbs") or {}).get("best_single_model"),
        "5_ensemble_improves": {
            "nfl": (comparisons.get("nfl") or {}).get("ensemble_improves_vs_best_single"),
            "fbs": (comparisons.get("fbs") or {}).get("ensemble_improves_vs_best_single"),
        },
        "6_overlapping_components_to_consolidate": recommend_canonical(inventory, comparisons),
        "7_remaining_prediction_weaknesses": [
            "M6 GBM is not serialized — live/sim use shrinkage rates, so offline hist_gbm metrics are not the served model.",
            "Shadow windows were previously examined in Phase 2/3/M6/atd_sim reports.",
            "FBS 2026 PBP missing; FBS comparisons use ≤2025 history / recent completed with box scores.",
            "FBS availability often unknown; never assumed healthy.",
            "Simulations trade some marginal Brier for team-budget coherence — evaluate joints separately.",
            "Upcoming opp_def_z still thin on some live rows.",
        ],
        "8_recommended_integration_plan": {
            "near_term": [
                "Keep M3 as untouched production baseline; Validated Play off.",
                "Canonical team scoring = team_td_v1.",
                "Canonical joint engine = existing atd_sim_v1 (do not rebuild).",
                "Pick one player probability research leader per league from this comparison; serialize that model for serving research forecasts only.",
                "If M6 hist_gbm wins research, persist a loadable GBM artifact so sim/serving match evaluation.",
                "Unify opportunity feature construction to one module used by Player TD v1 and M6.",
            ],
            "before_promotion": [
                "Prospective shadow on never-examined future games",
                "Sportsbook calibration / EV with complete injury coverage",
                "Confirm ensemble lift with train-fit blend weights (not test-tuned)",
            ],
        },
    }
    path = report_path()
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    print(f"[integration] WROTE {path}", flush=True)
    return report
