"""Pregame football cases from measured PBP, with no outcome or price inputs.

Coverage and routes are not inferred from targets. Counts are contextual evidence,
not independent samples or proof of an edge. QB conditioning uses a documented
starter when supplied, otherwise the last game's dominant passer, explicitly labeled.
"""
from collections import defaultdict

from spe.analytics.td_scoring_paths import PATHS

REC = tuple(p for p in PATHS if p.startswith('rec'))
RUSH = tuple(p for p in PATHS if p.startswith('rush'))
RZ = ('rec_inside20',)


def count(series, paths, field='opportunities'):
    return sum(float(m.get('buckets', {}).get(p, {}).get(field, 0)) for m in series for p in paths)


def subset(series, section, key):
    return [{'buckets':m.get(section, {}).get(key, {})} for m in series]


def expected_passer(offense, excluded=(), starter=None):
    blocked = {str(p).removeprefix('nfl:') for p in excluded}
    if starter and str(starter).removeprefix('nfl:') not in blocked:
        return str(starter).removeprefix('nfl:'), 'current_depth_chart'
    recent = offense[-1].get('passers', {}) if offense else {}
    counts = {p:count([{'buckets':b}], REC) for p,b in recent.items()}
    if not counts:
        return None, 'unavailable'
    passer = max(counts, key=counts.get)
    if passer in blocked or counts[passer] < 10 or counts[passer]/max(1,sum(counts.values())) < .60:
        return None, 'unresolved_starter'
    return passer, 'last_game_dominant_passer'


def build(offense, defense, players, excluded=(), context=None):
    context = context or {}
    qb, basis = expected_passer(offense, excluded, context.get('starting_qb_id'))
    team_qb = subset(offense, 'passers', qb) if qb else []
    qb_games = sum(count([m], REC)>0 for m in team_qb)
    qb_total = count(team_qb, REC)
    qualified = bool(qb and qb_games >= 2 and qb_total >= 30)
    summaries = {}
    for pid, info in players.items():
        own = info['series']
        own_qb = subset(own, 'passers', qb) if qb else []
        pos = info['position']
        allowed = subset(defense, 'by_position', pos)
        channel = RUSH if pos in ('RB','QB') and count(own,RUSH)>=count(own,REC) else REC
        long_share = count(own,channel)/max(1,count(offense,channel))
        recent_share = count(own[-2:],channel)/max(1,count(offense[-2:],channel))
        script = {}
        for state in ('leading','close','trailing'):
            team_state = subset(offense,'states',state)
            own_state = subset(own,'states',state)
            n = count(team_state,channel)
            script[state] = {'team_opportunities':n,'player_opportunities':count(own_state,channel),
                'share':count(own_state,channel)/n if n else None}
        summaries[pid] = {
            'version':'matchup_research_v1','games':len(offense), 'opponent_games':len(defense),
            'quarterback':{'player_id':'nfl:'+qb if qb else None,'basis':basis,'usable':qualified,
                'games':qb_games,'team_targets':qb_total,'player_targets':count(own_qb,REC),
                'team_redzone_targets':count(team_qb,RZ),'player_redzone_targets':count(own_qb,RZ),
                'player_endzone_targets':count(own_qb,('rec_endzone',)),
                'receiving_tds':count(own_qb,REC,'tds'),
                'target_share':count(own_qb,REC)/qb_total if qb_total else None,
                'redzone_share':count(own_qb,RZ)/max(1,count(team_qb,RZ)),
                'note':'End-zone targets are estimates from field position and air yards; this is a prior-game passer split, not a guaranteed starting assignment.'},
            'role':{'channel':'carries' if channel==RUSH else 'targets','recent_share':recent_share,
                'longer_share':long_share,'recent_opportunities':count(own[-2:],channel),
                'recent_touches':count(own[-2:],RUSH+REC),
                'redzone_targets':count(own,RZ),'endzone_targets':count(own,('rec_endzone',)),
                'inside5_carries':count(own,('rush_5',))},
            'opponent':{'position':pos,'targets':count(allowed,REC),'receiving_yards':count(allowed,REC,'yards'),
                'receiving_tds':count(allowed,REC,'tds'),'carries':count(allowed,RUSH),
                'rushing_yards':count(allowed,RUSH,'yards'),'rushing_tds':count(allowed,RUSH,'tds'),
                'redzone_targets':count(allowed,RZ),'endzone_targets':count(allowed,('rec_endzone',))},
            'game_states':script,
            'missing':['Current full-route participation','Man/zone, blitz and safety-coverage splits','Verified upcoming play calls'],
            'source':'nflverse prior-game play-by-play',
        }
    for pid, value in summaries.items():
        qb = value['quarterback']
        qb['redzone_team_rank'] = 1+sum(v['quarterback']['player_redzone_targets']>qb['player_redzone_targets'] for v in summaries.values())
        role = value['role']
        for field in ('redzone_targets','endzone_targets','inside5_carries'):
            role[field+'_rank'] = 1+sum(v['role'][field]>role[field] for v in summaries.values())
    return summaries


def contextual_share(share, path, own, offense, research):
    """Eight team opportunities of prior weight; receiving shares still conserve mass.

    This is used only by the separately recalibrated v11 probability artifact.
    Unknown/one-game QB situations leave the old estimate unchanged.
    """
    qb = research['quarterback']
    if not path.startswith('rec') or not qb['usable']:
        return share
    pid = qb['player_id'].removeprefix('nfl:')
    n = count(subset(offense,'passers',pid),(path,))
    own_n = count(subset(own,'passers',pid),(path,))
    posterior = (own_n+8*share)/(n+8)
    return .5*share+.5*posterior


def attach_unfitted_research(combined, allocation, player, artifact_sha):
    """Keep research durable when the probability champion has no path model.

    The research can select an alternative, but cannot silently recalibrate the
    champion or claim scenario-tested value at a quote.
    """
    path=(allocation.get('scoring_paths') or {}).get('players',{}).get(player)
    if not path:
        return combined
    return {**combined, 'scoring_paths_active':False, 'scoring_paths':{
        **path,'status':'research_only','research_artifact_sha256':artifact_sha,
        'baseline_probability':combined['probability'],'probability_low':combined['probability'],
        'probability_high':combined['probability'],
        'note':'Research influences alternative selection only. The displayed probability retains the independently validated model; path scenarios have not been calibrated for this model.'}}


def case_score(row, football):
    """Declared editorial ordering, never displayed as a probability.

    Half of the score comes from the forecast. Remaining terms reward observable
    scoring-area work, opponent fit, QB-specific role and a rising current role.
    Weights are fixed before replay, not tuned to Sharp's choices or today's scores.
    """
    paths = ((row.get('desk_analysis') or {}).get('combination') or {}).get('scoring_paths') or {}
    research = paths.get('research') or {}
    p = row['forecast']['p_estimate']
    near = [r for r in paths.get('paths',[]) if r['path'] in ('rush_5','rush_10','rec_endzone','rec_redzone')]
    opportunities = sum(r['projected_opportunities'] for r in near)
    favorable = [r for r in paths.get('paths',[]) if r['opponent_position_sample']>=10 and r['opponent_rate_ratio']>=1.1 and r['projected_opportunities']>=.25]
    qb = research.get('quarterback') or {}
    qb_edge = bool(qb.get('usable') and qb.get('player_redzone_targets',0)>=3 and qb.get('redzone_team_rank',99)<=2)
    role = research.get('role') or {}
    rising = role.get('recent_share',0)-role.get('longer_share',0)>=.10 and role.get('recent_opportunities',0)>=4
    spread=((row.get('desk_analysis') or {}).get('features') or {}).get('market_team_spread')
    state='leading' if isinstance(spread,(int,float)) and spread<=-7 else 'trailing' if isinstance(spread,(int,float)) and spread>=7 else None
    scenario=(research.get('game_states') or {}).get(state) or {}
    script_edge=bool(scenario.get('team_opportunities',0)>=10 and
        (scenario.get('share') or 0)>=role.get('longer_share',0)+.10 and opportunities>=.5)
    concentrated = any(r['recent_share']>=.25 and r['player_sample']>=3 for r in near)
    reasons = []
    if favorable: reasons.append('opponent_scoring_area_weakness')
    if qb_edge: reasons.append('quarterback_redzone_role')
    if rising: reasons.append('growing_role')
    if concentrated: reasons.append('concentrated_scoring_role')
    if script_edge: reasons.append('game_script_role')
    score = .50*p + .20*min(opportunities/2,1) + .15*bool(favorable) + .10*qb_edge + .05*bool(rising or script_edge)
    return {'score':round(score,6),'supported':bool(football['supported'] and reasons and p>=.15),
        'advantages':reasons,'scoring_area_opportunities':opportunities,
        'note':'Editorial research ranking, not a scoring probability or demonstrated betting edge.'}


def paragraphs(row):
    analysis = row.get('desk_analysis') or {}
    paths = (analysis.get('combination') or {}).get('scoring_paths') or {}
    r = paths.get('research') or {}
    if not r:
        return []
    name = row['player_name']; facts=[]; role=r['role']; qb=r['quarterback']; opponent=r['opponent']
    if qb.get('usable') and qb['player_targets']:
        passer = qb.get('name') or 'the expected quarterback'
        status = 'the current depth-chart starter' if qb['basis']=='current_depth_chart' else 'the last game’s main passer; his starting assignment still needs confirmation'
        facts.append(f"With {passer} ({status}), {name} received {qb['player_targets']:.0f} of {qb['team_targets']:.0f} targeted passes across {qb['games']} prior games. That includes {qb['player_redzone_targets']:.0f} red-zone targets, ranking {qb['redzone_team_rank']} on the team in that sample, and {qb['receiving_tds']:.0f} receiving touchdowns.")
    if role['recent_opportunities']:
        facts.append(f"His share of the team's {role['channel']} is {role['recent_share']:.0%} over the last two games, compared with {role['longer_share']:.0%} over {r['games']} games.")
    receiving = sum(p['projected_opportunities'] for p in paths.get('paths',[]) if p['path'].startswith('rec')) > sum(p['projected_opportunities'] for p in paths.get('paths',[]) if p['path'].startswith('rush'))
    if receiving and opponent['targets']:
        facts.append(f"Over {r['opponent_games']} measured games, {row['opponent']} allowed this position {opponent['receiving_yards']/r['opponent_games']:.1f} receiving yards per game, {opponent['receiving_yards']/opponent['targets']:.1f} yards per target and {opponent['receiving_tds']:.0f} receiving touchdowns. Those totals describe the position matchup; they do not identify the defender covering him.")
    elif opponent['carries']:
        facts.append(f"Over {r['opponent_games']} measured games, {row['opponent']} allowed this position {opponent['rushing_yards']/opponent['carries']:.1f} yards per carry and {opponent['rushing_tds']:.0f} rushing touchdowns.")
    spread=(analysis.get('features') or {}).get('market_team_spread')
    state = 'leading' if isinstance(spread,(int,float)) and spread<=-7 else 'trailing' if isinstance(spread,(int,float)) and spread>=7 else None
    if state and r['game_states'][state]['team_opportunities']>=10:
        s=r['game_states'][state]
        facts.append(f"His team is a {'favorite' if state=='leading' else 'underdog'} of at least a touchdown. In prior plays while {state} by eight or more points, he handled {s['share']:.0%} of the team's {role['channel']} ({s['player_opportunities']:.0f} of {s['team_opportunities']:.0f}). That is a scenario to consider, not a forecast of the coach's play calls.")
    return facts
