"""Short customer copy grounded only in the prediction's saved inputs."""
import math


def _number(value):
    return isinstance(value,(int,float)) and math.isfinite(value)


def _odds(value):
    return f'{value:+.0f}'


def build(row):
    analysis=row.get('desk_analysis') or {}
    f=analysis.get('features') or {}
    combination=analysis.get('combination') or {}
    path=combination.get('scoring_paths') or {}
    decision=row.get('decision') or {}
    selected=decision.get('selected') or {}
    p=row['forecast'].get('p_estimate')
    name=row.get('player_name','This player')
    opponent=row.get('opponent','the opponent')
    points=row.get('team_implied_points')
    position=row.get('position')
    role=[]
    paths=path.get('paths') or []
    carries=sum(r['projected_opportunities'] for r in paths if r['path'].startswith('rush'))
    targets=sum(r['projected_opportunities'] for r in paths if r['path'].startswith('rec'))
    rush=path.get('rush_lambda',combination.get('rush_lambda',0)) or 0
    rec=path.get('rec_lambda',combination.get('rec_lambda',0)) or 0
    if paths:
        if carries >= targets and carries >= 1:
            role.append(f"We project about {max(1,round(carries))} carries for {name}" +
                (f" and {max(1,round(targets))} passes thrown his way." if targets >= 1 else '.'))
        elif targets >= 1:
            role.append(f"We project about {max(1,round(targets))} passes thrown {name}'s way.")
        near=[r for r in paths if r['path'] in ('rush_5','rush_10','rush_20','rec_endzone','rec_redzone')]
        n=sum(r['projected_opportunities'] for r in near)
        if n>=.5:
            role.append(f"That includes roughly {max(1,round(n))} chance{'s' if round(n)>1 else ''} close to the end zone, where a carry or catch can turn into a touchdown.")
        else:
            role.append('His projected work close to the end zone is limited, so a longer scoring play may be needed.')
    elif _number(f.get('carries')) and _number(f.get('receptions')):
        role.append(f"Over his recent games, {name} has averaged {f['carries']:.1f} carries and {f['receptions']:.1f} catches.")
    if f.get('role_charting_known'):
        if rec>=rush and _number(f.get('role_red_zone_target_share')) and f['role_red_zone_target_share']>0:
            role.append(f"Over the last two team games, he received {f['role_red_zone_target_share']:.0%} of the team's passes aimed at receivers inside the opponent's 20-yard line.")
        elif rush>rec and _number(f.get('role_goal_line_share')) and f['role_goal_line_share']>0:
            role.append(f"Over the last two team games, he handled {f['role_goal_line_share']:.0%} of the team's carries inside the five-yard line.")
    if _number(points):
        role.append(f"The game odds put his team at about {points:.0f} points, giving us a starting point for how much scoring to expect.")
    lead=paths[0] if paths else None
    matchup='We do not have enough detailed matchup information to call this an advantage.'
    if lead:
        sample=lead['opponent_position_sample']; ratio=lead['opponent_rate_ratio']
        group={'QB':'quarterbacks','RB':'running backs','FB':'running backs','WR':'wide receivers','TE':'tight ends'}.get(position,'this position')
        area='near the goal line' if lead['path'].startswith('rush_') and lead['path'] in ('rush_5','rush_10','rush_20') else 'on throws into the end zone' if lead['path']=='rec_endzone' else 'in this type of scoring opportunity'
        if sample < 5:
            matchup=f"There are too few recent {opponent} plays against {group} {area} to call the matchup a clear advantage."
        elif ratio > 1.1:
            matchup=f"{opponent} has allowed {group} to turn these opportunities into touchdowns more often than the league benchmark. That helps the case, although the recent sample is small."
        elif ratio < .9:
            matchup=f"{opponent} has been tougher than the league benchmark against {group} {area}. The matchup is a concern."
        else:
            matchup=f"{opponent}'s recent results against {group} {area} are close to the league benchmark. We do not see a clear advantage there."
    elif _number(f.get('opp_position_td')):
        matchup=f"Recent opponents have scored {f['opp_position_td']:.1f} touchdowns per game from this position against {opponent}. That is useful background, but it does not establish an easy matchup."
    value_pick=bool(selected.get('price_supported'))
    leader=decision.get('most_likely') or {}
    lower_chance=bool(leader and leader.get('player_id')!=row.get('player_id'))
    is_alternative=row.get('explanation_role')=='value_alternative'
    label='Better-priced alternative' if is_alternative else 'Most likely scorer' if decision else 'Scorer pick'
    chance=f"We estimate a {p:.0%} chance of a touchdown." if _number(p) else 'A scoring estimate is unavailable.'
    caution=(f"Even at that estimate, he is more likely to finish without a touchdown." if _number(p) and p<.5 else 'A strong workload does not guarantee a touchdown.')
    if lower_chance and is_alternative:
        caution=f"This choice favors the payout over the highest scoring chance. {caution}"
    if lead and lead['recent_share'] < lead['longer_share']-.1:
        caution+=' His share of the main scoring opportunity has fallen over the last two team games.'
    status=((analysis.get('current_context') or {}).get('availability') or {}).get('status')
    if status not in ('active','available','healthy'):
        caution+=' Playing status still needs checking before kickoff.'
    else:
        from spe.db.base import as_utc
        from spe.models.player_td.scorer_readiness import readiness
        cutoff=as_utc(row['forecast'].get('data_cutoff'))
        if cutoff and readiness(row,cutoff)['status']=='provisional':
            caution+=' The saved availability or role check needs an update before kickoff.'
    price=row.get('book_price'); break_even=selected.get('break_even')
    if not _number(break_even) and _number(price) and abs(price)>=100:
        break_even=100/(price+100) if price>0 else abs(price)/(abs(price)+100)
    if _number(price) and _number(break_even) and _number(p):
        profit=10*price/100 if price>0 else 1000/abs(price)
        price_text=(f"At the saved {_odds(price)} odds, a $10 winning bet would earn ${profit:.2f} profit, plus the $10 stake back. "
            f"That price needs a {break_even:.0%} scoring chance to break even; our estimate is {p:.0%}. ")
        price_text+=('The saved odds look favorable under our estimates. The estimate could still be wrong.' if value_pick else
            'We have not established that this price is worth betting.')
    else:
        price_text='We do not have a usable saved price to judge whether this player is worth betting.'
    alternatives=decision.get('alternatives') or []
    peers=[r for r in alternatives if r['player_id']!=row.get('player_id') and r.get('position')==position and r.get('team')==row.get('team')]
    alternative=max(peers,key=lambda r:r['probability']) if peers else leader if lower_chance else None
    comparison=None
    if alternative:
        text=f"{alternative['name']} is estimated at {alternative['probability']:.0%}, compared with {p:.0%} for {name}. "
        if alternative.get('break_even') is not None and _number(alternative.get('american')):
            text+=f"His saved {_odds(alternative['american'])} price needs {alternative['break_even']:.0%} to break even. "
        text+=('Our price comparison favors '+name+'. It does not mean he is more likely to score.' if value_pick and alternative['probability']>p else
            'The choice reflects both the estimated scoring chance and the available price.' if value_pick else 'The scorer choice follows the estimated chance of a touchdown.')
        comparison={'heading':f"Why {name} over {alternative['name']}?",'text':text}
    return {'version':'customer_td_v1','label':label,'chance':chance,
        'why':' '.join(role),'matchup':matchup,'caution':caution,'price':price_text,
        'comparison':comparison, 'research_note':'These estimates are still being tested; better results have not yet been demonstrated.'}
