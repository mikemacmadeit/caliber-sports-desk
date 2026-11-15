"""Build a connected, outcome-blind football argument from saved measurements."""
from spe.analytics.td_scoring_paths import LABELS


def build(row):
    analysis = row.get('desk_analysis') or {}
    combined = analysis.get('combination') or {}
    path = combined.get('scoring_paths') or {}
    decision = row.get('decision') or {}
    selected = decision.get('selected') or {}
    p = row['forecast']['p_estimate']
    context = analysis.get('current_context') or {}
    if path.get('status') != 'active':
        return {'thesis':'The detailed scoring-path projection is unavailable for this player.',
            'support':[], 'counter':['The displayed chance comes from the existing model; missing charting has not been filled in.'],
            'paths':[], 'verdict':decision.get('reason','Scorer forecast only; a worthwhile price has not been established.')}
    paths = path.get('paths') or []
    lead = paths[0] if paths else None
    rush, rec = path['rush_lambda'], path['rec_lambda']
    kind = 'rushing' if rush >= rec else 'receiving'
    support, counter = [], []
    carries = sum(r['projected_opportunities'] for r in paths if r['path'].startswith('rush'))
    targets = sum(r['projected_opportunities'] for r in paths if r['path'].startswith('rec'))
    support.append(f"Projecting {carries:.1f} carries and {targets:.1f} targets from the last two team games, six-game history and the opponent's allowed opportunities. Missed team games remain in the workload window.")
    if lead:
        support.append(f"The strongest scoring path is {lead['label']}: {lead['projected_opportunities']:.1f} projected opportunities and {lead['share']:.0%} of the team's work in that area.")
        ratio = lead['opponent_rate_ratio']
        direction = 'above' if ratio >= 1 else 'below'
        fact = (f"Against this position in that scoring area, the opponent's TD conversion rate is {abs(ratio-1):.0%} {direction} the fit-period league rate after small-sample shrinkage ({lead['opponent_position_sample']:.0f} prior opportunities).")
        (support if ratio >= 1 else counter).append(fact)
        delta = lead['recent_share']-lead['longer_share']
        if abs(delta) >= .05:
            (support if delta > 0 else counter).append(f"The player's share of {lead['label']} is {lead['recent_share']:.0%} over the latest two team games versus {lead['longer_share']:.0%} over the longer window.")
        if lead['player_sample'] < 10:
            counter.append(f"Only {lead['player_sample']:.0f} recent opportunities support the main scoring-area conversion estimate; it is shrunk toward the league rate.")
    budget = combined.get('team_expected_tds') or {}
    if all(k in budget for k in ('rush','rec')):
        support.append(f"The team model projects {budget['rush']:.2f} rushing and {budget['rec']:.2f} receiving touchdowns. This player's path allocation earns {rush:.2f} and {rec:.2f}, respectively; teammates and unknown replacements retain the rest.")
    if any(r['inherited_share'] > .001 for r in paths):
        counter.append('Some projected work comes from absent teammates of the same position. Half is reserved for uncertain replacements; no-inheritance and full-inheritance scenarios are also evaluated.')
    low, high = selected.get('stress_low',path['probability_low']), selected.get('stress_high',path['probability_high'])
    counter.append(f"Declared workload scenarios and component disagreement span {low:.0%}–{high:.0%}; the central estimate is {p:.0%}. This is a sensitivity range, not a confidence interval.")
    if context.get('personnel_alerts'):
        counter.append('Reported quarterback, blocker or defender absences are listed below. Individual replacement quality and coverage changes are not assigned invented numerical bonuses.')
    if ((context.get('availability') or {}).get('status')) not in ('active','available','healthy'):
        counter.append('Playing status still needs confirmation before kickoff.')
    if selected.get('break_even') is not None:
        verdict = (f"The recorded price needs {selected['break_even']:.0%}; the central estimate is {p:.0%} and the stress-case estimate is {low:.0%}. " +
            ('That supports a conditional price candidate, subject to participation and model risk.' if selected.get('price_supported') else 'The price is not supported by the available stress tests; keep this as a scorer forecast.'))
    else:
        verdict = 'No fresh player quote is available. This is a scorer forecast, with no value claim.'
    return {'thesis':f"{row['player_name']}'s case is primarily {kind}: a projected role in specific scoring areas, checked against this opponent and the price.",
        'support':support,'counter':counter,'paths':paths,'verdict':verdict}
