"""Time-safe test: prior-chart starters who are Out or Doubtful.

Validation is the 2023 season. The holdout from 2024-09-01 is scored once.
The live model changes only when validation is not worse and the holdout
beats the current card by at least half a point.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from spe.db.session import init_db, session_factory
from spe.models.ats.model import fit_ridge_margin, score_predictions
from spe.models.ats.playing_time import CHART_FEATURES, load_nfl_chart_out
from spe.models.ats.predict import grade_displayed_pick
from spe.models.ats.ratings import build_team_strength_frame
from spe.models.ats.research_bundle import production_margin_path

VAL_START = pd.Timestamp("2023-09-01", tz="UTC")
HOLD_START = pd.Timestamp("2024-09-01", tz="UTC")
ALPHA = 8.0
MIN_HOLD_GAIN = 0.005


def cover_rate(art, frame: pd.DataFrame) -> tuple[float, int]:
    scored = score_predictions(art, frame, apply_calibration=False)
    hits = 0
    decided = 0
    for row in scored.itertuples(index=False):
        margin = getattr(row, "margin", None)
        handicap = getattr(row, "home_handicap", None)
        if margin is None or (isinstance(margin, float) and np.isnan(margin)):
            continue
        if handicap is None or (isinstance(handicap, float) and np.isnan(handicap)):
            continue
        home_p = float(row.p_home_cover)
        away_p = float(row.p_away_cover)
        side = "home" if home_p >= away_p else "away"
        result = grade_displayed_pick(
            status="final",
            home_score=float(row.home_score) if getattr(row, "home_score", None) is not None else None,
            away_score=float(row.away_score) if getattr(row, "away_score", None) is not None else None,
            pick_market="spread",
            favored_side=side,
            home_handicap=float(handicap),
        )
        if result is None or result == "push":
            # grade_displayed_pick needs scores. Fall back to the stored margin.
            if result is None:
                from spe.models.ats.labels import grade_side

                graded = grade_side(float(margin), float(handicap), side)
                if graded == "push":
                    continue
                result = "correct" if graded == "cover" else "miss"
            else:
                continue
        decided += 1
        hits += result == "correct"
    if decided == 0:
        return float("nan"), 0
    return hits / decided, decided


def evaluate(frame: pd.DataFrame, cols: list[str], start: pd.Timestamp, end: pd.Timestamp | None) -> tuple[float, int]:
    train = frame[frame["kickoff_utc"] < start]
    test = frame[frame["kickoff_utc"] >= start]
    if end is not None:
        test = test[test["kickoff_utc"] < end]
    art = fit_ridge_margin(train, league_id="nfl", alpha=ALPHA, feature_cols=cols)
    return cover_rate(art, test)


def main() -> None:
    init_db()
    session = session_factory()
    charts = load_nfl_chart_out(session)
    known = int(((charts["home_chart_known"] + charts["away_chart_known"]) > 0).sum()) if not charts.empty else 0
    outs = float(charts[["home_chart_out", "away_chart_out"]].sum().sum()) if not charts.empty else 0
    print(f"charts games={len(charts)} with_chart={known} starter_outs={outs:.0f}", flush=True)
    print("building strength frame", flush=True)
    frame = build_team_strength_frame(session, "nfl")
    session.close()
    frame["kickoff_utc"] = pd.to_datetime(frame["kickoff_utc"], utc=True)
    frame = frame.merge(charts, on="game_id", how="left")
    for col in CHART_FEATURES:
        frame[col] = frame[col].fillna(0.0)
    base_cols = json.loads(production_margin_path("nfl").read_text(encoding="utf-8"))["feature_cols"]
    extra_cols = list(base_cols) + list(CHART_FEATURES)
    print("validation 2023", flush=True)
    val_base, val_n = evaluate(frame, base_cols, VAL_START, HOLD_START)
    val_new, _ = evaluate(frame, extra_cols, VAL_START, HOLD_START)
    print(f"VAL  n={val_n}  base={val_base:.4f}  chart_out={val_new:.4f}", flush=True)
    print("holdout from 2024-09-01", flush=True)
    hold_base, hold_n = evaluate(frame, base_cols, HOLD_START, None)
    hold_new, _ = evaluate(frame, extra_cols, HOLD_START, None)
    print(f"HOLD n={hold_n}  base={hold_base:.4f}  chart_out={hold_new:.4f}", flush=True)
    ship = val_new >= val_base - 1e-9 and hold_new >= hold_base + MIN_HOLD_GAIN
    print(f"SHIP={ship}", flush=True)


if __name__ == "__main__":
    main()
