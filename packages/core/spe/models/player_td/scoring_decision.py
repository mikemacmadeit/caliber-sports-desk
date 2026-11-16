"""A scorer forecast and a price decision are different public claims.

The main pick maximizes scoring chance. Price candidates are a separate lane;
an attractive payout never displaces a more likely scorer. There is no position
quota or hard team-total cutoff. An unsupported main price is marked clearly.
"""
from spe.models.player_td.scorer_eligibility import exclusions
from spe.models.player_td.scorer_value import quote_value, reference_quote

VERSION = 'scoring_case_v5'


def evaluate(row):
    p = row['forecast']['p_estimate']
    quote = quote_value(row)
    path = ((row.get('desk_analysis') or {}).get('combination') or {}).get('scoring_paths') or {}
    active = path.get('status') == 'active'
    low = min(p, path.get('probability_low', p), path.get('baseline_probability', p))
    high = max(p, path.get('probability_high', p), path.get('baseline_probability', p))
    blockers = exclusions(row)
    context = (row.get('desk_analysis') or {}).get('current_context') or {}
    simulation = (row.get('desk_analysis') or {}).get('simulation') or {}
    availability = (context.get('availability') or {}).get('status')
    caution = []
    if simulation.get('status') == 'unavailable':
        caution.append('Game-flow simulation unavailable; using the original model estimate')
    weekly = simulation.get('weekly_context') or {}
    caution.extend(weekly.get('role_flags', []))
    if weekly.get('weather_status') == 'sensitivity_active':
        caution.append('Outdoor weather stress test is included in the range; its effect is not yet fitted')
    if availability not in ('active', 'available', 'healthy'):
        caution.append('Game-day participation is unconfirmed')
    if availability in ('questionable', 'uncertain'):
        caution.append('Resolve the injury designation before treating the price as actionable')
    if not active:
        caution.append('Detailed scoring-path stress tests are unavailable')
    if not quote:
        caution.append('No quote with a source timestamp within two hours of this prediction')
    supported = bool(quote and active and not blockers and low > quote['break_even'])
    from spe.models.player_td.football_case import assess
    football = assess(row)
    from spe.models.player_td.matchup_research import case_score
    research = case_score(row, football)
    reference = reference_quote(row)
    import math
    conservative = min(.999,max(.001,low))
    minimum = math.ceil((100*(1-conservative)/conservative if conservative<=.5 else -100*conservative/(1-conservative))/5)*5
    return {'player_id': row['player_id'], 'name': row['player_name'], 'team': row['team'],
        'position': row.get('position'), 'probability': p, 'stress_low': low, 'stress_high': high,
        'american': row.get('book_price'), 'bookmaker': row.get('bookmaker'),
        'source_at': row.get('price_source_at') or row.get('source_timestamp'),
        'break_even': quote['break_even'] if quote else None,
        'model_edge': quote['model_edge'] if quote else None,
        'expected_return': quote['expected_return'] if quote else None,
        'stress_edge': low-quote['break_even'] if quote else None,
        'price_supported': supported, 'bet_qualified':supported, 'football_case':football,
        'research_rank':research, 'research':path.get('research'),
        'reference_quote':reference, 'minimum_american':minimum,
        'eligibility_inputs':{'features':{k:(row.get('desk_analysis') or {}).get('features',{}).get(k)
            for k in ('recent_measured_opportunity_games','recent_measured_touches','role_complete_boxes','role_recent_appearance_rate')
            if k in (row.get('desk_analysis') or {}).get('features',{})},
            'current_context':{k:context.get(k) for k in ('availability','depth') if context.get(k)}},
        **({'simulation':simulation} if simulation else {}),
        'cautions': caution+football['cautions'], 'blockers': blockers,
        'price_status': 'conditional_value' if supported else 'no_supported_value' if quote else 'no_fresh_price',
        'uncertainty_note': 'Range spans declared role scenarios and component disagreement; it is not a confidence interval.'}


def choose(rows):
    eligible = [r for r in rows if not exclusions(r)]
    if not eligible:
        return None
    leader = max(eligible, key=lambda r:(r['forecast']['p_estimate'], r['player_id']))
    cases = {r['player_id']: evaluate(r) for r in eligible}
    leader_quote = reference_quote(leader)
    supported = [r for r in eligible if r['player_id']!=leader['player_id']
        and cases[r['player_id']]['research_rank']['supported']
        and (not reference_quote(r) or not leader_quote or reference_quote(r)['decimal']>leader_quote['decimal'])]
    selected = leader
    price_pick = max(supported, key=lambda r:(cases[r['player_id']]['research_rank']['score'], r['forecast']['p_estimate'], r['player_id'])) if supported else None
    alternative_basis = 'research_and_price'
    if price_pick is None:
        remaining = [r for r in eligible if r['player_id'] != leader['player_id']]
        research = [r for r in remaining if cases[r['player_id']]['research_rank']['supported']]
        if research:
            price_pick = max(research, key=lambda r:(cases[r['player_id']]['research_rank']['score'], r['forecast']['p_estimate'], r['player_id']))
            alternative_basis = 'football_research'
        else:
            scoring_role = [r for r in remaining if cases[r['player_id']]['football_case']['supported']]
            if scoring_role:
                price_pick = max(scoring_role, key=lambda r:(
                    cases[r['player_id']]['research_rank']['scoring_area_opportunities'],
                    r['forecast']['p_estimate'], r['player_id']))
                alternative_basis = 'football_research'
            elif remaining:
                price_pick = max(remaining, key=lambda r:(r['forecast']['p_estimate'], r['player_id']))
                alternative_basis = 'next_scoring_chance'
    case = cases[selected['player_id']]
    value = dict(cases[price_pick['player_id']]) if price_pick else None
    if value:
        value['research_selected']=alternative_basis != 'next_scoring_chance'
        value['alternative_basis']=alternative_basis
        if alternative_basis == 'next_scoring_chance':
            value['cautions'].append('Next-highest eligible scoring estimate; a distinct matchup advantage has not been established')
        from spe.models.player_td.customer_explanation import build
        value['customer'] = build({**price_pick,'explanation_role':'value_alternative',
            'decision':{'version':VERSION,'selected':value,'most_likely':cases[leader['player_id']],
                'alternatives':list(cases.values())}})
    reason = 'Our most likely scorer after considering workload, scoring opportunities, team expectations and the opponent. Price is assessed separately and does not replace this pick with a less likely scorer.'
    if ((selected.get('desk_analysis') or {}).get('simulation') or {}).get('status') == 'active':
        reason = 'Our most likely scorer after matchup and workload analysis followed by game-flow simulations. Price is assessed separately and does not replace this pick with a less likely scorer.'
    decision = {'version':VERSION, 'status':case['price_status'], 'reason':reason,
        'most_likely':cases[leader['player_id']], 'value_candidate':value, 'selected':case,
        'eligible_candidates':len(eligible), 'price_supported_candidates':sum(c['price_supported'] for c in cases.values()),
        'alternative_status':('qualified' if value['price_supported'] else 'research_only') if value else 'no_supported_alternative',
        'alternative_note':(('The alternate has the next-highest eligible scoring estimate. A distinct matchup advantage has not been established; its price is assessed separately.' if alternative_basis == 'next_scoring_chance' else
            'The alternate is selected by its scoring role, quarterback usage and opponent fit. Its price is checked separately.') if value else
            'Only one eligible scorer is available; a second name requires a player who passes the playing-status checks.'),
        'excluded_candidates':[{'player_id':r.get('player_id'),'name':r.get('player_name'),'reasons':exclusions(r)} for r in rows if exclusions(r)],
        'alternatives':sorted(cases.values(),key=lambda c:(-c['probability'],c['player_id']))[:8],
        'policy':'Highest model scoring chance first. A distinct alternate is selected whenever two eligible players exist: prefer a supported better-priced research case, then a player with a measured scoring-area role, then the next-highest scoring estimate. Ordinary touches do not make an alternate when another player has goal-line or red-zone work. Only a fresh qualifying quote is counted as a value bet.',
        'validation_note':'Research estimates; price support is not demonstrated profitability. Original pregame forecasts are preserved separately.'}
    return {**selected, 'replay_selection':'scoring_case', 'selection_version':VERSION,
        'selection_reason':reason, 'selection_warnings':case['cautions'], 'decision':decision,
        'best_candidate_probability':leader['forecast']['p_estimate']}
