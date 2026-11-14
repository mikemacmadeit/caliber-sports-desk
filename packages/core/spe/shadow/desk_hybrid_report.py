"""Paired prospective comparison at a fixed one-hour pregame information cutoff."""
from collections import defaultdict
from datetime import timedelta

import numpy as np

from spe.db.base import as_utc
from spe.models.metrics import brier_score, log_loss
from spe.market.odds import american_to_implied


def _metrics(rows, key):
    y, p = np.array([r["y"] for r in rows]), np.array([r[key] for r in rows])
    bins = []
    for low, high in ((0., .1), (.1, .25), (.25, .5), (.5, .75), (.75, 1.01)):
        mask = (p >= low) & (p < high)
        if mask.any():
            bins.append({"from": low, "to": min(high, 1.), "n": int(mask.sum()),
                         "predicted": float(p[mask].mean()), "observed": float(y[mask].mean())})
    return {"n": len(rows), "brier": brier_score(y, p), "log_loss": log_loss(y, p), "reliability": bins}


def _picks(rows, key):
    picks = {}
    for r in rows:
        if r["blockers"] or r[key] < .22 or r[key] - american_to_implied(r["price"]) < .04:
            continue
        old = picks.get(r["game"])
        if old is None or (r[key], r["player"]) > (old[key], old["player"]):
            picks[r["game"]] = r
    values = list(picks.values())
    return {"games": len(values), "wins": sum(r["y"] for r in values),
            "hit_rate": sum(r["y"] for r in values) / len(values) if values else None,
            "flat_stake_roi": float(np.mean([r["return"] for r in values])) if values and all(r["return"] is not None for r in values) else None,
            "note": "One research pick per game on the matched candidate pool; not the executed Desk record."}


def comparison_report(predictions, settlements):
    latest = {}
    late = 0
    for pred in predictions:
        snapshot = (pred.components or {}).get("paired_snapshot")
        if not snapshot:
            continue
        kickoff = as_utc(snapshot.get("kickoff_utc"))
        stamp = as_utc(pred.forecast_as_of)
        if not kickoff or not stamp or stamp > kickoff - timedelta(hours=1):
            late += 1
            continue
        key = (pred.league_id, pred.model_version_id, snapshot["incumbent_version"], pred.game_id, pred.player_id)
        old = latest.get(key)
        if old is None or (stamp, pred.shadow_id) > (as_utc(old.forecast_as_of), old.shadow_id):
            latest[key] = pred
    groups = defaultdict(list)
    pending = void = blocked = 0
    for key, pred in latest.items():
        settled = settlements.get(pred.shadow_id)
        if settled is None:
            pending += 1
            continue
        if settled.result not in {"win", "loss"}:
            if settled.result == "blocked":
                blocked += 1
            else:
                void += 1
            continue
        s = pred.components["paired_snapshot"]
        groups[key[:3]].append({"game": pred.game_id, "player": pred.player_id,
            "y": int(settled.result == "win"), "hybrid": pred.forecast_probability,
            "incumbent": s["incumbent_probability"], "position": s.get("position"),
            "price": pred.price_american, "return": settled.flat_stake_return,
            "blockers": s.get("common_blockers", [])})
    reports = []
    for (league, hybrid_version, incumbent_version), rows in sorted(groups.items()):
        by_game = defaultdict(list)
        for r in rows:
            by_game[r["game"]].append((r["hybrid"] - r["y"]) ** 2 - (r["incumbent"] - r["y"]) ** 2)
        ci = None
        if len(by_game) >= 20:
            # Resample games, retaining every player in each sampled game.
            grouped = list(by_game.values())
            sums, counts = np.array([sum(v) for v in grouped]), np.array([len(v) for v in grouped])
            idx = np.random.default_rng(42).integers(0, len(grouped), size=(1000, len(grouped)))
            draws = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
            ci = np.quantile(draws, [.025, .975]).tolist()
        metrics = {k: _metrics(rows, k) for k in ("hybrid", "incumbent")}
        reports.append({"league": league, "hybrid_version": hybrid_version,
            "incumbent_version": incumbent_version, "games": len(by_game), **metrics,
            "brier_difference": metrics["hybrid"]["brier"] - metrics["incumbent"]["brier"],
            "brier_difference_game_bootstrap_95": ci,
            "selection": {k: _picks(rows, k) for k in ("hybrid", "incumbent")},
            "by_position": {str(pos): {k: _metrics([r for r in rows if r["position"] == pos], k)
                for k in ("hybrid", "incumbent")} for pos in {r["position"] for r in rows}}})
    return {"research_only": True, "promotion": "disabled", "cutoff_minutes_before_kickoff": 60,
            "pending": pending, "void_or_push": void, "blocked": blocked,
            "snapshots_after_cutoff": late, "comparisons": reports,
            "limitations": ["Matched supported, priced players; unsupported rookies and replacements are excluded.",
                "No automatic model promotion; small samples and repeated research comparisons do not prove an edge."]}
