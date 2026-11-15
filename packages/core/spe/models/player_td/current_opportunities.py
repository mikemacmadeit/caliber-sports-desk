"""Channel-specific opportunity inputs shared by training and serving.

Only completed prior games enter these features. Missing receiving identities do
not discard a well-covered rushing channel. No game outcome enters a feature.
"""
FEATURES = ['current_carries', 'current_targets', 'current_inside5_carries',
    'current_inside20_targets', 'current_rush_known', 'current_target_known',
    'current_carry_share', 'current_target_share', 'current_qb_carries',
    'current_qb_inside5_carries', 'role_carries', 'role_receptions',
    'role_recent_appearance_rate']


def current_features(history, player, team, position, cutoff):
    result = {k: None for k in FEATURES[:4]}
    result.update(current_rush_known=0., current_target_known=0.,
                  current_carry_share=None, current_target_share=None,
                  current_qb_carries=None, current_qb_inside5_carries=None)
    evidence = getattr(history, 'evidence', None)
    if evidence is None:
        return result
    # Use the latest two TEAM games. Never skip an absent player's recent game
    # to reach back to an old starter's appearances.
    games = history.recent(history.teams.get(team, []), cutoff)[-2:]
    if len(games) < 2:
        return result
    observed = {r['game_id'].split(':')[-1]: r
        for r in evidence.recent(evidence.players.get((player, team), []), cutoff)}
    for kind, count, area, share in (
        ('rush', 'carries', 'inside5_carries', 'current_carry_share'),
        ('target', 'targets', 'inside20_targets', 'current_target_share')):
        values, totals = [], []
        for game in games:
            gid = game['game_id'].split(':')[-1]
            team_box = evidence.game_teams.get((gid, team))
            if not team_box or not evidence.recent([team_box], cutoff):
                continue
            coverage = min(team_box.get(kind+'_coverage') or 0, team_box.get('field_coverage') or 0)
            if coverage < .95:
                continue
            player_box = observed.get(gid)
            if player_box and (player_box.get(kind+'_coverage') or 0) < .95:
                continue
            # Complete charting establishes zero touches, not active status.
            values.append((float((player_box or {}).get(count) or 0), float((player_box or {}).get(area) or 0)))
            totals.append(float(team_box.get('rushes' if kind=='rush' else 'targets') or 0))
        if len(values) == 2:
            n, area_count = (sum(v[i] for v in values)/2 for i in (0,1))
            result.update({f'current_{count}':n, f'current_{area}':area_count,
                           f'current_{kind}_known':1., share:min(1., n/max(sum(totals)/2, 1.))})
    if position == 'QB' and result['current_rush_known']:
        result['current_qb_carries'] = result['current_carries']
        result['current_qb_inside5_carries'] = result['current_inside5_carries']
    return result


def transform(features, profile):
    """Keep existing model inputs untouched; only new fitted profiles opt in."""
    if profile not in ('channels', 'current'):
        return features
    f = dict(features)
    if f.get('rush_pbp_known'):
        f.update(carries=f['free_carries'], gl_carries=f['free_inside5_carries'],
                 measured_inside5_carries=f['free_inside5_carries'],
                 measured_inside20_carries=f['free_inside20_carries'])
    if f.get('target_pbp_known'):
        f.update(targets=f['free_targets'], rz_targets=f['free_inside20_targets'],
                 measured_targets=f['free_targets'], measured_inside5_targets=f['free_inside5_targets'],
                 measured_inside20_targets=f['free_inside20_targets'])
    return f


def allocation_vector(features, kind, fallback, profile):
    if profile != 'current':
        return fallback
    if kind == 'rush' and features.get('current_rush_known'):
        n = max(0., features['current_carries']); area = min(n, max(0., features['current_inside5_carries']))
        return [n-area, area, 0.]
    if kind == 'rec' and features.get('current_target_known'):
        n = max(0., features['current_targets']); area = min(n, max(0., features['current_inside20_targets']))
        return [n-area, area, 0.]
    return fallback
