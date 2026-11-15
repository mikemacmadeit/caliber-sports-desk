"""Measured role-by-opponent interactions. No coverage/route labels are invented."""
import math

POSITION_STATS = ('targets','receptions','rec_yards','carries','rush_yards','gl_carries','rz_targets')
FEATURES = ['position_allowed_'+s for s in POSITION_STATS] + [
    'position_targets_known', 'position_charting_known', 'position_yards_known',
    'position_allowed_yards_per_target', 'position_allowed_yards_per_carry',
    'position_target_fraction', 'position_carry_fraction',
    'role_target_share', 'role_goal_line_share', 'role_red_zone_target_share',
    'role_inside5_carries', 'role_red_zone_targets', 'role_charting_known',
    'projected_carries', 'projected_targets', 'projected_receptions',
    'rush_role_defense_interaction', 'target_role_defense_interaction',
    'rush_role_offense_interaction', 'target_role_offense_interaction',
    'target_role_position_yards_interaction', 'rush_role_position_yards_interaction',
    'goal_line_position_interaction', 'red_zone_position_interaction',
    'target_role_script_interaction', 'rush_role_script_interaction']


def average(rows, key):
    pairs = [(0.8**i,r.get(key)) for i,r in enumerate(reversed(rows)) if r.get(key) is not None]
    return sum(w*v for w,v in pairs)/sum(w for w,_ in pairs) if pairs else None


def features(history, player, team, opponent, position, cutoff):
    result = {k:None for k in FEATURES}
    offense = history.recent(history.teams.get(team,[]),cutoff)
    defense = history.recent(history.defenses.get(opponent,[]),cutoff)
    if len(offense) < 2:
        return result
    pos = 'RB' if position == 'FB' else position
    for stat in POSITION_STATS:
        result['position_allowed_'+stat] = average(defense, pos+'_'+stat)
    for flag,stat in [('targets','targets'),('charting','gl_carries'),('yards','rec_yards')]:
        result['position_'+flag+'_known'] = float(sum(r.get(pos+'_'+stat) is not None for r in defense)>=2)
    def ratio(a,b):
        return a/max(b,1.) if a is not None and b is not None else None
    result['position_allowed_yards_per_target'] = ratio(result['position_allowed_rec_yards'],result['position_allowed_targets'])
    result['position_allowed_yards_per_carry'] = ratio(result['position_allowed_rush_yards'],result['position_allowed_carries'])
    result['position_target_fraction'] = ratio(result['position_allowed_targets'], average(defense,'targets'))
    result['position_carry_fraction'] = ratio(result['position_allowed_carries'], average(defense,'carries'))
    own = {r['game_id']:r for r in history.recent(history.players.get((player,team),[]),cutoff)}
    recent = offense[-2:]
    role = {}
    for stat in ('targets','receptions','carries','gl_carries','rz_targets'):
        values = [own[g['game_id']].get(stat) if g['game_id'] in own else
                  (0. if g.get('measured_'+stat) else None) for g in recent]
        role[stat] = sum(values)/len(values) if all(v is not None for v in values) else None
    for name,stat in [('role_target_share','targets'),('role_goal_line_share','gl_carries'),('role_red_zone_target_share','rz_targets')]:
        total = sum(g.get(stat) or 0 for g in recent)/len(recent)
        result[name] = min(1.,ratio(role[stat],total)) if role[stat] is not None else None
    result.update(role_inside5_carries=role['gl_carries'], role_red_zone_targets=role['rz_targets'],
        role_charting_known=float(role['gl_carries'] is not None and role['rz_targets'] is not None))
    # Simple volume projections are FEATURES whose predictive weights are fitted,
    # not multipliers applied to a probability. No routes are inferred from snaps.
    for stat in ('carries','targets','receptions'):
        share = ratio(role[stat],sum(g.get(stat) or 0 for g in recent)/len(recent))
        off,allowed = average(offense,stat),average(defense,stat)
        result['projected_'+stat] = min(1.,share)*(off+allowed)/2 if share is not None and off is not None and allowed is not None else None
    return result


def interactions(f):
    def product(a,b):
        x,y=f.get(a),f.get(b)
        return x*y if x is not None and y is not None and math.isfinite(x) and math.isfinite(y) else None
    return {
        'rush_role_defense_interaction':product('projected_carries','defense_rush_epa'),
        'target_role_defense_interaction':product('projected_targets','defense_pass_epa'),
        'rush_role_offense_interaction':product('projected_carries','offense_rush_epa'),
        'target_role_offense_interaction':product('projected_targets','offense_pass_epa'),
        'target_role_position_yards_interaction':product('projected_targets','position_allowed_yards_per_target'),
        'rush_role_position_yards_interaction':product('projected_carries','position_allowed_yards_per_carry'),
        'goal_line_position_interaction':product('role_goal_line_share','position_allowed_gl_carries'),
        'red_zone_position_interaction':product('role_red_zone_target_share','position_allowed_rz_targets'),
        'target_role_script_interaction':product('role_target_share','market_team_spread'),
        'rush_role_script_interaction':product('role_carry_share','market_team_spread')}


def fitted_contributions(model, f):
    """Exact standardized linear terms; not causal claims or probability bonuses."""
    from spe.models.player_td.desk_opportunity import vector
    base=model['player_model']; names=base['features']
    values=(vector(f,names)-base['center'])/base['scale']
    return sorted([{'feature':k,'logit_contribution':float(v*w)}
        for k,v,w in zip(names,values,base['coefficients']) if k in FEATURES and abs(v*w)>.015],
        key=lambda r:abs(r['logit_contribution']),reverse=True)[:5]
