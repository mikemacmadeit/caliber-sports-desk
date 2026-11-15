"""Compare free NFL inputs against the existing Desk on identical temporal slices.

No downloading, book prices, or model tuning on the 2026 evaluation window.
Usage: PYTHONPATH=packages/core python scripts/evaluate_nflverse_desk.py --help
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from train_desk_opportunity import fit, probabilities, metrics
from spe.analytics.nflverse_opportunities import pbp_context, snap_context
from spe.models.metrics import apply_platt, platt_calibrate
from spe.models.player_td.desk_opportunity import FEATURES, NFL_VERSION, OpportunityHistory, prepare_history, predict, vector
from spe.models.player_td.nflverse_context import attach_context, NFL_EXTRA_FEATURES


def run(args):
    history = prepare_history(json.loads(args.history.read_text()))
    players = pd.read_parquet(args.data / "players.parquet")
    positions = dict(zip(players.gsis_id, players.position_group))
    context, source_counts = [], {}
    for year in sorted({int(r["game_id"].split(":")[1][:4]) for r in history}):
        pbp = pbp_context(pd.read_parquet(args.data / f"play_by_play_{year}.parquet"), positions)
        snaps, unresolved = snap_context(pd.read_parquet(args.data / f"snap_counts_{year}.parquet"), players)
        context.extend(pbp + snaps)
        source_counts[year] = {"pbp_context_rows": len(pbp), "snap_rows": len(snaps), "unresolved_snap_rows": unresolved}
    attach_context(history, context)
    engine = OpportunityHistory(history)
    samples = []
    for r in history:
        f = engine.features(r["player_id"], r["team_id"], r["opponent_id"], r["position"], r["kickoff_utc"], r["team_id"] == r["home_team_id"])
        if f:
            samples.append({**r, "features": f, "label": int((r["rush_td"] or 0) + (r["rec_td"] or 0) > 0)})
    train = [r for r in samples if r["kickoff_utc"].isoformat() < "2025-11-01"]
    calibration = [r for r in samples if "2025-11-01" <= r["kickoff_utc"].isoformat() < "2025-12-01"]
    valid = [r for r in samples if "2025-12-01" <= r["kickoff_utc"].isoformat() < "2026-02-15"]
    test = [r for r in samples if r["kickoff_utc"].isoformat() >= "2026-08-01"]
    if min(map(len, (train, calibration, valid, test))) < 200:
        raise ValueError("Insufficient temporal samples")
    names = FEATURES + NFL_EXTRA_FEATURES
    x = lambda part: np.array([vector(r["features"], names) for r in part])
    model = fit(x(train), np.array([r["label"] for r in train]))
    model["features"] = names
    a, b = platt_calibrate(probabilities(model, x(calibration)), np.array([r["label"] for r in calibration]))
    model.update(version=NFL_VERSION, league="nfl", calibration_intercept=a, calibration_slope=b,
                 train_end="2025-11-01", calibration_end="2025-12-01", validation_end="2026-02-15", research_only=True, live_enabled=False)
    base = json.loads(args.baseline.read_text())
    report = {"source_counts": source_counts, "train_n": len(train), "calibration_n": len(calibration), "validation_n": len(valid), "evaluation_n": len(test),
        "promotion_rule": "Shadow comparison only. A validation score cannot authorize live promotion; independent prospective results are required.",
        "limitations": ["Revised postgame data with an assumed 48-hour availability lag; not timestamped historical source snapshots.",
                        "Observed participants only; historical availability and executable sportsbook prices unavailable.",
                        "2026 results are an examined retrospective evaluation, not evidence of prospective profit.",
                        "NGS and depth-chart summaries are descriptive context, not fitted probability inputs."]}
    for name, part in (("validation", valid), ("evaluation_2026", test)):
        p = apply_platt(probabilities(model, x(part)), a, b)
        prior = np.array([predict(base, r["features"]) for r in part])
        # The live fallback is also applied to this replay.
        complete = np.array([bool(r["features"].get("snap_known") and r["features"].get("pbp_known") and r["features"].get("free_data_current")) for r in part])
        report[name] = {"candidate_with_fallback": metrics(part, np.where(complete, p, prior)),
                        "existing_v2": metrics(part, prior), "enriched_rows": int(complete.sum())}
    v = report["validation"]
    new, old = v["candidate_with_fallback"], v["existing_v2"]
    # Permit collecting a shadow comparison even if it loses validation. Never
    # select a model by modifying it after examining the final test outcomes.
    report["validation_gate_passed"] = bool(b > 0 and new["brier"] < old["brier"] and new["log_loss"] <= old["log_loss"] and new["hits"] >= old["hits"])
    model["research_approved"] = bool(b > 0)
    report["research_approved"] = model["research_approved"]
    model["validation"] = report
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / f"nfl_{NFL_VERSION}.json").write_text(json.dumps(model, indent=2) + "\n")
    (args.output / "nflverse-evaluation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, default=Path("deploy/runtime-data/parquet/models/nfl_desk_opportunity_v2.json"))
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args())
