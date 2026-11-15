"""Team scoring allocation plus a fitted player model, independently implemented.

The allocation pool comes from pregame history, never the bookmaker's player list.
Passing touchdowns belong to receivers, not to the quarterback's ATD outcome.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import numpy as np

from spe.config import settings
from spe.models.player_td import desk_opportunity as opportunity

VERSION = "desk_combined_v4"
from spe.models.player_td.availability_status import INACTIVE
TEAM_FEATURES = ["team_rush_td", "team_rec_td", "opp_rush_td", "opp_rec_td", "is_home"]


def sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -25, 25)))


def logit(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def team_vector(f, with_market=False, with_rich=False, with_situational=False):
    values = [1.0] + [math.log1p(max(0, f[k])) for k in TEAM_FEATURES[:-1]] + [f["is_home"]]
    if with_market:
        values += [f.get("market_team_spread", 0)/14, f.get("market_game_total", 0)/50,
                   f.get("market_spread_known", 0), f.get("market_total_known", 0)]
    if with_rich:
        from spe.models.player_td.desk_evidence import TEAM_RICH_FEATURES
        values.extend(float(f.get(k) or 0) for k in TEAM_RICH_FEATURES)
    if with_situational:
        from spe.models.player_td.situational import SITUATIONAL_FEATURES
        values.extend(float(f.get(k) or 0) for k in SITUATIONAL_FEATURES)
    return np.array(values)


def opportunity_vector(f, kind, league, with_free=False, with_role=False, input_profile=None, matchups=False):
    if input_profile == 'current':
        from spe.models.player_td.current_opportunities import allocation_vector
        fallback = opportunity_vector(f, kind, league, with_free, with_role)
        return np.array(allocation_vector(f, kind, fallback, input_profile))
    if matchups and f.get('role_charting_known'):
        f = dict(f)
        f['gl_carries'] = .5*float(f.get('gl_carries') or 0)+.5*f['role_inside5_carries']
        f['rz_targets'] = .5*float(f.get('rz_targets') or 0)+.5*f['role_red_zone_targets']
    if with_role:
        f = dict(f)
        measured_carries = float(f.get('free_carries') or 0)
        for stat in ('carries', 'targets', 'receptions'):
            # Blend current team-game workload with longer history; conversion
            # rates and final probability coefficients are fitted chronologically.
            f[stat] = .5*float(f.get(stat) or 0)+.5*float(f.get('role_'+stat) or 0)
        if with_free:
            f['free_carries'] = .5*measured_carries+.5*float(f.get('role_carries') or 0)
            f['free_inside5_carries'] = min(float(f.get('free_inside5_carries') or 0), f['free_carries'])
            # College boxes have catches, not targets. Never overwrite measured
            # PBP targets with the box feed's missing-target zero.
    if with_free and kind == "rush" and f.get("rush_pbp_known"):
        area = max(0, f["free_inside5_carries"])
        return np.array([max(0, f["free_carries"]-area), area, 0.])
    if with_free and kind == "rec" and f.get("target_pbp_known"):
        area = max(0, f["free_inside20_targets"])
        return np.array([max(0, f["free_targets"]-area), area, 0.])
    if kind == "rush":
        area = min(max(0, f['carries']), max(0, f.get("gl_carries", 0))) if f.get("charting_known") else 0
        return np.array([max(0, f["carries"] - area), area, 0.0])
    if league == "nfl" and f.get("targets_known", f.get("targets", 0) > 0):
        area = min(max(0, f['targets']), max(0, f.get("rz_targets", 0))) if f.get("charting_known") else 0
        return np.array([max(0, f["targets"] - area), area, 0.0])
    return np.array([0.0, 0.0, max(0, f["receptions"])])


class AllocationHistory:
    def __init__(self, history):
        self.history = history
        self.by_team = defaultdict(list)
        self.by_player = defaultdict(list)
        for (player, team), rows in history.players.items():
            self.by_team[team].append(player)
            self.by_player[player].extend(rows)

    def pool(self, team, cutoff, with_free=False):
        """Include unpriced and one-game teammates; reserve covers unknown roles."""
        pool = {}
        for player in self.by_team.get(team, []):
            own = self.history.recent(self.history.players[(player, team)], cutoff)
            if not own or cutoff - own[-1]["kickoff_utc"] > timedelta(days=21):
                continue
            latest = self.history.recent(sorted(self.by_player[player], key=lambda r: r["kickoff_utc"]), cutoff)
            if latest and latest[-1]["team_id"] != team:
                continue
            f = {k: opportunity._mean(own, k) for k in ("carries", "targets", "receptions", "gl_carries", "rz_targets")}
            f["charting_known"] = any(r.get("gl_carries") is not None for r in own)
            f["targets_known"] = any(r.get("targets") is not None for r in own)
            from spe.models.player_td.role_features import recent_role
            f.update(recent_role(self.history, player, team, cutoff))
            from spe.models.player_td.matchup_features import features as matchup_features
            f.update(matchup_features(self.history, player, team, '', own[-1]['position'], cutoff))
            if with_free and getattr(self.history, "evidence", None):
                f.update(self.history.evidence.features(team, "", cutoff, player, own))
                from spe.models.player_td.current_opportunities import current_features
                f.update(current_features(self.history, player, team, own[-1]['position'], cutoff))
            pool[player] = f
        return pool

    def allocate(self, model, team, opponent, cutoff, is_home, unavailable=(), market=None):
        offense = self.history.recent(self.history.teams.get(team, []), cutoff)
        defense = self.history.recent(self.history.defenses.get(opponent, []), cutoff)
        if min(len(offense), len(defense)) < 2:
            return None
        f = {"is_home": float(is_home)}
        if market:
            from spe.models.player_td.desk_market import market_features
            f.update(market_features(market, is_home, model.get("market_total_trained", True)))
        for kind, stat in (("rush", "rush_td"), ("rec", "rec_td")):
            f["team_" + stat] = opportunity._mean(offense, stat)
            f["opp_" + stat] = opportunity._mean(defense, stat)
        if model.get("rich_features") and getattr(self.history, "evidence", None):
            f.update(self.history.evidence.features(team, opponent, cutoff))
        pool = self.pool(team, cutoff, model.get("free_opportunities", False))
        excluded = set(unavailable)
        players = {p: {"rush_lambda": 0.0, "rec_lambda": 0.0} for p in pool if p not in excluded}
        totals, residual = {}, {}
        for kind in ("rush", "rec"):
            lam = float(np.exp(np.clip(team_vector(f, model.get("market_features", False), model.get("rich_features", False), model.get("situational_features", False)) @ model["team_coefficients"][kind], -8, 3)))
            weights = {p: float(opportunity_vector(pf, kind, model["league"], model.get("free_opportunities", False), model.get('role_features', False), model.get('input_profile'), model.get('player_matchups',False)) @ model["allocation_rates"][kind])
                       for p, pf in pool.items() if p not in excluded}
            denom = sum(weights.values())
            reserve = model["residual_share"][kind] if denom > 0 else 1.0
            for p, weight in weights.items():
                players[p][kind + "_lambda"] = lam * (1 - reserve) * weight / denom if denom else 0.0
            totals[kind] = lam
            residual[kind] = lam * reserve
        for values in players.values():
            values["probability"] = -math.expm1(-values["rush_lambda"] - values["rec_lambda"])
        result = {"players": players, "team_expected_tds": totals, "unassigned_expected_tds": residual,
                  "pool_size": len(players), "excluded_players": sorted(excluded)}
        if model.get('scoring_paths'):
            from spe.models.player_td.scoring_paths import project
            result['scoring_paths'] = project(self.history, model, team, opponent, cutoff, result, excluded, market)
        return result


def combined_prediction(model, features, allocation, player):
    if allocation is None or player not in allocation["players"]:
        return None
    from spe.models.player_td.current_opportunities import transform
    bottom = opportunity.predict(model["player_model"], transform(features, model.get('input_profile')))
    top = allocation["players"][player]["probability"]
    weight = model["allocation_weight"]
    raw = (1 - weight) * logit(bottom) + weight * logit(top)
    chance = float(sigmoid(model["calibration_intercept"] + model["calibration_slope"] * raw))
    baseline_chance = chance
    scorer = model.get('scorer_model')
    if scorer:
        from spe.models.player_td.scorer_candidate import predict as scorer_predict
        chance = scorer_predict(scorer, features, float(raw))
    matchup_terms = {}
    if model.get('player_matchups'):
        from spe.models.player_td.matchup_features import fitted_contributions
        matchup_terms = {'fitted_matchup_terms':fitted_contributions(model,features)}
    path_detail = {}
    path = (allocation.get('scoring_paths') or {}).get('players', {}).get(player)
    if model.get('scoring_paths'):
        if path:
            from spe.models.player_td.scoring_paths import calibrated
            baseline_chance = chance
            chance = calibrated(model, baseline_chance, path['probability'])
            scenarios = {k: calibrated(model, baseline_chance, v) for k,v in path['scenario_probabilities'].items()}
            path_detail = {'scoring_paths': {**path, 'calibrated_scenarios': scenarios,
                'probability_low': min(scenarios.values()), 'probability_high': max(scenarios.values()),
                'baseline_probability': baseline_chance, 'status': 'active'},
                'scoring_paths_active': True}
        else:
            path_detail = {'scoring_paths_active': False,
                'scoring_paths': {'status': 'missing_recent_pbp',
                    'note': 'Kept the existing probability; detailed path history is unavailable.'}}
    return {"player_probability": bottom, "allocation_probability": top, **matchup_terms, **path_detail,
            "allocation_weight": weight, **allocation["players"][player],
            "team_expected_tds": allocation["team_expected_tds"],
            "unassigned_expected_tds": allocation["unassigned_expected_tds"],
            "pool_size": allocation["pool_size"], "excluded_players": allocation["excluded_players"],
            "probability": chance, "combined_probability": chance,
            **({'baseline_combined_probability':baseline_chance,
                'nonlinear_scorer_active':True,'nonlinear_scorer_weight':scorer['tree_weight']} if scorer else {})}


def load_combined_model(league, *, research_candidate=False, version=None):
    suffix = "_candidate" if research_candidate else ""
    path = Path(settings.parquet_dir) / "models" / f"{league}_{version or VERSION}{suffix}.json"
    if version is None and not research_candidate:
        advanced = path.with_name(f'{league}_desk_combined_v8.json')
        if advanced.exists():
            path = advanced
    if version and not path.exists():
        return None
    if not path.exists():
        path = opportunity.BUNDLED_MODEL_DIR / path.name
    if not path.exists():
        path = Path(settings.parquet_dir) / "models" / f"{league}_desk_combined_v3{suffix}.json"
    if not path.exists() and not research_candidate:
        path = Path(settings.parquet_dir) / "models" / f"{league}_desk_combined_v2.json"
        if not path.exists():
            path = opportunity.BUNDLED_MODEL_DIR / path.name
    try:
        body = path.read_bytes()
        m = json.loads(body)
        if m.get("version") not in (VERSION, "desk_combined_v10", "desk_combined_v9", "desk_combined_v8", "desk_combined_v7", "desk_combined_v6", "desk_combined_v5", "desk_combined_v3", "desk_combined_v2") or m.get("league") != league or (not research_candidate and not m.get("live_enabled")):
            return None
        if m.get('version') == 'desk_combined_v10':
            from spe.models.player_td.scoring_paths import validate
            if league != 'nfl' or not validate(m.get('scoring_paths')):
                return None
        if m.get('version') == 'desk_combined_v8':
            from spe.models.player_td.scorer_candidate import validate
            if not validate(m.get('scorer_model') or {}):
                return None
            if not research_candidate and not m.get('validation',{}).get('promotion_gate_passed'):
                return None
        from spe.models.player_td.desk_market import MARKET_FEATURES
        expected_features = opportunity.FEATURES + (MARKET_FEATURES if m.get("market_features") else [])
        from spe.models.player_td.desk_evidence import RICH_FEATURES, TEAM_RICH_FEATURES
        if m.get("rich_features"):
            expected_features += RICH_FEATURES
        if m.get("free_opportunities"):
            from spe.models.player_td.desk_evidence import FREE_FEATURES
            expected_features += FREE_FEATURES
        from spe.models.player_td.situational import SITUATIONAL_FEATURES
        if m.get("situational_features"):
            expected_features += SITUATIONAL_FEATURES
        if m.get('role_features'):
            from spe.models.player_td.role_features import ROLE_FEATURES
            expected_features += ROLE_FEATURES
        if m.get('input_profile') == 'current':
            from spe.models.player_td.current_opportunities import FEATURES as CURRENT_FEATURES
            expected_features += CURRENT_FEATURES
        if m.get('player_matchups'):
            from spe.models.player_td.matchup_features import FEATURES as PLAYER_MATCHUPS
            expected_features += PLAYER_MATCHUPS
            if league == 'nfl':
                from spe.models.player_td.nflverse_context import NFL_EXTRA_FEATURES
                expected_features += NFL_EXTRA_FEATURES
        if m["player_model"]["features"] != expected_features:
            return None
        # Reject malformed/nonfinite artifacts instead of silently emitting bad prices.
        numeric = [m["allocation_weight"], m["calibration_intercept"], m["calibration_slope"]]
        base = m["player_model"]
        for name in ("coefficients", "center", "scale"):
            if len(base[name]) != len(expected_features):
                return None
            numeric += base[name]
        numeric += [base["intercept"]]
        if min(base["scale"]) <= 0:
            return None
        for kind in ("rush", "rec"):
            expected_team = (10 if m.get("market_features") else 6) + (len(TEAM_RICH_FEATURES) if m.get("rich_features") else 0)
            if m.get("situational_features"):
                expected_team += len(SITUATIONAL_FEATURES)
            if len(m["team_coefficients"][kind]) != expected_team or len(m["allocation_rates"][kind]) != 3:
                return None
            numeric += m["team_coefficients"][kind] + m["allocation_rates"][kind] + [m["residual_share"][kind]]
            if not 0 <= m["residual_share"][kind] <= 1 or min(m["allocation_rates"][kind]) < 0:
                return None
        if not all(math.isfinite(v) for v in numeric) or not 0 <= m["allocation_weight"] <= 1 or m["calibration_slope"] <= 0:
            return None
        m["artifact_sha256"] = hashlib.sha256(body).hexdigest()
        return m
    except (OSError, ValueError, KeyError, TypeError):
        return None
