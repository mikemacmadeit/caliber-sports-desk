"""Evidence requirements for a researched alternative, separate from probability.

These are declared editorial coverage checks, not fitted probability bonuses or
proven accuracy thresholds. They prevent a price alone from becoming a pick.
"""
import math


def number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def assess(row):
    analysis = row.get('desk_analysis') or {}
    f = analysis.get('features') or {}
    projection = (analysis.get('combination') or {}).get('scoring_paths') or {}
    paths = projection.get('paths') or []
    reasons, cautions, missing = [], [], []
    recent_games = f.get('recent_measured_opportunity_games') or 0
    recent_touches = f.get('recent_measured_touches')
    current_role = recent_games >= 2 and number(recent_touches) and recent_touches > 0
    if current_role:
        reasons.append({'kind':'current_role', 'text':f"Recorded offensive work in the last two team games averages {recent_touches:.1f} carries plus passes thrown his way per game.",
            'source':'prior-game play-by-play', 'window':'latest two team games'})
    else:
        missing.append('A current offensive role is not supported by two measured team games.')
    if f.get('snap_known') and number(f.get('snap_share')):
        reasons.append({'kind':'playing_time', 'text':f"Played {f['snap_share']:.0%} of measured offensive snaps in the recent window.",
            'source':'PFR snap counts via nflverse', 'window':'up to six prior games'})
    near_keys = {'rush_5','rush_10','rush_20','rec_endzone','rec_redzone'}
    near = [p for p in paths if p['path'] in near_keys and p.get('player_sample',0) >= 2
        and p.get('recent_share',0) >= .10 and p.get('projected_opportunities',0) >= .25]
    deep = [p for p in paths if p['path']=='rec_deep' and p.get('player_sample',0)>=4
        and p.get('projected_opportunities',0)>=1 and p.get('opponent_position_sample',0)>=10
        and p.get('opponent_rate_ratio',0)>=1.1]
    route = bool(projection.get('status')=='active' and (near or deep))
    if near:
        best = max(near, key=lambda p:p.get('unscaled_expected_tds',p['projected_opportunities']))
        reasons.append({'kind':'scoring_area', 'text':f"{best['recent_share']:.0%} of the team's recent {best['label']}, with {best['player_sample']:.0f} measured opportunities in the longer window.",
            'source':'prior-game play-by-play; shares use declared small-sample smoothing', 'window':'two-game share; up to six-game opportunity count'})
    elif deep:
        reasons.append({'kind':'deep_matchup', 'text':'A measured downfield receiving role meets a defense allowing above-benchmark touchdown conversion on deep targets to this position.',
            'source':'prior-game play-by-play', 'window':'up to six prior games'})
    else:
        missing.append('No sufficiently measured recent scoring-area role or supported deep-receiving matchup.')
    for p in paths:
        if p.get('opponent_position_sample',0)>=10 and p.get('opponent_rate_ratio',0)>=1.1 and p.get('projected_opportunities',0)>=.25:
            reasons.append({'kind':'matchup', 'text':f"The opponent has allowed above-benchmark touchdown conversion on {p['label']} to this position after small-sample adjustment.",
                'source':'prior-game play-by-play', 'window':'up to six prior games'})
            break
    if paths:
        lead = paths[0]
        if lead.get('opponent_position_sample',0)>=5 and lead.get('opponent_rate_ratio',1)<.9:
            cautions.append('The opponent has been relatively tough against the main scoring opportunity; this is not an obvious matchup advantage.')
        if lead.get('recent_share',0)<lead.get('longer_share',0)-.1:
            cautions.append('The share of the main scoring opportunity has fallen in the last two team games.')
    old = (f.get('carries') or 0)+(f.get('targets') or 0)
    declining = current_role and old>0 and recent_touches<old*.5
    if declining:
        missing.append('Recent measured workload is less than half the longer-term average; a replacement role needs confirmation.')
    context=analysis.get('current_context') or {}
    if (context.get('availability') or {}).get('status') in ('questionable','uncertain'):
        missing.append('The injury designation is unresolved.')
    return {'version':'football_evidence_v1','supported':bool(current_role and route and not missing),
        'reasons':reasons, 'cautions':cautions, 'missing':missing,
        'note':'Evidence coverage and editorial selection rules; not an additional probability adjustment or validated win-rate improvement.'}
