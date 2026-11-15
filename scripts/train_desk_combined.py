"""Fit the combined Desk using existing SQLite data, opened read-only. No downloads.

Four distinct periods: component fit, blending, calibration, validation. The 2026
replay is reported after parameters/gates are fixed and is not a profit backtest.
"""
import argparse
import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from train_desk_opportunity import fit, metrics as base_metrics
from spe.models.metrics import apply_platt, log_loss, platt_calibrate
from spe.models.player_td.desk_live import HISTORY_SQL
from spe.models.player_td.desk_opportunity import FEATURES, OpportunityHistory, prepare_history, predict, vector
from spe.models.player_td.desk_market import MARKET_FEATURES, market_features
from spe.models.player_td.desk_evidence import RICH_FEATURES, FREE_FEATURES, load_evidence
from spe.models.player_td.desk_combined import (
    VERSION, AllocationHistory, combined_prediction, logit, opportunity_vector, sigmoid, team_vector,
)
from spe.integrity.assignments import canonical_team_map


def metrics(rows, probabilities):
    result = base_metrics(rows, probabilities)
    selected = {}
    for row, probability in zip(rows, probabilities):
        if row['game_id'] not in selected or probability > selected[row['game_id']][0]:
            selected[row['game_id']] = (float(probability), row['label'])
    hits = sum(label for _, label in selected.values())
    result['one_per_game'] = {'games':len(selected), 'hits':int(hits), 'hit_rate':hits/max(len(selected),1)}
    return result


def paired_brier_interval(rows, candidate, baseline):
    """Resample whole games, keeping teammates/opponents together."""
    sums, counts = defaultdict(float), defaultdict(int)
    for r, new, old in zip(rows, candidate, baseline):
        sums[r["game_id"]] += (new - r["label"]) ** 2 - (old - r["label"]) ** 2
        counts[r["game_id"]] += 1
    keys = sorted(sums)
    errors = np.array([sums[k] for k in keys])
    sizes = np.array([counts[k] for k in keys])
    indices = np.random.default_rng(20261114).integers(0, len(keys), (2000, len(keys)))
    differences = errors[indices].sum(axis=1) / sizes[indices].sum(axis=1)
    return {"difference": float(errors.sum()/sizes.sum()), "game_bootstrap_95_percent": np.quantile(differences, [.025, .975]).tolist(), "games": len(keys)}


def poisson_fit(x, y):
    b = np.zeros(x.shape[1])
    b[0] = np.log(max(y.mean(), .01))
    penalty = np.eye(len(b)) * 10
    penalty[0, 0] = 0
    for _ in range(50):
        mu = np.exp(np.clip(x @ b, -8, 3))
        step = np.linalg.solve(x.T @ (x * mu[:, None]) + penalty + np.eye(len(b)) * 1e-8,
                               x.T @ (mu - y) + penalty @ b)
        b -= step / max(1, np.max(np.abs(step)))
        if np.max(np.abs(step)) < 1e-7:
            break
    return b.tolist()


def rates_fit(x, y):
    # Nonnegative Poisson identity-link rates; disjoint ordinary/scoring-area work.
    rates = np.full(x.shape[1], max(y.sum(), 1) / max(x.sum(), 1))
    for _ in range(300):
        rates *= (x.T @ (y / np.maximum(x @ rates, 1e-8))) / np.maximum(x.sum(axis=0), 1e-8)
    return rates.tolist()


def run(league, records, output, aliases=None, markets=None, situational=False, roles=False,
        input_profile=None, with_totals=False, player_matchups=False):
    markets = markets or {}
    source_digest = hashlib.sha256(json.dumps({"records": records, "markets": markets}, sort_keys=True).encode()).hexdigest()
    records = prepare_history(records, aliases)
    history = OpportunityHistory(records)
    history.evidence = load_evidence(league, replay=True)
    from spe.config import settings
    evidence_path = settings.parquet_dir / "desk_evidence" / (league+".json")
    evidence_sha256 = hashlib.sha256(evidence_path.read_bytes()).hexdigest() if evidence_path.exists() else None
    allocator = AllocationHistory(history)
    component_end = "2025-10-15" if league == "nfl" else "2025-01-01"
    blend_end, calibration_end, validation_end = "2025-11-15", "2025-12-15", "2026-02-15"
    if league == "fbs":
        # Available college archive ends at the regular season, before Dec 15.
        blend_end, calibration_end = "2025-10-15", "2025-11-01"
    rows, games = [], defaultdict(list)
    names = FEATURES + MARKET_FEATURES + RICH_FEATURES + (FREE_FEATURES if league == "fbs" else [])
    from spe.models.player_td.situational import SITUATIONAL_FEATURES
    if situational:
        names += SITUATIONAL_FEATURES
    if roles:
        from spe.models.player_td.role_features import ROLE_FEATURES
        names += ROLE_FEATURES
    from spe.models.player_td.current_opportunities import transform, FEATURES as CURRENT_FEATURES
    if input_profile == 'current':
        names += CURRENT_FEATURES
    if player_matchups:
        from spe.models.player_td.matchup_features import FEATURES as PLAYER_MATCHUPS
        names += PLAYER_MATCHUPS
        if league == 'nfl':
            from spe.models.player_td.nflverse_context import NFL_EXTRA_FEATURES
            names += NFL_EXTRA_FEATURES
    for r in records:
        games[(r["game_id"], r["team_id"])].append(r)
        f = history.features(r["player_id"], r["team_id"], r["opponent_id"], r["position"],
                             r["kickoff_utc"], r["team_id"] == r["home_team_id"])
        context = markets.get(r["game_id"], {})
        if f and context.get("home_handicap") is not None and (league == "fbs" or context.get("total") is not None) and r["kickoff_utc"] - opportunity_time(f["last_played_at"]) <= timedelta(days=21):
            f.update(market_features(context, r["team_id"] == r["home_team_id"], league == "nfl" or with_totals))
            rows.append({**r, "features": f, "market": context, "label": int((r["rush_td"] or 0) + (r["rec_td"] or 0) > 0)})
    parts = {
        "fit": [r for r in rows if r["kickoff_utc"].isoformat() < component_end],
        "blend": [r for r in rows if component_end <= r["kickoff_utc"].isoformat() < blend_end],
        "calibrate": [r for r in rows if blend_end <= r["kickoff_utc"].isoformat() < calibration_end],
        "validation": [r for r in rows if calibration_end <= r["kickoff_utc"].isoformat() < validation_end],
        "evaluation_2026": [r for r in rows if r["kickoff_utc"].isoformat() >= "2026-08-01"],
    }
    counts = {k: len(v) for k, v in parts.items()}
    if min(counts.values()) < 100:
        raise ValueError(f"Insufficient chronological samples: {counts}")
    train = parts["fit"]
    model = {"version": VERSION, "league": league, "research_only": True, "rich_features": True, "free_opportunities": league == "fbs",
             "market_features": True, "market_total_trained": league == "nfl" or with_totals,
             "player_model": fit(np.array([vector(transform(r["features"], input_profile), names) for r in train]), np.array([r["label"] for r in train])),
             "team_coefficients": {}, "allocation_rates": {}, "residual_share": {},
             "allocation_weight": 0.0, "calibration_intercept": 0.0, "calibration_slope": 1.0,
             "component_end": component_end, "blend_end": blend_end, "calibration_end": calibration_end,
             "validation_end": validation_end, "source_sha256": source_digest, "evidence_sha256": evidence_sha256}
    model["player_model"]["features"] = names
    if situational:
        model.update(version="desk_combined_v5", situational_features=True)
    if roles:
        model.update(version="desk_combined_v6", role_features=True)
    if input_profile:
        model.update(version='desk_combined_v7', input_profile=input_profile)
    if player_matchups:
        model.update(version='desk_combined_v9', player_matchups=True)
    unique = {(r["game_id"], r["team_id"]): r for r in train}
    for kind, label in (("rush", "rush_td"), ("rec", "rec_td")):
        model["team_coefficients"][kind] = poisson_fit(
            np.array([team_vector(r["features"], True, True, situational) for r in unique.values()]),
            np.array([sum(p[label] or 0 for p in games[key]) for key in unique]))
        model["allocation_rates"][kind] = rates_fit(
            np.array([opportunity_vector(r["features"], kind, league, league == "fbs", roles, input_profile, player_matchups) for r in train]),
            np.array([r[label] or 0 for r in train]))
        missed, total = 1.0, 20.0  # weak Beta(1,19) smoothing of unknown-role share
        for key, r in unique.items():
            pool = allocator.pool(r["team_id"], r["kickoff_utc"], league == "fbs")
            for p in games[key]:
                scores = p[label] or 0
                total += scores
                if p["player_id"] not in pool or not np.any(opportunity_vector(pool[p["player_id"]], kind, league, league == "fbs", roles, input_profile, player_matchups)):
                    missed += scores
        model["residual_share"][kind] = float(missed / total)

    def components(part):
        cached, bottom, top = {}, [], []
        for r in part:
            key = (r["game_id"], r["team_id"])
            if key not in cached:
                cached[key] = allocator.allocate(model, r["team_id"], r["opponent_id"], r["kickoff_utc"], r["team_id"] == r["home_team_id"], market=r["market"])
            c = combined_prediction(model, r["features"], cached[key], r["player_id"])
            if c is None:
                raise ValueError("Evaluation candidate absent from pregame allocation pool")
            bottom.append(c["player_probability"])
            top.append(c["allocation_probability"])
        return np.array(bottom), np.array(top)

    cp = {k: components(v) for k, v in parts.items() if k != "fit"}
    y = lambda key: np.array([r["label"] for r in parts[key]])
    bottom, top = cp["blend"]
    weights = np.linspace(0, 1, 101)
    if input_profile:
        # Scorer-first objective, selected ONLY in the blend period. Probability
        # quality still has to pass the independent validation gate below.
        def blend_key(weight):
            p = sigmoid((1-weight)*logit(bottom)+weight*logit(top))
            m = metrics(parts['blend'], p)
            return (-m['one_per_game']['hits'], m['log_loss'])
        model['allocation_weight'] = float(min(weights, key=blend_key))
    else:
        model["allocation_weight"] = float(min(weights, key=lambda w: log_loss(y("blend"), sigmoid((1-w)*logit(bottom)+w*logit(top)))))
    raw = lambda key: sigmoid((1-model["allocation_weight"])*logit(cp[key][0])+model["allocation_weight"]*logit(cp[key][1]))
    from spe.models.player_td.calibration import stable_platt
    calibrator = stable_platt if roles or input_profile else platt_calibrate
    a, b = calibrator(raw("calibrate"), y("calibrate"))
    base_a, base_b = calibrator(cp["calibrate"][0], y("calibrate"))
    model.update(calibration_intercept=a, calibration_slope=b)
    report = {"sample_counts": counts, "allocation_weight": model["allocation_weight"],
        "limitations": ["Revised historical stats and historical market archives retrieved after games; not archived pregame source snapshots.",
                        "Labels include observed participants with two prior same-team games; inactive/DNP outcomes not reconstructed.",
                        "Historical injury reports and executable prices unavailable; pick metrics are highest-probability scorers, not priced Desk ROI.",
                        "Current team pool uses recent appearances; rookies, transfers and new replacements need role history.",
                        "NFL and FBS fitted independently. Spreads enter both models; NFL totals also enter probability models. College historical totals are insufficient, so current college totals are used only for selection screening.",
                        "No complete routes or fitted weather adjustment. Schedule residuals use only preceding games. Archived odds timing is not independently verified.",
                        "Play-by-play is revised historical data with a 48-hour postgame availability assumption; not archived pregame evidence.",
                        "2026 is an examined retrospective replay, not an untouched prospective test."],
        "promotion_rule": "Positive allocation weight and calibration slope; validation Brier and log loss no worse than independently calibrated player-only component. Research Desk only."}
    if input_profile:
        report['blend_selection'] = metrics(parts['blend'], raw('blend'))
        report['input_profile'] = input_profile
        report['limitations'][4] = 'NFL and FBS fit independently. This FBS experiment uses retrospectively collected historical spreads and totals; original observation times and executable prices are not verified.'
    for key in ("validation", "evaluation_2026"):
        combined = apply_platt(raw(key), a, b)
        player_only = apply_platt(cp[key][0], base_a, base_b)
        report[key] = {"combined": metrics(parts[key], combined),
                       "player_only": metrics(parts[key], player_only),
                       "paired_brier_vs_player_only": paired_brier_interval(parts[key], combined, player_only),
                       "allocation_uncalibrated": metrics(parts[key], cp[key][1])}
        baseline_path = Path("data/parquet/models") / f"{league}_desk_combined_v3.json"
        if not baseline_path.exists():
            baseline_path = Path("data/parquet/models") / f"{league}_desk_combined_v2.json"
        if baseline_path.exists():
            previous = json.loads(baseline_path.read_text())
            prior_p, cached = [], {}
            for r in parts[key]:
                game_key = (r["game_id"], r["team_id"])
                if game_key not in cached:
                    cached[game_key] = allocator.allocate(previous, r["team_id"], r["opponent_id"], r["kickoff_utc"], r["team_id"] == r["home_team_id"], market=r["market"])
                baseline_f = {**r['features'], **market_features(r['market'], r['team_id']==r['home_team_id'], previous.get('market_total_trained', True))}
                prior_p.append(combined_prediction(previous, baseline_f, cached[game_key], r["player_id"])["probability"])
            report[key]["previous_combined"] = metrics(parts[key], np.array(prior_p))
            report[key]["paired_brier_vs_previous_combined"] = paired_brier_interval(parts[key], combined, np.array(prior_p))
        if key == "evaluation_2026":
            baseline_path = Path(__file__).resolve().parents[1] / f"deploy/runtime-data/parquet/models/{league}_desk_opportunity_v2.json"
            if baseline_path.exists():
                prior = json.loads(baseline_path.read_text())
                prior_p = np.array([predict(prior, r["features"]) for r in parts[key]])
                report[key]["previous_v2"] = metrics(parts[key], prior_p)
                report[key]["paired_brier_vs_previous_v2"] = paired_brier_interval(parts[key], combined, prior_p)
    v = report["validation"]
    model["live_enabled"] = bool(b > 0 and model["allocation_weight"] > 0 and
        v["combined"]["brier"] <= v["player_only"]["brier"] and v["combined"]["log_loss"] <= v["player_only"]["log_loss"])
    if "previous_combined" in v:
        model["live_enabled"] = bool(model["live_enabled"] and v["combined"]["brier"] <= v["previous_combined"]["brier"]
                                     and v["combined"]["log_loss"] <= v["previous_combined"]["log_loss"])
    report["promotion_rule"] += " Also requires validation Brier and log loss no worse than the previous combined model on identical candidates."
    if roles:
        report['limitations'].append('Role features use last two observed team games, including absent player rows as zero measured work; missing whole-team box scores remain a coverage limitation. Allocation mixes recent and longer workload equally, fixed before this evaluation.')
        if 'previous_combined' in v:
            model['live_enabled'] = bool(model['live_enabled'] and v['combined']['one_per_game']['hits'] >= v['previous_combined']['one_per_game']['hits'])
        report['promotion_rule'] += ' Role candidate also requires no reduction in one-per-game validation wins.'
    if input_profile:
        report['limitations'].append('New channel/current-opportunity variants were declared before evaluation; Nov 14 outcomes are excluded from training and evaluation. Previously examined seasons are retrospective validation, not fresh prospective evidence.')
        model['live_enabled'] = bool(model['live_enabled'] and 'previous_combined' in v and
            v['combined']['one_per_game']['hits'] > v['previous_combined']['one_per_game']['hits'])
        report['promotion_rule'] += ' v7 also requires strictly more one-per-game validation wins than the unchanged baseline.'
    report["feature_coverage"] = {key: {name: sum(r["features"].get(name) is not None for r in part)/len(part)
        for name in RICH_FEATURES + FREE_FEATURES + (SITUATIONAL_FEATURES if situational else [])} for key, part in parts.items()}
    if player_matchups:
        report['matchup_feature_coverage'] = {key:{name:sum(r['features'].get(name) is not None for r in part)/len(part)
            for name in PLAYER_MATCHUPS + (NFL_EXTRA_FEATURES if league=='nfl' else []) if 'interaction' not in name}
            for key,part in parts.items()}
        prior = v.get('previous_combined',{})
        model['live_enabled'] = bool(model['live_enabled'] and prior and
            v['combined']['one_per_game']['hits'] > prior['one_per_game']['hits'])
        report['promotion_rule'] += ' v9 requires strictly more validation game winners; no threshold changes after evaluation.'
        report['limitations'].append('Player-role by defense interactions and current scoring-area allocation are fitted. Coverage splits, full routes and injury replacement shares are not measured; no fabricated adjustments.')
    report["live_enabled"] = model["live_enabled"]
    model["validation"] = report
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{league}_{model['version']}.json").write_text(json.dumps(model, indent=2) + "\n")
    print(json.dumps({"league": league, **report}, indent=2), flush=True)
    return model


def opportunity_time(value):
    from spe.db.base import as_utc
    return as_utc(value)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--league", choices=("all","nfl","fbs"), default="all")
    parser.add_argument("--situational", action="store_true")
    parser.add_argument("--roles", action="store_true")
    args = parser.parse_args()
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        markets = {}
        for line in db.execute("SELECT game_id,market_id,home_line,source FROM closing_lines WHERE market_id IN ('point_spread','total')"):
            entry = markets.setdefault(line["game_id"], {})
            if line["market_id"] == "total":
                entry["total"] = line["home_line"]
            else:
                entry["home_handicap"] = -line["home_line"] if line["source"] == "nflverse_consensus" else line["home_line"]
        archive = Path("data/cache/desk_market_archive.json")
        if archive.exists():
            markets.update(json.loads(archive.read_text()))
        teams = [dict(r) for r in db.execute("SELECT team_id,league_id,aliases FROM teams")]
        for team in teams:
            team["aliases"] = json.loads(team["aliases"] or "[]")
        aliases = canonical_team_map([SimpleNamespace(**r) for r in teams])
        for league in (("nfl", "fbs") if args.league == "all" else (args.league,)):
            records = [dict(r) for r in db.execute(HISTORY_SQL, {"league": league, "start": "2023-07-01", "cutoff": "2026-11-14"})]
            for r in records:
                if not r.pop("charted"):
                    r["gl_carries"] = r["rz_targets"] = None
            run(league, records, args.output, aliases, markets, args.situational, args.roles)
