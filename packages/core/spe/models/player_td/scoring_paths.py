"""Current-role scoring allocation with observable, disjoint opportunity paths.

Fixed design: last two TEAM games blended equally with six-game history. A
player's ordinary carries do not create goal-line or red-zone share. Those
scoring-area paths shrink toward no role. Open-field and other paths still
shrink toward channel share. Absence work stays with the same position, with
half held in an unknown-role reserve.
Opponent conversion rates shrink toward fit-period league rates. Scenario
ranges measure sensitivity, not confidence intervals. No outcome-time lookups.
"""
from collections import defaultdict
import math
import numpy as np

from spe.analytics.td_scoring_paths import PATHS, LABELS
from spe.models.player_td.nflverse_context import _available

VERSION = 'scoring_paths_v1'
CONTEXT_VERSION = 'scoring_paths_v2'
SHARE_PRIOR = 3.
CONVERSION_PRIOR = 25.
SCORING_AREA = frozenset({'rush_5', 'rush_10', 'rush_20', 'rec_endzone', 'rec_redzone'})


def role_share(observed, team_observed, channel_share, path):
    """Scoring-area work is counted only when it was actually observed."""
    prior = 0.0 if path in SCORING_AREA else channel_share
    return (observed + SHARE_PRIOR * prior) / (team_observed + SHARE_PRIOR)


def _count(m, path, key='opportunities'):
    return float(((m or {}).get('buckets', {}).get(path) or {}).get(key, 0))


def _sum(m, kind, key='opportunities'):
    return sum(_count(m, p, key) for p in PATHS if p.startswith(kind))


def _weighted(values):
    weights = [.8**i for i in reversed(range(len(values)))]
    return sum(w*v for w,v in zip(weights, values))/sum(weights) if weights else 0.


def prior_rates(records, fit_end):
    counts = defaultdict(lambda: [0., 0.])
    seen = set()
    for r in records:
        if r['kickoff_utc'].isoformat() >= fit_end:
            continue
        key = (r['game_id'], r['player_id'])
        if key in seen:
            continue
        seen.add(key)
        m = r.get('nflverse_context', {}).get('player', {}).get('metrics', {}).get('scoring_paths')
        for path in PATHS:
            counts[path][0] += _count(m, path)
            counts[path][1] += _count(m, path, 'tds')
    # Weak, declared smoothing; no current-season labels enter these priors.
    return {k: {'rate': (v[1]+.5)/(v[0]+10), 'opportunities': v[0], 'tds': v[1]} for k,v in counts.items()}


def _profiles(history, team, opponent, cutoff, pool):
    offense = history.recent(history.teams.get(team, []), cutoff)
    defense = history.recent(history.defenses.get(opponent, []), cutoff)
    def measured(rows):
        return [(r, m['scoring_paths']) for r in rows for m in [_available(r, 'team', cutoff)]
            if m and m.get('field_position_coverage', 0) >= .98 and m.get('scoring_paths')]
    off, deff = measured(offense), measured(defense)
    if min(len(off), len(deff)) < 2 or off[-1][0]['game_id'] != offense[-1]['game_id']:
        return None
    players = {}
    for pid in pool:
        rows = {r['game_id']: r for r in history.recent(history.players.get((pid, team), []), cutoff)}
        series = []
        for r, _ in off:
            own = _available(rows[r['game_id']], 'player', cutoff) if r['game_id'] in rows else None
            series.append((own or {}).get('scoring_paths') or {})
        prior = history.recent(history.players.get((pid, team), []), cutoff)
        players[pid] = {'series': series, 'position': 'RB' if prior[-1]['position'] == 'FB' else prior[-1]['position']}
    return [m for _,m in off], [m for _,m in deff], players


def project(history, model, team, opponent, cutoff, base, unavailable=(), context=None):
    """Keep the fitted team TD budget; change how individual scoring paths earn it."""
    if not base or not model.get('scoring_paths'):
        return None
    from spe.models.player_td.desk_combined import AllocationHistory
    pool = AllocationHistory(history).pool(team, cutoff)
    profiles = _profiles(history, team, opponent, cutoff, pool)
    if not profiles:
        return None
    offense, defense, players = profiles
    excluded = set(unavailable)
    from spe.models.player_td.matchup_research import build, contextual_share
    research = build(offense, defense, players, excluded, context)
    contextual = model['scoring_paths'].get('version') == CONTEXT_VERSION
    priors = model['scoring_paths']['rates']
    live = {p: v for p,v in players.items() if p not in excluded and p in base['players']}
    details = {p: {'paths': [], 'rush_lambda': 0., 'rec_lambda': 0.} for p in live}
    scenario_weights = {name: {p: {'rush': 0., 'rec': 0.} for p in live}
        for name in ('central', 'longer_role', 'recent_role', 'no_inheritance', 'full_inheritance')}
    residuals = {name: {'rush': 0., 'rec': 0.} for name in scenario_weights}
    for path in PATHS:
        kind = path.split('_')[0]
        league_rate = priors[path]['rate']
        team_volume = _weighted([_count(m, path) for m in offense])
        allowed_volume = _weighted([_count(m, path) for m in defense])
        volume = (team_volume + allowed_volume)/2
        # All rates use only previous games. A 2-for-2 defender allowance does
        # not turn into a 100% future conversion rate.
        def rate(series, position=None):
            bins = [({'buckets': m.get('by_position', {}).get(position, {})} if position else m) for m in series]
            n = sum(_count(m, path) for m in bins)
            y = sum(_count(m, path, 'tds') for m in bins)
            return (y+CONVERSION_PRIOR*league_rate)/(n+CONVERSION_PRIOR), n
        shares = {}
        for pid, info in players.items():
            shares[pid] = {}
            for scenario in ('longer_role', 'recent_role'):
                ps = info['series'] if scenario == 'longer_role' else info['series'][-2:]
                ts = offense if scenario == 'longer_role' else offense[-2:]
                channel_share = sum(_sum(m, kind) for m in ps)/max(1., sum(_sum(m, kind) for m in ts))
                shares[pid][scenario] = role_share(sum(_count(m, path) for m in ps), sum(_count(m, path) for m in ts), channel_share, path)
            shares[pid]['central'] = (shares[pid]['longer_role']+shares[pid]['recent_role'])/2
            if contextual:
                for scenario in ('central','longer_role','recent_role'):
                    shares[pid][scenario] = contextual_share(shares[pid][scenario],path,info['series'],offense,research[pid])
        for pid, info in live.items():
            own_rate, own_n = rate(info['series'])
            opp_rate, opp_n = rate(defense, info['position'])
            conversion = min(.95, own_rate * opp_rate / max(league_rate, 1e-6))
            same_position = [p for p,v in live.items() if v['position'] == info['position']]
            vacated = sum(shares[p]['central'] for p,v in players.items() if p in excluded and v['position'] == info['position'])
            position_share = sum(shares[p]['central'] for p in same_position)
            inherited = vacated * shares[pid]['central']/position_share if position_share else 0.
            central_share = shares[pid]['central']+.5*inherited
            for name, weights in scenario_weights.items():
                share = shares[pid].get(name, shares[pid]['central'])
                share += inherited*(0 if name == 'no_inheritance' else 1 if name == 'full_inheritance' else .5)
                weights[pid][kind] += volume*share*conversion
            if volume*central_share > .005:
                details[pid]['paths'].append({'path': path, 'label': LABELS[path],
                    'projected_opportunities': volume*central_share, 'share': central_share,
                    'recent_share': shares[pid]['recent_role'], 'longer_share': shares[pid]['longer_role'],
                    'inherited_share': .5*inherited, 'conversion_rate': conversion,
                    'opponent_rate_ratio': opp_rate/max(league_rate, 1e-6),
                    'player_sample': own_n, 'opponent_position_sample': opp_n,
                    'unscaled_expected_tds': volume*central_share*conversion})
        # Unobserved players and unassigned replacement work retain a share.
        for name, weights in scenario_weights.items():
            allocated_opportunities = 0.
            for pid, info in live.items():
                source = name if name in ('longer_role', 'recent_role') else 'central'
                own_share = shares[pid][source]
                same = [p for p,v in live.items() if v['position'] == info['position']]
                vac = sum(shares[p]['central'] for p,v in players.items() if p in excluded and v['position'] == info['position'])
                denom = sum(shares[p]['central'] for p in same)
                inh = vac*shares[pid]['central']/denom if denom else 0.
                allocated_opportunities += own_share + inh*(0 if name == 'no_inheritance' else 1 if name == 'full_inheritance' else .5)
            residuals[name][kind] += volume*max(0., 1-allocated_opportunities)*league_rate
    scenarios = {}
    for name, weights in scenario_weights.items():
        scenarios[name] = {p: {'rush_lambda': 0., 'rec_lambda': 0.} for p in live}
        for kind in ('rush', 'rec'):
            total = sum(w[kind] for w in weights.values())
            # Keep the fit-period unknown-player reserve even on a full chart.
            reserve_floor = model['residual_share'][kind]
            reserve = max(residuals[name][kind], total*reserve_floor/max(1-reserve_floor, .01))
            for pid in live:
                scenarios[name][pid][kind+'_lambda'] = base['team_expected_tds'][kind]*weights[pid][kind]/max(total+reserve, 1e-9)
    for pid, detail in details.items():
        detail['research'] = research[pid]
        detail['quarterback_conditioning_active'] = contextual and research[pid]['quarterback']['usable']
        detail.update(scenarios['central'][pid])
        detail['probability'] = -math.expm1(-detail['rush_lambda']-detail['rec_lambda'])
        detail['scenario_probabilities'] = {name: -math.expm1(-values[pid]['rush_lambda']-values[pid]['rec_lambda']) for name,values in scenarios.items()}
        detail['history_games'] = len(offense)
        detail['opponent_games'] = len(defense)
        detail['role_uncertainty'] = 'Five declared workload scenarios; not a statistical confidence interval.'
        detail['paths'].sort(key=lambda r: -r['unscaled_expected_tds'])
    return {'version': model['scoring_paths']['version'], 'players': details, 'team_expected_tds': base['team_expected_tds'],
        'excluded_players': sorted(excluded), 'pool_size': len(live),
        'sources': ['nflverse prior-game play-by-play'],
        'limitations': ['No complete routes, man/zone splits or verified upcoming play calls.',
            'Half the absent same-position workload is reserved; replacement shares are scenario assumptions.',
            'Team scoring and market context use the separately fitted team model.']}


def calibrated(model, baseline_probability, path_probability):
    from spe.models.player_td.desk_combined import logit, sigmoid
    weights = model['scoring_paths']['calibration']
    return float(sigmoid(weights[0]+weights[1]*logit(baseline_probability)+weights[2]*logit(path_probability)))


def validate(config):
    if not isinstance(config, dict) or config.get('version') not in (VERSION, CONTEXT_VERSION):
        return False
    try:
        w = config['calibration']
        return (len(w) == 3 and all(math.isfinite(v) for v in w) and min(w[1:]) >= 0
            and set(config['rates']) == set(PATHS)
            and all(0 < config['rates'][k]['rate'] < 1 for k in PATHS))
    except (KeyError, TypeError, ValueError):
        return False
