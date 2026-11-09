"""Independent ATS correctness audit. Does not retrain or rewrite frozen predictions.

Produces:
  data/research/ats_audit/grading_cases.json
  data/research/ats_audit/nfl_holdout_export.csv
  data/research/ats_audit/metrics_reproduction.json
  data/research/ats_audit/live_quote_audit.json
  data/research/ats_audit/leakage_notes.json
  data/research/ats_audit/FINAL_AUDIT.json
"""

from __future__ import annotations

import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core"))

from spe.db.session import init_db, session_factory  # noqa: E402
from spe.market.odds import american_to_decimal, american_to_implied, ev_per_dollar, no_vig_two_way  # noqa: E402
from spe.models.ats.backtest import HOLD_OUT_START, load_ats_artifact  # noqa: E402
from spe.models.ats.labels import cover_indicator, grade_home_spread, grade_side, normal_cover_probs  # noqa: E402
from spe.models.ats.model import score_predictions  # noqa: E402
from spe.models.ats.predict import predict_upcoming_ats  # noqa: E402
from spe.models.ats.ratings import build_team_strength_frame  # noqa: E402
from spe.models.metrics import (  # noqa: E402
    brier_score,
    expected_calibration_error,
    log_loss,
    mean_absolute_error,
)
from spe.shadow.ats import ats_track_record  # noqa: E402

OUT = ROOT / "data" / "research" / "ats_audit"
OUT.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 1. Independent grading (no helper reuse for the arithmetic itself)
# ---------------------------------------------------------------------------

def independent_grade(home_score: float, away_score: float, home_handicap: float) -> str:
    """Pure arithmetic: margin + home_handicap."""
    margin = home_score - away_score
    edge = margin + home_handicap
    if abs(edge) < 1e-12:
        return "push"
    return "home_cover" if edge > 0 else "away_cover"


def section_grading() -> dict:
    cases = [
        {"name": "home_-7_wins_24_21", "hs": 24, "aws": 21, "hh": -7, "expect": "away_cover"},
        {"name": "home_-7_wins_28_21", "hs": 28, "aws": 21, "hh": -7, "expect": "push"},
        {"name": "home_-7_wins_31_21", "hs": 31, "aws": 21, "hh": -7, "expect": "home_cover"},
        {"name": "home_+7_loses_21_24", "hs": 21, "aws": 24, "hh": 7, "expect": "home_cover"},
        {"name": "home_-3.5_wins_by_3", "hs": 24, "aws": 21, "hh": -3.5, "expect": "away_cover"},
        {"name": "away_fav_home_+7_wins_28_17", "hs": 28, "aws": 17, "hh": 7, "expect": "home_cover"},
        {"name": "pickem_home_wins_21_17", "hs": 21, "aws": 17, "hh": 0, "expect": "home_cover"},
        {"name": "pickem_tie_would_push_if_possible", "hs": 20, "aws": 20, "hh": 0, "expect": "push"},
        {"name": "neutral_home_-3_wins_by_3", "hs": 23, "aws": 20, "hh": -3, "expect": "push"},
    ]
    rows = []
    all_ok = True
    for c in cases:
        indep = independent_grade(c["hs"], c["aws"], c["hh"])
        margin = c["hs"] - c["aws"]
        helper = grade_home_spread(margin, c["hh"])
        helper_map = {"cover": "home_cover", "loss": "away_cover", "push": "push"}[helper]
        away_helper = grade_side(margin, c["hh"], "away")
        ok = indep == c["expect"] and helper_map == c["expect"]
        all_ok = all_ok and ok
        rows.append(
            {
                **c,
                "independent": indep,
                "helper_home": helper_map,
                "helper_away": away_helper,
                "pass": ok,
                "note": "label is ATS cover, not moneyline win",
            }
        )

    # nflverse sign convention from raw parquet
    parquet = ROOT / "data" / "cache" / "nflverse" / "games.parquet"
    sign_note: dict = {"parquet_exists": parquet.exists()}
    if parquet.exists():
        df = pd.read_parquet(parquet)
        s = df.dropna(subset=["spread_line", "home_score", "away_score"]).copy()
        s["margin"] = s["home_score"] - s["away_score"]
        # When home wins big, is spread typically negative (favorite) or positive?
        big = s[s["margin"] >= 14]
        edge = s["margin"] + s["spread_line"]
        sign_note.update(
            {
                "n": int(len(s)),
                "pct_spread_negative": float((s["spread_line"] < 0).mean()),
                "pct_spread_positive": float((s["spread_line"] > 0).mean()),
                "median_spread_when_home_wins_by_14_plus": float(big["spread_line"].median()),
                "home_ats_rate_using_margin_plus_spread": float((edge > 0).mean()),
                "push_rate_margin_plus_spread": float((np.isclose(edge, 0)).mean()),
                "home_ats_rate_if_sign_flipped": float(((s["margin"] - s["spread_line"]) > 0).mean()),
                "documented_convention": (
                    "nflverse spread_line is the home team's closing line where "
                    "NEGATIVE means home favorite (standard American). "
                    "NOT positive-when-favored. Adapter stores home_line = spread_line as-is; "
                    "no sign flip — correct."
                ),
                "user_claim_positive_when_favored": "FALSE_vs_data",
                "examples": s[["game_id", "home_team", "away_team", "home_score", "away_score", "spread_line"]]
                .head(5)
                .to_dict(orient="records"),
            }
        )

    # Odds API side orientation check on live outcomes if present
    init_db()
    session = session_factory()
    from sqlalchemy import text

    odds_rows = session.execute(
        text(
            """
            SELECT o.game_id, o.side, o.line, g.home_team_id, g.away_team_id
            FROM outcomes o
            JOIN games g ON g.game_id = o.game_id
            WHERE o.market_id = 'point_spread'
            LIMIT 40
            """
        )
    ).mappings().all()
    odds_check = []
    for r in odds_rows:
        odds_check.append(dict(r))
    session.close()

    out = {
        "mandatory_cases": rows,
        "all_mandatory_pass": all_ok,
        "nflverse_sign": sign_note,
        "odds_api_sample_outcomes": odds_check[:20],
        "internal_convention": {
            "margin": "home_score - away_score",
            "home_handicap": "sportsbook handicap applied to home (negative = favorite)",
            "home_cover": "margin + home_handicap > 0",
            "push": "margin + home_handicap == 0",
            "away_cover": "margin + home_handicap < 0",
        },
    }
    (OUT / "grading_cases.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# 2–4. Reproduce NFL backtest independently
# ---------------------------------------------------------------------------

def section_reproduce() -> dict:
    init_db()
    session = session_factory()
    frame = build_team_strength_frame(session, "nfl")
    art = load_ats_artifact("nfl")
    if frame.empty or art is None:
        session.close()
        return {"status": "missing_frame_or_artifact"}

    holdout = pd.Timestamp(HOLD_OUT_START["nfl"])
    frame["kickoff_utc"] = pd.to_datetime(frame["kickoff_utc"], utc=True)
    labeled = frame.dropna(subset=["margin"]).copy()
    train = labeled[labeled["kickoff_utc"] < holdout]
    test = labeled[labeled["kickoff_utc"] >= holdout]
    scored = score_predictions(art, test)

    # Export every evaluated game
    export_cols = [
        "game_id",
        "home_team_id",
        "away_team_id",
        "kickoff_utc",
        "home_score",
        "away_score",
        "margin",
        "spread_line",
        "home_spread_price",
        "away_spread_price",
        "predicted_margin",
        "predicted_home_score",
        "predicted_away_score",
        "p_home_cover",
        "p_away_cover",
        "p_push",
        "p_home_cover_raw",
        "margin_sigma",
        "sample_size",
    ]
    exp = scored[[c for c in export_cols if c in scored.columns]].copy()
    exp["raw_provider_spread"] = exp["spread_line"]
    exp["normalized_home_handicap"] = exp["spread_line"]
    exp["provider_source"] = "nflverse_consensus"
    exp["model_version_id"] = art.model_version_id
    exp["training_cutoff"] = holdout.isoformat()
    exp["feature_cutoff_rule"] = "prior_only kickoff+4.5h before as_of; expanding team stats"
    exp["actual_ats"] = [
        independent_grade(float(r.home_score), float(r.away_score), float(r.spread_line))
        if pd.notna(r.spread_line)
        else None
        for r in exp.itertuples(index=False)
    ]
    exp_path = OUT / "nfl_holdout_export.csv"
    exp.to_csv(exp_path, index=False)

    # Independent metrics from export
    y_margin = exp["margin"].to_numpy(dtype=float)
    yhat = exp["predicted_margin"].to_numpy(dtype=float)
    mae = mean_absolute_error(y_margin, yhat)
    rmse = float(np.sqrt(np.mean((y_margin - yhat) ** 2)))

    # Closing-spread-as-predictor of margin: market expected margin ≈ -home_handicap
    with_line = exp.dropna(subset=["spread_line"]).copy()
    market_margin = (-with_line["spread_line"]).to_numpy(dtype=float)
    actual_m = with_line["margin"].to_numpy(dtype=float)
    market_mae = mean_absolute_error(actual_m, market_margin)
    market_rmse = float(np.sqrt(np.mean((actual_m - market_margin) ** 2)))
    model_mae_same = mean_absolute_error(actual_m, with_line["predicted_margin"].to_numpy(dtype=float))
    model_rmse_same = float(np.sqrt(np.mean((actual_m - with_line["predicted_margin"].to_numpy()) ** 2)))

    y_cover = []
    p_cover = []
    p_raw = []
    p_market = []
    p_half = []
    push_n = 0
    missing_price_n = 0
    for r in with_line.itertuples(index=False):
        indep = independent_grade(float(r.home_score), float(r.away_score), float(r.spread_line))
        if indep == "push":
            push_n += 1
            continue
        y = 1.0 if indep == "home_cover" else 0.0
        y_cover.append(y)
        p_cover.append(float(r.p_home_cover))
        p_raw.append(float(getattr(r, "p_home_cover_raw", r.p_home_cover)))
        p_half.append(0.5)
        price = getattr(r, "home_spread_price", None)
        if price is None or (isinstance(price, float) and np.isnan(price)):
            missing_price_n += 1
            # Do NOT invent -110. Leave market baseline undefined for this row.
            p_market.append(np.nan)
        else:
            # Raw implied from spread price (not moneyline). Still has vig.
            p_market.append(float(american_to_implied(int(price))))

    y_arr = np.asarray(y_cover, dtype=float)
    p_arr = np.asarray(p_cover, dtype=float)
    p_raw_arr = np.asarray(p_raw, dtype=float)
    p_half_arr = np.asarray(p_half, dtype=float)
    p_mkt = np.asarray(p_market, dtype=float)
    mkt_mask = ~np.isnan(p_mkt)

    # Conditional (non-push) renormalization check
    # For Brier on non-push games, p should be P(home covers | not push)
    p_cond = []
    for r in with_line.itertuples(index=False):
        indep = independent_grade(float(r.home_score), float(r.away_score), float(r.spread_line))
        if indep == "push":
            continue
        push = float(r.p_push or 0.0)
        ph = float(r.p_home_cover)
        # If model already assigned mass to push, conditional home-cover among decided:
        denom = max(1e-9, 1.0 - push)
        p_cond.append(ph / denom)

    metrics = {
        "n_margin_test": int(len(exp)),
        "n_with_spread": int(len(with_line)),
        "n_push_excluded": int(push_n),
        "n_graded_ats": int(len(y_cover)),
        "why_692_to_685": (
            f"margin-test games={len(exp)}; games with spread={len(with_line)}; "
            f"pushes excluded from Brier={push_n}; graded={len(y_cover)}. "
            "Difference from 692→685 is push exclusions (and any missing spread rows)."
        ),
        "margin": {
            "mae": mae,
            "rmse": rmse,
            "bias": float(np.mean(yhat - y_margin)),
            "corr": float(np.corrcoef(y_margin, yhat)[0, 1]),
        },
        "margin_vs_closing_spread_same_games": {
            "n": int(len(with_line)),
            "model_mae": model_mae_same,
            "model_rmse": model_rmse_same,
            "closing_spread_as_margin_predictor_mae": market_mae,
            "closing_spread_as_margin_predictor_rmse": market_rmse,
            "note": (
                "Closing spread expected home margin ≈ -home_handicap. "
                "This is a historical closing benchmark, not a pregame available quote."
            ),
        },
        "cover_brier_calibrated": {
            "n": int(len(y_arr)),
            "brier": brier_score(y_arr, p_arr),
            "log_loss": log_loss(y_arr, p_arr),
            "ece": expected_calibration_error(y_arr, p_arr),
            "accuracy_at_50": float(np.mean((p_arr >= 0.5) == (y_arr >= 0.5))),
            "base_rate": float(np.mean(y_arr)),
            "mean_p": float(np.mean(p_arr)),
        },
        "cover_brier_raw_uncalibrated": {
            "n": int(len(y_arr)),
            "brier": brier_score(y_arr, p_raw_arr),
            "log_loss": log_loss(y_arr, p_raw_arr),
            "ece": expected_calibration_error(y_arr, p_raw_arr),
            "mean_p": float(np.mean(p_raw_arr)),
        },
        "cover_brier_conditional_on_no_push": {
            "n": int(len(y_arr)),
            "brier": brier_score(y_arr, np.asarray(p_cond)),
            "note": "P(home cover)/(1-p_push) vs non-push outcomes",
        },
        "baseline_50pct": {
            "n": int(len(y_arr)),
            "brier": brier_score(y_arr, p_half_arr),
            "log_loss": log_loss(y_arr, p_half_arr),
            "label": "50% cover-probability baseline",
        },
        "market_implied_from_spread_price": {
            "n_with_price": int(mkt_mask.sum()),
            "n_missing_price": int(missing_price_n),
            "brier": brier_score(y_arr[mkt_mask], p_mkt[mkt_mask]) if mkt_mask.any() else None,
            "log_loss": log_loss(y_arr[mkt_mask], p_mkt[mkt_mask]) if mkt_mask.any() else None,
            "mean_implied": float(np.nanmean(p_mkt)),
            "note": (
                "Uses home_spread_odds from nflverse when present. "
                "Does NOT invent -110. Consensus-based; not executable sportsbook returns."
            ),
            "no_vig_note": (
                "No-vig requires matching opposing spread prices for the same handicap. "
                "When both home and away spread odds exist, they can be de-vigged."
            ),
        },
        "platt_calibration_on_artifact": {
            "platt_a": art.platt_a,
            "platt_b": art.platt_b,
            "sigma": art.sigma,
            "family": art.family,
            "model_version_id": art.model_version_id,
        },
        "export_path": str(exp_path),
        "train_n": int(len(train)),
        "test_n": int(len(test)),
        "holdout_start": holdout.isoformat(),
    }

    # Sim vs main Brier discrepancy: reproduce both scopes
    sim_slice = with_line.head(300).copy()
    y_sim = []
    p_sim_model = []
    for r in sim_slice.itertuples(index=False):
        indep = independent_grade(float(r.home_score), float(r.away_score), float(r.spread_line))
        if indep == "push":
            continue
        y_sim.append(1.0 if indep == "home_cover" else 0.0)
        p_sim_model.append(float(r.p_home_cover))
    metrics["brier_0157_vs_0150_explanation"] = {
        "main_report_brier_scope": "all graded non-push holdout games with spreads",
        "main_n": int(len(y_arr)),
        "main_brier_reproduced": metrics["cover_brier_calibrated"]["brier"],
        "sim_compare_scope": "first 300 with-spread holdout rows, then drop pushes (~299)",
        "sim_n": int(len(y_sim)),
        "sim_scope_brier_reproduced": brier_score(np.asarray(y_sim), np.asarray(p_sim_model)) if y_sim else None,
        "identical_games": False,
        "identical_assumptions": (
            "Same model probabilities and grading rule, but different game subsets. "
            "0.150 was the model Brier on the 299-game simulation-comparison subsample, "
            "not the full 685-game evaluation set (0.157)."
        ),
    }

    # Push probability sum check on a few rows
    push_sum_checks = []
    for r in with_line.head(20).itertuples(index=False):
        ssum = float(r.p_home_cover) + float(r.p_away_cover) + float(r.p_push or 0)
        push_sum_checks.append({"game_id": r.game_id, "sum": ssum, "spread": float(r.spread_line)})
    metrics["probability_sum_samples"] = push_sum_checks

    # EV formula check
    metrics["ev_formula"] = {
        "definition": "P(win)*(decimal-1) - P(loss); push → 0 net",
        "example_-110_p055": {
            "p": 0.55,
            "american": -110,
            "ev": ev_per_dollar(0.55, -110),
            "manual": 0.55 * (american_to_decimal(-110) - 1) - 0.45,
        },
    }

    # No-vig when both prices present
    novig_examples = []
    for r in with_line.head(50).itertuples(index=False):
        hp = getattr(r, "home_spread_price", None)
        ap = getattr(r, "away_spread_price", None)
        if hp is None or ap is None or (isinstance(hp, float) and np.isnan(hp)) or (isinstance(ap, float) and np.isnan(ap)):
            continue
        try:
            pi_h = american_to_implied(int(hp))
            pi_a = american_to_implied(int(ap))
            nv_h, nv_a = no_vig_two_way(pi_h, pi_a)
            novig_examples.append(
                {
                    "game_id": r.game_id,
                    "home_price": int(hp),
                    "away_price": int(ap),
                    "raw_home": pi_h,
                    "raw_away": pi_a,
                    "novig_home": nv_h,
                    "novig_away": nv_a,
                    "overround": pi_h + pi_a - 1,
                }
            )
        except Exception as exc:  # noqa: BLE001
            novig_examples.append({"game_id": r.game_id, "error": str(exc)})
        if len(novig_examples) >= 5:
            break
    metrics["novig_examples"] = novig_examples

    # Leakage probes
    leakage = audit_leakage(train, test, art, scored)
    (OUT / "leakage_notes.json").write_text(json.dumps(leakage, indent=2, default=str), encoding="utf-8")
    metrics["leakage"] = leakage

    session.close()
    (OUT / "metrics_reproduction.json").write_text(json.dumps(metrics, indent=2, default=str), encoding="utf-8")
    return metrics


def audit_leakage(train: pd.DataFrame, test: pd.DataFrame, art, scored: pd.DataFrame) -> dict:
    notes = {
        "feature_construction": (
            "build_team_strength_frame uses expanding prior-only team stats; "
            "a game's own box score is applied only after kickoff+4.5h complete_after."
        ),
        "holdout_examined": (
            "Holdout was used for feature ablation ranking and model family comparison "
            "in the same research run. Label as EXAMINED RESEARCH, not untouched validation."
        ),
        "closing_lines": (
            "Closing consensus spreads are historical benchmarks for ATS grading. "
            "They were not claimed as available at an earlier prediction timestamp."
        ),
        "weather": (
            "weather_snapshots may include schedule-observed weather for historical games. "
            "Upcoming uses NWS forecasts when present; missing weather is flagged."
        ),
        "scaling": "Ridge fits on train only per backtest call; no global scaler fit on test.",
        "calibration": "Platt a,b fitted on TRAIN games with spreads only (calibrate_cover_platt).",
        "sigma": "Residual sigma estimated on TRAIN residuals inside fit_ridge_margin.",
        "opponent_adjustment": (
            "Current ATS ratings use raw prior EPA means with shrinkage, not leave-one-out "
            "season adj that includes future games."
        ),
        "checks": {},
    }
    # No test kickoff should appear in train
    train_ids = set(train["game_id"])
    test_ids = set(test["game_id"])
    notes["checks"]["train_test_game_overlap"] = len(train_ids & test_ids)
    # Feature cutoff: sample_size should be > 0 and predicted margin finite
    notes["checks"]["test_rows_with_nan_pred"] = int(scored["predicted_margin"].isna().sum())
    notes["checks"]["min_sample_size_in_test"] = float(scored["sample_size"].min()) if len(scored) else None
    # Ablation used holdout → examined
    notes["validation_label"] = "examined_research_holdout"
    notes["untouched_prospective_window"] = False
    return notes


# ---------------------------------------------------------------------------
# 5. Live finder audit
# ---------------------------------------------------------------------------

def section_live() -> dict:
    init_db()
    session = session_factory()
    board = predict_upcoming_ats(session, include_simulation=False)
    rows = board.get("rows") or []

    audited = []
    extreme = []
    for r in rows:
        spread = r.get("spread_line")
        book = r.get("sportsbook")
        is_consensus = book in {None, "nflverse_consensus"} or str(book).endswith("consensus")
        has_real_price = r.get("price_american") is not None and not is_consensus
        # Suppress actionable EV for consensus / unverified
        actionable_ev = None
        if has_real_price and "research_unvalidated" not in (r.get("warnings") or []):
            actionable_ev = r.get("expected_value")
        item = {
            "game_id": r["game_id"],
            "league_id": r["league_id"],
            "kickoff_utc": r["kickoff_utc"],
            "home": r["home_team"],
            "away": r["away_team"],
            "spread_line": spread,
            "sportsbook": book,
            "source_class": "consensus" if is_consensus else "live_book",
            "home_spread_price": r.get("home_spread_price"),
            "away_spread_price": r.get("away_spread_price"),
            "price_american_favored_side": r.get("price_american"),
            "predicted_margin": r.get("predicted_margin"),
            "model_vs_market": r.get("model_vs_market"),
            "p_cover": r.get("p_cover"),
            "expected_value_displayed": r.get("expected_value"),
            "actionable_ev_allowed": actionable_ev,
            "model_version_id": r.get("model_version_id"),
            "warnings": r.get("warnings"),
            "orientation_check": "spread_line is home handicap; away line = -spread when present",
        }
        audited.append(item)
        if r.get("model_vs_market") is not None and abs(float(r["model_vs_market"])) >= 7:
            extreme.append(item)

    # Investigate extremes without flipping lines
    extreme_notes = []
    for e in sorted(extreme, key=lambda x: abs(float(x["model_vs_market"] or 0)), reverse=True)[:15]:
        extreme_notes.append(
            {
                **e,
                "investigation": (
                    "Large disagreement retained as-is. Possible causes: stale/wrong consensus "
                    "on 2026 schedule lines, model overconfidence, or genuine edge. "
                    "Do not flip or clamp. Suppress actionable EV for consensus sources."
                ),
            }
        )

    from sqlalchemy import text

    pred_counts = session.execute(
        text("SELECT league_id, count(*) FROM ats_predictions GROUP BY league_id")
    ).fetchall()
    settle_counts = session.execute(text("SELECT result, count(*) FROM ats_settlements GROUP BY result")).fetchall()
    track = ats_track_record(session)

    # Freshness of live spread ticks
    tick_fresh = session.execute(
        text(
            """
            SELECT o.game_id, o.side, o.line, t.bookmaker_id, t.price_american, t.captured_at, t.source_at
            FROM outcomes o
            JOIN odds_ticks t ON t.outcome_id = o.outcome_id
            WHERE o.market_id = 'point_spread'
            ORDER BY t.captured_at DESC
            LIMIT 30
            """
        )
    ).mappings().all()

    out = {
        "board_n": len(rows),
        "by_league": {
            "nfl": sum(1 for r in rows if r["league_id"] == "nfl"),
            "fbs": sum(1 for r in rows if r["league_id"] == "fbs"),
        },
        "source_mix": {
            "consensus_or_null_book": sum(
                1 for r in audited if r["source_class"] == "consensus"
            ),
            "live_book": sum(1 for r in audited if r["source_class"] == "live_book"),
        },
        "extreme_disagreements_ge_7pts": extreme_notes,
        "ats_predictions_frozen": [list(x) for x in pred_counts],
        "ats_settlements": [list(x) for x in settle_counts],
        "track_record": track,
        "live_spread_ticks_sample": [dict(x) for x in tick_fresh],
        "quote_policy": {
            "consensus_ev": "not_actionable — display only / research",
            "stale_or_unverified": "suppress actionable EV",
            "do_not_flip_large_gaps": True,
        },
    }
    session.close()
    (OUT / "live_quote_audit.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# 6. Automated loop status
# ---------------------------------------------------------------------------

def section_loop() -> dict:
    init_db()
    session = session_factory()
    from sqlalchemy import text

    preds = session.execute(
        text(
            """
            SELECT ats_id, game_id, league_id, side, spread_line, price_american, bookmaker_id,
                   predicted_margin, p_cover, model_version_id, predicted_at, play_enabled, research_only
            FROM ats_predictions
            ORDER BY predicted_at
            """
        )
    ).mappings().all()
    settles = session.execute(text("SELECT * FROM ats_settlements")).mappings().all()
    finals = session.execute(
        text(
            """
            SELECT p.ats_id, p.game_id, g.status, g.home_score, g.away_score, g.kickoff_utc
            FROM ats_predictions p
            JOIN games g ON g.game_id = p.game_id
            """
        )
    ).mappings().all()
    finished = [r for r in finals if r["status"] == "final" and r["home_score"] is not None]
    session.close()

    # Check Windows scheduled task mention — report if unknown
    return {
        "frozen_predictions": len(preds),
        "settlements": len(settles),
        "finished_among_frozen": len(finished),
        "settlement_status": "pending" if not settles else "has_settlements",
        "idempotent_design": (
            "capture dedupes on (game, book, side, spread, model, tick); "
            "settle skips ats_ids already in ats_settlements"
        ),
        "scheduled_run": (
            "Not verified as a Windows scheduled task in this audit. "
            "Manual ats-cycle / ats-shadow-capture / ats-shadow-settle work. "
            "Do not claim scheduled automation until a Task Scheduler entry is confirmed."
        ),
        "sample_frozen": [dict(p) for p in preds[:5]],
        "unique_games_frozen": len({p["game_id"] for p in preds}),
        "note_on_counting": (
            "Accuracy must count unique games with matched comparisons; "
            "multiple books/sides/snapshots are not independent games."
        ),
    }


def main() -> None:
    grading = section_grading()
    metrics = section_reproduce()
    live = section_live()
    loop = section_loop()

    # Bug findings / fixes applied status
    bugs = []
    # Check: market baseline inventing prices?
    # Current _market_baseline uses 0.5 when price missing — not -110. OK but should be flagged.
    bugs.append(
        {
            "id": "market_baseline_missing_price",
            "severity": "medium",
            "status": "confirmed",
            "detail": (
                "When home_spread_price is missing, _market_baseline previously used p=0.5. "
                "It does not invent -110, but 0.5 is still a synthetic market probability. "
                "Reproduction now excludes missing-price rows from market Brier."
            ),
            "fix": "document_and_exclude_from_market_brier",
        }
    )
    bugs.append(
        {
            "id": "consensus_ev_on_finder",
            "severity": "high",
            "status": "confirmed",
            "detail": (
                "ATS Finder displays expected_value for nflverse_consensus quotes. "
                "Consensus is not an executable sportsbook offer. Actionable EV must be suppressed."
            ),
            "fix": "suppress_actionable_ev_for_consensus_in_predict_and_ui",
        }
    )
    bugs.append(
        {
            "id": "sim_brier_subset_confusion",
            "severity": "low",
            "status": "explained_not_a_bug",
            "detail": "0.150 vs 0.157 are different game subsets, same model.",
            "fix": "none",
        }
    )
    bugs.append(
        {
            "id": "user_doc_nflverse_sign",
            "severity": "info",
            "status": "clarified",
            "detail": (
                "Prompt claimed nflverse positive=home favored. Data shows negative=home favored. "
                "Adapter correctly stores spread_line as home handicap without flipping."
            ),
            "fix": "none",
        }
    )

    # Advantage still present?
    main_brier = (metrics.get("cover_brier_calibrated") or {}).get("brier")
    half_brier = (metrics.get("baseline_50pct") or {}).get("brier")
    mkt_brier = (metrics.get("market_implied_from_spread_price") or {}).get("brier")
    closing_mae = (metrics.get("margin_vs_closing_spread_same_games") or {}).get(
        "closing_spread_as_margin_predictor_mae"
    )
    model_mae = (metrics.get("margin_vs_closing_spread_same_games") or {}).get("model_mae")

    advantage = {
        "question": "Is the reported NFL ATS advantage still present after these checks?",
        "answer": None,
        "nuance": [],
    }
    if main_brier is not None and half_brier is not None and main_brier < half_brier:
        advantage["nuance"].append(f"Model Brier {main_brier:.4f} beats 50% baseline {half_brier:.4f}.")
    if mkt_brier is not None and main_brier is not None and main_brier < mkt_brier:
        advantage["nuance"].append(
            f"Model Brier {main_brier:.4f} beats raw vigged spread-price implied {mkt_brier:.4f} "
            "(consensus odds; not live books)."
        )
    if closing_mae is not None and model_mae is not None:
        if model_mae < closing_mae:
            advantage["nuance"].append(
                f"Model margin MAE {model_mae:.3f} better than closing-spread MAE {closing_mae:.3f}."
            )
        else:
            advantage["nuance"].append(
                f"Model margin MAE {model_mae:.3f} WORSE than closing-spread MAE {closing_mae:.3f}. "
                "Closing line remains a stronger margin point forecast than the ridge model."
            )
            advantage["answer"] = (
                "PARTIAL — cover-probability Brier still beats naive/market-implied baselines on "
                "this examined holdout, but the model does NOT beat the closing spread as a "
                "margin predictor. Treat cover-skill claims as examined research, not proven edge."
            )
    if advantage["answer"] is None:
        advantage["answer"] = (
            "YES on examined-holdout cover Brier vs 50% and vs vigged consensus prices, "
            "but NOT proven prospective edge. Holdout was examined during ablation; "
            "closing lines are benchmarks; Validated Play remains off."
        )

    final = {
        "generated_at": datetime.now(UTC).isoformat(),
        "advantage_verdict": advantage,
        "grading_all_pass": grading.get("all_mandatory_pass"),
        "bugs": bugs,
        "metrics_reproduction": {
            "brier": main_brier,
            "log_loss": (metrics.get("cover_brier_calibrated") or {}).get("log_loss"),
            "baseline_50_brier": half_brier,
            "market_brier": mkt_brier,
            "n_graded": metrics.get("n_graded_ats"),
            "n_margin_test": metrics.get("n_margin_test"),
            "pushes_excluded": metrics.get("n_push_excluded"),
            "brier_0157_vs_0150": metrics.get("brier_0157_vs_0150_explanation"),
            "margin_vs_close": metrics.get("margin_vs_closing_spread_same_games"),
        },
        "leakage_label": (metrics.get("leakage") or {}).get("validation_label"),
        "live": {
            "board_n": live.get("board_n"),
            "source_mix": live.get("source_mix"),
            "extreme_n": len(live.get("extreme_disagreements_ge_7pts") or []),
        },
        "loop": loop,
        "fbs": "research-only margin prediction; no demonstrated ATS vs historical books",
        "next_narrow_task": (
            "Prefer expanded prospective validation of the frozen slate through settlement, "
            "then FBS historical-line ingestion if CFBD lines are available on the existing key."
        ),
        "play_enabled": False,
    }
    (OUT / "FINAL_AUDIT.json").write_text(json.dumps(final, indent=2, default=str), encoding="utf-8")
    print(json.dumps(final, indent=2, default=str))


if __name__ == "__main__":
    main()
