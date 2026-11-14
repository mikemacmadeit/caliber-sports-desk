"""Team scoring × player opportunities, combined with Caliber's player model.

This is an independent research challenger, never a production promotion. All
players in the prior-history pool share the team budget, including unpriced
players. Unsupported players retain a fitted residual rather than invented usage.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path

import numpy as np

from spe.config import settings
from spe.db.base import as_utc
from spe.models.metrics import apply_platt, brier_score, log_loss, platt_calibrate
from spe.models.player_td.desk_opportunity import (
    FEATURES, OpportunityHistory, _mean, fit_logistic, predict, prepare_history, vector,
)

VERSION = "desk_hybrid_v1"
TEAM_FEATURES = ["team_rush_td", "team_rec_td", "opp_rush_td", "opp_rec_td", "is_home"]
EXCLUDED = {"out", "inactive", "confirmed_inactive", "ir", "injured reserve", "suspended"}


def team_context(history, team, opponent, cutoff, is_home):
    own = history.recent(history.teams.get(team, []), cutoff)
    defense = history.recent(history.defenses.get(opponent, []), cutoff)
    if min(len(own), len(defense)) < 2:
        return None
    return {"team_rush_td": _mean(own, "rush_td"), "team_rec_td": _mean(own, "rec_td"),
            "opp_rush_td": _mean(defense, "rush_td"), "opp_rec_td": _mean(defense, "rec_td"),
            "is_home": float(is_home)}


def player_pool(history, team, opponent, cutoff, is_home, *, roster_ids=None, statuses=None):
    """Build before looking at the target game's outcomes or available prices."""
    pool = {}
    for pid in sorted(history.player_ids_by_team.get(team, [])):
        if roster_ids is not None and pid not in roster_ids:
            continue
        appearances = history.players[(pid, team)]
        if str((statuses or {}).get(pid, "unknown")).lower() in EXCLUDED:
            continue
        recent = history.recent(appearances, cutoff)
        if not recent or (cutoff - recent[-1]["kickoff_utc"]).days > 21:
            continue
        f = history.features(pid, team, opponent, recent[-1]["position"], cutoff, is_home)
        if f is not None:
            pool[pid] = {"features": f, "position": recent[-1]["position"]}
    return pool


def opportunity_vector(features, kind, league):
    # High-value opportunities are a subset of total opportunities: subtract them
    # from the ordinary bucket so a goal-line carry is never counted twice.
    if kind == "rush":
        total = max(0., features["carries"])
        high = min(total, max(0., features["gl_carries"])) if features["charting_known"] and league == "nfl" else 0.
    else:
        total = max(0., features["targets"] if league == "nfl" else features["receptions"])
        high = min(total, max(0., features["rz_targets"])) if features["charting_known"] and league == "nfl" else 0.
    return np.array([total - high, high])


def _fit_nonnegative(x, y):
    # Two-coordinate ridge least squares; no extra dependencies or negative usage.
    coef = np.zeros(x.shape[1])
    for _ in range(300):
        old = coef.copy()
        for j in range(len(coef)):
            residual = y - x @ coef + x[:, j] * coef[j]
            coef[j] = max(0., float(x[:, j] @ residual) / (float(x[:, j] @ x[:, j]) + 1.))
        if np.max(np.abs(coef - old)) < 1e-9:
            break
    return coef.tolist()


def _fit_count(x, y):
    center, scale = x.mean(axis=0), x.std(axis=0)
    scale[scale < 1e-8] = 1
    z = np.column_stack([np.ones(len(x)), (x - center) / scale])
    b = np.zeros(z.shape[1])
    b[0] = np.log(max(float(y.mean()), 1e-4))
    penalty = np.eye(len(b)) * 10.
    penalty[0, 0] = 0
    for _ in range(60):
        mu = np.exp(np.clip(z @ b, -12, 6))
        h = z.T @ (z * mu[:, None]) + penalty + np.eye(len(b)) * 1e-8
        step = np.linalg.solve(h, z.T @ (mu - y) + penalty @ b)
        b -= step / max(1., float(np.max(np.abs(step))))
        if np.max(np.abs(step)) < 1e-7:
            break
    return {"center": center.tolist(), "scale": scale.tolist(), "coefficients": b[1:].tolist(),
            "intercept": float(b[0])}


def _count(model, features):
    x = np.array([features[k] for k in TEAM_FEATURES])
    z = (x - model["center"]) / model["scale"]
    return float(np.exp(np.clip(model["intercept"] + z @ model["coefficients"], -12, 6)))


def allocate(model, context, pool):
    """Conserve each team TD budget, even when no supported recipients remain."""
    result = {pid: {"lambda_rush": 0., "lambda_rec": 0.} for pid in pool}
    totals, residuals = {}, {}
    for kind in ("rush", "rec"):
        total = _count(model["team_models"][kind], context)
        raw = {pid: float(opportunity_vector(v["features"], kind, model["league"]) @
                          model["allocation_coefficients"][kind]) for pid, v in pool.items()}
        denom = sum(raw.values())
        residual = model["other_share"][kind] if denom > 0 else 1.
        for pid in pool:
            result[pid][f"lambda_{kind}"] = total * (1 - residual) * raw[pid] / denom if denom else 0.
        totals[kind], residuals[kind] = total, total * residual
    for v in result.values():
        v["team_probability"] = float(-np.expm1(-v["lambda_rush"] - v["lambda_rec"]))
    return result, totals, residuals


def blended_probability(model, player_probability, team_probability, *, calibrated=True):
    logit = lambda p: np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
    w = model["team_weight"]
    z = (1 - w) * logit(player_probability) + w * logit(team_probability)
    if calibrated:
        z = model["calibration_intercept"] + model["calibration_slope"] * z
    return float(1 / (1 + np.exp(-np.clip(z, -25, 25))))


def build_samples(records, aliases=None):
    rows = prepare_history(records, aliases)
    history = OpportunityHistory(rows)
    grouped = defaultdict(list)
    for r in rows:
        grouped[(r["game_id"], r["team_id"])].append(r)
    samples = []
    for (gid, team), actual in sorted(grouped.items()):
        first = actual[0]
        cutoff, opponent = first["kickoff_utc"], first["opponent_id"]
        home = team == first["home_team_id"]
        context = team_context(history, team, opponent, cutoff, home)
        if context is None:
            continue
        pool = player_pool(history, team, opponent, cutoff, home)
        # Missing participants are not silently assigned a no-TD label.
        observed = {r["player_id"]: r for r in actual if r.get("rush_td") is not None and r.get("rec_td") is not None}
        if len(observed) != len(actual):
            continue
        samples.append({"game_id": gid, "team_id": team, "kickoff": cutoff,
                        "context": context, "pool": pool, "observed": observed})
    return sorted(samples, key=lambda r: (r["kickoff"], r["game_id"], r["team_id"]))


def _paired_rows(samples, model):
    rows = []
    for s in samples:
        allocation, _, _ = allocate(model, s["context"], s["pool"])
        for pid in sorted(s["observed"].keys() & s["pool"].keys()):
            r, f = s["observed"][pid], s["pool"][pid]["features"]
            rows.append({"game_id": s["game_id"], "player_id": pid, "position": r["position"],
                         "y": int(r["rush_td"] + r["rec_td"] > 0),
                         "player": predict(model["player_model"], f),
                         "team": allocation[pid]["team_probability"]})
    return rows


def _metrics(rows, values):
    y = np.array([r["y"] for r in rows])
    p = np.array(values)
    return {"n": len(y), "brier": brier_score(y, p), "log_loss": log_loss(y, p),
            "predicted_rate": float(p.mean()), "actual_rate": float(y.mean())}


def train_hybrid(records, league, *, train_end, selection_end, calibration_end, test_end, aliases=None):
    bounds = list(map(as_utc, (train_end, selection_end, calibration_end, test_end)))
    if league not in {"nfl", "fbs"} or any(b is None for b in bounds) or bounds != sorted(set(bounds)):
        raise ValueError("Use one league and four strictly increasing UTC boundaries")
    if any(isinstance(r, dict) and r.get("league_id", league) != league for r in records):
        raise ValueError("NFL and college records must be trained separately")
    samples = build_samples(records, aliases)
    parts = [[s for s in samples if (i == 0 or s["kickoff"] >= bounds[i-1]) and s["kickoff"] < bounds[i]] for i in range(4)]
    if min(map(len, parts)) < 40:
        raise ValueError(f"Need at least 40 team-games in each separate period; got {list(map(len, parts))}")
    train, selection, calibration, test = parts
    fit_rows = [(v["features"], s["observed"][pid]) for s in train for pid, v in s["pool"].items() if pid in s["observed"]]
    labels = np.array([int(r["rush_td"] + r["rec_td"] > 0) for _, r in fit_rows])
    if len(labels) < 200 or len(set(labels)) < 2:
        raise ValueError("Need at least 200 observed training player-games with both outcomes")
    model = {"version": VERSION, "league": league, "features": FEATURES, "team_features": TEAM_FEATURES,
             "research_only": True, "production_approved": False, "team_models": {},
             "allocation_coefficients": {}, "other_share": {}, "team_weight": 0.,
             "calibration_intercept": 0., "calibration_slope": 1.,
             "player_model": fit_logistic(np.array([vector(f) for f, _ in fit_rows]), labels),
             **dict(zip(("train_end", "selection_end", "calibration_end", "test_end"), [b.isoformat() for b in bounds]))}
    x = np.array([[s["context"][k] for k in TEAM_FEATURES] for s in train])
    for kind in ("rush", "rec"):
        key = f"{kind}_td"
        y = np.array([sum(r[key] for r in s["observed"].values()) for s in train])
        model["team_models"][kind] = _fit_count(x, y)
        model["allocation_coefficients"][kind] = _fit_nonnegative(
            np.array([opportunity_vector(f, kind, league) for f, _ in fit_rows]),
            np.array([r[key] for _, r in fit_rows]))
        missed = sum(r[key] for s in train for pid, r in s["observed"].items() if pid not in s["pool"])
        model["other_share"][kind] = float((missed + 1) / (y.sum() + 2))
    selected = _paired_rows(selection, model)
    calibrated = _paired_rows(calibration, model)
    evaluated = _paired_rows(test, model)
    if min(map(len, (selected, calibrated, evaluated))) < 200 or any(len({r["y"] for r in part}) < 2 for part in (selected, calibrated, evaluated)):
        raise ValueError("Need 200 paired player-games and both outcomes in each later period")
    # Choose the blend before calibration and evaluation. Ties favor the existing method.
    losses = []
    for w in np.linspace(0, 1, 11):
        model["team_weight"] = float(w)
        p = [blended_probability(model, r["player"], r["team"], calibrated=False) for r in selected]
        losses.append(_metrics(selected, p)["log_loss"])
    model["team_weight"] = float(np.linspace(0, 1, 11)[int(np.argmin(losses))])
    cp = np.array([blended_probability(model, r["player"], r["team"], calibrated=False) for r in calibrated])
    cy = np.array([r["y"] for r in calibrated])
    a, b = platt_calibrate(cp, cy)
    model.update(calibration_intercept=float(a), calibration_slope=float(b), shadow_ready=bool(b > 0))
    # Calibrate the player-only comparator on the SAME calibration games.
    pa, pb = platt_calibrate(np.array([r["player"] for r in calibrated]), cy)
    p_test = [blended_probability(model, r["player"], r["team"]) for r in evaluated]
    report = {"team_games_by_period": list(map(len, parts)), "paired_players_by_period": list(map(len, (selected, calibrated, evaluated))),
              "coverage_by_period": [{"observed_player_games": sum(len(s["observed"]) for s in part),
                  "supported_observed_player_games": sum(len(s["observed"].keys() & s["pool"].keys()) for s in part),
                  "unobserved_pool_player_games": sum(len(s["pool"].keys() - s["observed"].keys()) for s in part),
                  "earliest_kickoff": part[0]["kickoff"].isoformat(),
                  "latest_kickoff": part[-1]["kickoff"].isoformat()} for part in parts],
              "evaluation_used_for_selection": False, "promotion": "disabled",
              "comparison": "refitted player-only recipe; not the frozen production artifact",
              "hybrid": _metrics(evaluated, p_test),
              "player_only": _metrics(evaluated, apply_platt(np.array([r["player"] for r in evaluated]), pa, pb)),
              "team_allocation": _metrics(evaluated, [r["team"] for r in evaluated]),
              "by_position": {pos: _metrics([r for r in evaluated if r["position"] == pos],
                  [p for r, p in zip(evaluated, p_test) if r["position"] == pos]) for pos in sorted({r["position"] for r in evaluated})},
              "limitations": ["Revised historical stats; not contemporaneous source snapshots.",
                  "Evaluation conditional on observed participants with sufficient earlier history; missing players are not losses.",
                  "Historical injuries and role replacements are not reconstructed.",
                  "No price or return evaluation; no automatic promotion or claim of profitability.",
                  "Previously inspected dates are retrospective comparison, not a pristine holdout."]}
    # Hash only prediction parameters. Evaluation labels cannot change the deployed model ID.
    model["artifact_sha256"] = hashlib.sha256(json.dumps(model, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return model, report


def artifact_path(league):
    return Path(settings.parquet_dir) / "models" / f"{league}_{VERSION}.json"


def load_hybrid(league, cutoff):
    try:
        model = json.loads(artifact_path(league).read_bytes())
        digest = model.pop("artifact_sha256")
        if hashlib.sha256(json.dumps(model, sort_keys=True, allow_nan=False).encode()).hexdigest() != digest:
            return None
        if (model["version"] != VERSION or model["league"] != league or model["features"] != FEATURES
                or model["team_features"] != TEAM_FEATURES or not model["research_only"]
                or model["production_approved"] or not model["shadow_ready"]
                or not 0 <= model["team_weight"] <= 1 or model["calibration_slope"] <= 0
                or not (as_utc(model["train_end"]) < as_utc(model["selection_end"]) < as_utc(model["calibration_end"]) < cutoff)):
            return None
        # Reject malformed shapes and missing coefficients before reaching the live request.
        for fitted, width in [(model["player_model"], len(FEATURES)),
                              *[(m, len(TEAM_FEATURES)) for m in model["team_models"].values()]]:
            if (any(len(fitted[k]) != width for k in ("center", "scale", "coefficients"))
                    or any(v <= 0 for v in fitted["scale"])):
                return None
        if any(len(v) != 2 or any(c < 0 for c in v) for v in model["allocation_coefficients"].values()):
            return None
        dummy = {k: 1. for k in FEATURES}
        if not np.isfinite(predict(model["player_model"], dummy)):
            return None
        values, _, _ = allocate(model, dummy, {"check": {"features": dummy}})
        if not all(np.isfinite(v) for v in values["check"].values()):
            return None
        if any(not 0 <= v <= 1 for v in model["other_share"].values()):
            return None
        model["artifact_sha256"] = digest
        return model
    except (OSError, KeyError, TypeError, ValueError, OverflowError):
        return None
