"""Train separate research models; replay uses the same feature function as production.

Input: compact archived rows in desk_opportunity.COLUMNS order. Never downloads data.
Historical results are conditional on observed participants, not executable-price ROI.
"""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from spe.integrity.assignments import canonical_team_map
from spe.models.metrics import apply_platt, brier_score, log_loss, platt_calibrate
from spe.models.player_td.desk_opportunity import FEATURES, VERSION, OpportunityHistory, prepare_history, vector


def fit(x, y):
    center, scale = x.mean(axis=0), x.std(axis=0)
    scale[scale < 1e-8] = 1
    z = np.column_stack([np.ones(len(x)), (x - center) / scale])
    b = np.zeros(z.shape[1])
    penalty = np.eye(len(b)) * 10.0
    penalty[0, 0] = 0
    for _ in range(40):
        p = 1 / (1 + np.exp(-np.clip(z @ b, -25, 25)))
        h = z.T @ (z * (p * (1 - p))[:, None]) + penalty + np.eye(len(b)) * 1e-8
        step = np.linalg.solve(h, z.T @ (p - y) + penalty @ b)
        b -= step
        if np.max(np.abs(step)) < 1e-7:
            break
    return {"features": FEATURES, "center": center.tolist(), "scale": scale.tolist(),
            "intercept": float(b[0]), "coefficients": b[1:].tolist()}


def probabilities(model, x):
    z = (x - model["center"]) / model["scale"]
    return 1 / (1 + np.exp(-np.clip(model["intercept"] + z @ model["coefficients"], -25, 25)))


def metrics(rows, p):
    y = np.array([r["label"] for r in rows])
    picks = {}
    for row, chance in zip(rows, p):
        if chance < .22:
            continue
        old = picks.get(row["game_id"])
        if old is None or chance > old[0]:
            picks[row["game_id"]] = (float(chance), row["label"])
    hits = sum(r[1] for r in picks.values())
    return {"player_games": len(rows), "brier": brier_score(y, p), "log_loss": log_loss(y, p),
            "predicted_rate": float(p.mean()), "actual_rate": float(y.mean()),
            "picks": len(picks), "hits": int(hits), "hit_rate": hits / max(len(picks), 1)}


def run(league, records, aliases, out):
    history_rows = prepare_history(records, aliases)
    history = OpportunityHistory(history_rows)
    rows = []
    for r in history_rows:
        f = history.features(r["player_id"], r["team_id"], r["opponent_id"], r["position"],
                             r["kickoff_utc"], r["team_id"] == r["home_team_id"])
        if f is not None:
            rows.append({**r, "features": f, "label": int((r["rush_td"] or 0) + (r["rec_td"] or 0) > 0)})
    train_end = "2025-11-01" if league == "nfl" else "2025-01-01"
    train = [r for r in rows if r["kickoff_utc"].isoformat() < train_end]
    valid = [r for r in rows if train_end <= r["kickoff_utc"].isoformat() < "2026-02-15"]
    test = [r for r in rows if r["kickoff_utc"].isoformat() >= "2026-08-01"]
    if min(len(train), len(valid), len(test)) < 200:
        raise ValueError(f"Insufficient temporal samples: {[len(x) for x in (train,valid,test)]}")
    x = lambda part: np.array([vector(r["features"]) for r in part])
    model = fit(x(train), np.array([r["label"] for r in train]))
    a, b = platt_calibrate(probabilities(model, x(valid)), np.array([r["label"] for r in valid]))
    model.update(calibration_intercept=a, calibration_slope=b, league=league, version=VERSION,
                 train_end=train_end, calibration_end="2026-02-15", research_only=True)
    report = {"train_n": len(train), "validation_n": len(valid), "test_n": len(test),
              "test_window": "2026 season through available November 7–9 finals",
              "limitations": ["Retrospective revised stats; not a prospective betting record.",
                              "Observed skill-player participants with at least two same-season, same-team prior games.",
                              "No historical injuries, route/coverage charting, or executable prices; no ROI claim.",
                              "NFL team identity coverage starts in 2025; FBS 2023–2024 training.",
                              "Comparison is recent-TD baseline, not the previous production model."]}
    for name, part in (("validation", valid), ("test", test)):
        p = apply_platt(probabilities(model, x(part)), a, b)
        baseline = np.array([1 - np.exp(-r["features"]["rush_td"] - r["features"]["rec_td"]) for r in part])
        report[name] = {"opportunity_model": metrics(part, p), "recent_td_baseline": metrics(part, baseline)}
    # Predeclared validation criterion; the 2026 test is never used for parameter fitting or promotion.
    v = report["validation"]
    model["research_approved"] = (b > 0 and v["opportunity_model"]["brier"] < v["recent_td_baseline"]["brier"])
    model["validation"] = report
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{league}_{VERSION}.json").write_text(json.dumps(model, indent=2) + "\n")
    print(json.dumps({"league": league, "research_approved": model["research_approved"], **report}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-prefix", required=True)
    parser.add_argument("--teams", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    aliases = canonical_team_map([SimpleNamespace(**r) for r in json.loads(Path(args.teams).read_text())])
    for league in ("nfl", "fbs"):
        run(league, json.loads(Path(f"{args.input_prefix}-{league}.json").read_text()), aliases, Path(args.output))
