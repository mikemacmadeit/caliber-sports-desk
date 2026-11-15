"""Outcome-blind scoring cases, sourced from the exact prediction inputs.

This explains fitted model inputs without adding probability multipliers. Capture extra
comparisons before kickoff; old picks can only explain their frozen inputs.
"""
import math
from datetime import timedelta

from spe.db.base import as_utc
from spe.market.odds import american_to_implied
from spe.models.player_td.scorer_eligibility import exclusions

VERSION = 'td_scouting_v2'


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def capture_matchup(history, team, opponent, cutoff):
    """Same-league, same-cutoff comparisons; never current-game statistics."""
    from spe.models.player_td.desk_evidence import weighted
    evidence = getattr(history, 'evidence', None)
    result = {'cutoff': cutoff.isoformat(), 'benchmarks': []}
    if evidence is None:
        return result
    for channel in ('rush', 'pass'):
        key = channel + '_epa'
        samples = {}
        counts = {}
        for defense, rows in evidence.defenses.items():
            recent = evidence.recent(rows, cutoff)
            measured = [r for r in recent if number(r.get(key))]
            if len(measured) < 2 or cutoff-as_utc(measured[-1]['kickoff_utc']) > timedelta(days=21):
                continue
            samples[defense] = weighted(measured, key)
            counts[defense] = len(measured)
        if opponent not in samples or len(samples) < 8:
            continue
        value = samples[opponent]
        percentile = sum((v < value) + .5*(v == value) for v in samples.values())/len(samples)
        result['benchmarks'].append({'channel': channel, 'epa_allowed': value,
            'allowed_percentile': percentile, 'defenses': len(samples), 'games': counts[opponent]})
    return result


def capture_personnel(history, team, cutoff, reports, players):
    """Quantify recorded work of confirmed absences, without inventing a heir."""
    from spe.models.player_td.desk_combined import INACTIVE
    games = history.recent(history.teams.get(team, []), cutoff)[-2:]
    result = []
    if len(games) != 2:
        return result
    for (pid, tid), rows in history.players.items():
        if tid != team or str(reports.get(pid) or '').lower() not in INACTIVE:
            continue
        recent = {r['game_id']: r for r in history.recent(rows, cutoff)}
        if not all(g['game_id'] in recent or g.get('complete_player_box') for g in games):
            continue
        player = players.get(pid)
        result.append({'player_id': pid, 'name': player.name if player else pid,
            'status': reports[pid], 'games': 2,
            **{k: sum(float(recent.get(g['game_id'], {}).get(k) or 0) for g in games)/2
               for k in ('carries', 'receptions')}})
    return result


def model_fields(model):
    """Fields with nonzero fitted coefficients or actual tree splits, not a feature wish list."""
    from spe.models.player_td.desk_combined import TEAM_FEATURES
    from spe.models.player_td.desk_evidence import TEAM_RICH_FEATURES
    from spe.models.player_td.situational import SITUATIONAL_FEATURES
    player = model.get('player_model') or {}
    names = {k for k, w in zip(player.get('features', []), player.get('coefficients', [])) if w != 0}
    team_names = ['intercept'] + TEAM_FEATURES
    if model.get('market_features'):
        team_names += ['market_team_spread', 'market_game_total', 'market_spread_known', 'market_total_known']
    if model.get('rich_features'):
        team_names += TEAM_RICH_FEATURES
    if model.get('situational_features'):
        team_names += SITUATIONAL_FEATURES
    for coefficients in (model.get('team_coefficients') or {}).values():
        names.update(k for k, w in zip(team_names, coefficients) if k != 'intercept' and w != 0)
    scorer = model.get('scorer_model') or {}
    fields = scorer.get('features') or []
    for tree in scorer.get('trees', []):
        names.update(fields[i % len(fields)] for i, left in zip(tree['feature'], tree['left']) if left != -1)
    # This names fitted fields, not local feature attributions. Opportunity
    # allocation also depends on workload and fitted channel conversion rates.
    return sorted(names)


def price_case(row):
    p = (row.get('forecast') or {}).get('p_estimate')
    price = row.get('book_price')
    valid_p = number(p) and 0 < p < 1
    valid_price = number(price) and abs(price) >= 100
    implied = american_to_implied(price) if valid_price else None
    fair = round(-100*p/(1-p) if p >= .5 else 100*(1-p)/p) if valid_p else None
    return {'american': price if valid_price else None, 'bookmaker': row.get('bookmaker'),
        'recorded_at': row.get('source_timestamp'), 'break_even': implied,
        'fair_american': fair, 'estimated_edge': p-implied if valid_p and implied is not None else None,
        'note': 'Break-even includes bookmaker margin. A positive model edge is not a validated betting advantage.'}


def selection_context(report, row):
    """Explain the saved selection policy without changing its inputs or pick.

    Can also update an older report on the response only, using its already
    saved alternative summaries. No fresh candidate search or outcome read.
    """
    from spe.models.player_td.customer_explanation import build as customer_explanation
    report = {**report, 'customer': customer_explanation(row)}
    if row.get('decision'):
        from spe.models.player_td.scoring_case import build
        decision = row['decision']
        return {**report, 'version': 'td_scouting_v3', 'case': build(row),
            'comparison': decision['reason'] + ' Both teams are considered; team scoring expectations enter the model rather than a 21-point exclusion rule.',
            'alternatives': [{**a, 'policy_excluded': False} for a in report.get('alternatives', [])]}
    from spe.models.player_td.desk_market import MIN_TEAM_IMPLIED_POINTS
    v2 = row.get('selection_version') in ('highest_chance_scorer_v2','matchup_value_scorer_v3')
    own_points = row.get('team_implied_points')
    restricted = v2 and number(own_points) and own_points >= MIN_TEAM_IMPLIED_POINTS
    alternatives = [{**r, 'policy_excluded': bool(restricted and
        (not number(r.get('team_implied_points')) or r['team_implied_points'] < MIN_TEAM_IMPLIED_POINTS))}
        for r in report.get('alternatives') or []]
    if not alternatives:
        comparison = 'An alternative comparison was not captured with these inputs; old picks are not re-ranked using later information.'
    elif v2:
        comparison = (f"Selection policy prefers players from teams with at least {MIN_TEAM_IMPLIED_POINTS:g} implied points when such candidates exist. "
            "This rule can bypass a higher individual scoring probability. It is a selection preference, not a fitted probability adjustment or proven accuracy gain.")
    else:
        comparison = 'Ranked by estimated scoring chance after availability and role checks. A larger team total creates more opportunities, but the player must earn a large enough share.'
    policy=row.get('value_selection')
    if policy:
        comparison += (f" Price comparison permits at most {policy['maximum_chance_gap']:.0%} points less scoring chance than the leading eligible player, and requires a fresh quote plus at least {policy['minimum_model_edge']:.0%} points estimated edge. "
            + (f"This pick gives up {policy['chance_given_up']:.1%} points for its recorded payout." if policy['reason']=='positive_model_edge_within_five_points_of_top' else 'No price-qualified alternative met that rule; the highest chance remains the pick.'))
    return {**report, 'version': VERSION, 'alternatives': alternatives, 'comparison': comparison}


def build_report(row, candidates=None, *, frozen_inputs_only=False):
    """Pure function. Deliberately ignores result, actual, final score and game status."""
    a = row.get('desk_analysis') or {}
    f = a.get('features') or {}
    c = a.get('current_context') or {}
    combination = a.get('combination') or {}
    role, matchup, script, checks, gaps = [], [], [], [], []
    games = int(f.get('games') or 0)
    if number(f.get('carries')) and number(f.get('receptions')):
        role.append(f"Across {games} recent appearances: {f['carries']:.1f} carries and {f['receptions']:.1f} catches per game (recent games weighted more).")
    if number(f.get('role_carries')) and number(f.get('role_receptions')):
        role.append(f"Last two recorded team games: {f['role_carries']:.1f} carries and {f['role_receptions']:.1f} catches per game; {f.get('role_carry_share', 0):.0%} of team carries and {f.get('role_receiving_share', 0):.0%} of catches. This measures recorded work, not active status.")
    if f.get('snap_known') and number(f.get('snap_share')):
        role.append(f"Offensive snap share: {f['snap_share']:.0%} across {int(f.get('snap_games') or 0)} measured appearances" +
            (f"; last-two trend {f['snap_trend']:+.0%} points." if number(f.get('snap_trend')) else '.'))
    else:
        gaps.append('Recent offensive snap counts are not available for this pick.')
    for flag, key, label, game_key in (
        ('current_rush_known', 'current_inside5_carries', 'carries inside the five per team game (latest two)', None),
        ('current_target_known', 'current_inside20_targets', 'red-zone targets per team game (latest two)', None),
        ('rush_pbp_known', 'free_inside5_carries', 'carries inside the five per measured appearance', 'rush_pbp_games'),
        ('target_pbp_known', 'free_targets', 'targets per measured appearance', 'target_pbp_games'),
        ('target_pbp_known', 'free_inside20_targets', 'red-zone targets per measured appearance', 'target_pbp_games'),
        ('end_zone_known', 'end_zone_targets_estimated', 'estimated end-zone targets per measured appearance (air yards plus field position)', None),
    ):
        if f.get(flag) and number(f.get(key)):
            role.append(f"{f[key]:.1f} {label}" + (f" across {int(f.get(game_key) or 0)} games." if game_key else '.'))
    if f.get('charting_known') and number(f.get('goal_line_share')):
        if number(f.get('gl_carries')) and number(f.get('rz_targets')):
            role.append(f"{f['gl_carries']:.1f} measured carries inside the five and {f['rz_targets']:.1f} red-zone targets per recent appearance.")
        role.append(f"{f['goal_line_share']:.0%} of the team's measured carries inside the five across the longer appearance window.")
    if row.get('league_id') == 'nfl' and f.get('pbp_known'):
        if number(f.get('targets')) and number(f.get('target_share')):
            role.append(f"{f['targets']:.1f} targets per recent appearance; {f['target_share']:.0%} of measured team targets.")
        if number(f.get('inside10_carries')) and number(f.get('inside10_targets')):
            role.append(f"Inside the ten: {f['inside10_carries']:.1f} carries and {f['inside10_targets']:.1f} targets per measured appearance.")
    if not (f.get('rush_pbp_known') or f.get('charting_known')):
        gaps.append('Player goal-line carries are unmeasured in the saved inputs.')
    if not (f.get('target_pbp_known') or f.get('pbp_known')):
        gaps.append('Targets and receiving scoring-area opportunities are incomplete; catches are not targets.')
    path_allocation = combination.get('scoring_paths') or {}
    current_allocation = path_allocation if path_allocation.get('status') == 'active' else combination
    rush, rec = current_allocation.get('rush_lambda'), current_allocation.get('rec_lambda')
    channel = 'Scoring route remains uncertain'
    if number(rush) and number(rec) and rush+rec > 0:
        channel = 'Rushing-led scoring case' if rush >= rec else 'Receiving-led scoring case'
        script.append(f"Team allocation assigns this player {rush:.2f} expected rushing TDs and {rec:.2f} expected receiving TDs. These are expected counts, not separate calibrated probabilities.")
    team_tds = combination.get('team_expected_tds') or {}
    if all(number(team_tds.get(k)) for k in ('rush', 'rec')):
        script.append(f"Team model: {team_tds['rush']:.2f} rushing and {team_tds['rec']:.2f} receiving TDs, shared across {combination.get('pool_size', '?')} recent teammates, including unpriced players.")
    market = row.get('market_context') or {}
    home = row.get('team') == row.get('home_abbr')
    own_total = row.get('team_implied_points')
    other_total = market.get('away_implied_points' if home else 'home_implied_points')
    if number(own_total):
        script.append(f"Bookmaker team expectation: {row.get('team', 'team')} {own_total:.1f} points" +
            (f", {row.get('opponent', 'opponent')} {other_total:.1f}." if number(other_total) else '.'))
        if not a.get('market_total_active'):
            script.append('That point total is context only for this saved probability model.')
    from spe.models.player_td.situational import explanations
    script.extend(explanations(f))
    for kind, label in (('rush', 'rushing'), ('pass', 'passing')):
        offense, defense = f.get('offense_'+kind+'_epa'), f.get('defense_'+kind+'_epa')
        if f.get('offense_evidence_known') and f.get('defense_evidence_known') and number(offense) and number(defense):
            matchup.append(f"Recent {label} efficiency: offense {offense:+.2f} EPA/play; opponent allows {defense:+.2f}. Higher allowed EPA means more production conceded, not a guaranteed scoring matchup.")
    if number(f.get('opp_position_td')):
        matchup.append(f"Opponent allowed {f['opp_position_td']:.2f} TDs per game to {row.get('position', 'this position')} across {int(f.get('opponent_games') or 0)} recent games. This is scoring history, not a coverage assignment or a schedule-adjusted position ranking.")
    if a.get('player_matchups_active'):
        if f.get('position_targets_known') and number(f.get('position_allowed_yards_per_target')):
            matchup.append(f"Opponent allowed {f['position_allowed_targets']:.1f} targets and {f['position_allowed_yards_per_target']:.1f} yards per target to {row.get('position')} in recent games. The fitted model connects this position matchup with the player's projected receiving workload.")
        if f.get('position_yards_known') and number(f.get('position_allowed_yards_per_carry')):
            matchup.append(f"Opponent allowed {f['position_allowed_carries']:.1f} carries per game and {f['position_allowed_yards_per_carry']:.1f} yards per carry to {row.get('position')}; this is measured production, not individual blocking or coverage grades.")
        if number(f.get('projected_carries')) and number(f.get('projected_targets')):
            script.append(f"Volume inputs project {f['projected_carries']:.1f} carries and {f['projected_targets']:.1f} targets from recent shares and both teams' play volumes. Their effects are fitted from earlier games.")
        if f.get('role_charting_known'):
            role.append(f"Latest two team games: {f['role_inside5_carries']:.1f} carries inside the five ({f['role_goal_line_share']:.0%} team share) and {f['role_red_zone_targets']:.1f} red-zone targets ({f['role_red_zone_target_share']:.0%} team share).")
    if f.get('pbp_known') and number(f.get('opponent_position_inside10_targets')) and number(f.get('opponent_position_inside5_carries')):
        matchup.append(f"Opponent conceded {f['opponent_position_inside10_targets']:.1f} targets inside the ten and {f['opponent_position_inside5_carries']:.1f} carries inside the five per recent game to {row.get('position', 'this position')}. Opportunity counts do not measure coverage ability by themselves.")
    for b in (a.get('scouting_inputs') or {}).get('benchmarks', []):
        matchup.append(f"Opponent's {b['channel']} EPA allowed is at the {b['allowed_percentile']:.0%} percentile among {b['defenses']} measured defenses in this league (higher is more allowed; {b['games']} opponent games). Small samples and schedule differences still matter.")
        primary = ('rush' if rush >= rec else 'pass') if number(rush) and number(rec) and rush+rec > 0 else None
        if b['channel'] == primary and b['allowed_percentile'] <= .25:
            checks.append('Matchup caution: the primary scoring route faces a defense in the quarter allowing the least EPA per play in this comparison. Strong workload alone does not establish a favorable matchup.')
    if not matchup:
        gaps.append('No reliable matchup comparison was captured for this pick.')
    status = (c.get('availability') or {}).get('status')
    checks.append(f"Availability report: {status}." if status else 'Game-day active status is unconfirmed; roster membership and prior appearances do not confirm participation.')
    depth = c.get('depth') or {}
    if depth.get('rank'):
        checks.append(f"Depth chart: #{depth['rank']} from {depth.get('source', 'recorded source')}" + (f", dated {depth['as_of']}." if depth.get('as_of') else '.'))
    else:
        gaps.append('No current verified depth-chart rank was captured.')
    if number(f.get('days_since_appearance')) and f['days_since_appearance'] > 14:
        checks.append(f"Last recorded appearance was {f['days_since_appearance']:.0f} days before the prediction; verify the current role.")
    if games < 4:
        checks.append('Fewer than four player appearances support this estimate.')
    if row.get('position') == 'QB':
        checks.append('Only a touchdown the quarterback scores counts here; passing touchdowns do not count.')
    for item in a.get('vacated_workload') or []:
        method = ('The new path allocation redistributes half the vacated work within the same position and reserves the rest; the older blended component still uses proportional allocation.'
            if path_allocation.get('status') == 'active' else 'The allocation redistributes excluded players\' shares proportionally; who inherits the actual work is unconfirmed.')
        checks.append(f"{item['name']} reported {item['status']}: {item['carries']:.1f} carries and {item['receptions']:.1f} catches per game over two prior team games. {method}")
    for item in c.get('personnel_alerts') or []:
        checks.append(f"{item['side']} {item['position']} {item['name']}: {item['status']}. No verified individual replacement or coverage adjustment.")
    weather = c.get('weather') or {}
    if weather:
        checks.append(f"Weather forecast: wind {weather.get('wind_mps', 'unknown')} m/s; precipitation probability {weather.get('precip_probability', 'unknown')}. Context only; no fitted probability adjustment.")
    supplement = c.get('nflverse_context') or a.get('nflverse_context') or {}
    ngs_rec = (supplement.get('receiving') or {}).get('metrics') or {}
    ngs_rush = (supplement.get('rushing') or {}).get('metrics') or {}
    if number(ngs_rec.get('avg_separation')):
        matchup.append(f"Latest measured NGS week: {ngs_rec['avg_separation']:.1f} yards average separation at catch/incompletion. Context, not an upcoming defender matchup.")
    if number(ngs_rush.get('rush_yards_over_expected_per_att')):
        matchup.append(f"Latest measured NGS week: {ngs_rush['rush_yards_over_expected_per_att']:+.1f} rush yards over expected per attempt. Context only.")
    alternatives = []
    eligible = [r for r in candidates or [] if r.get('game_id') == row.get('game_id') and r.get('player_id') != row.get('player_id') and not exclusions(r)]
    eligible.sort(key=lambda r: (-r['forecast']['p_estimate'], str(r.get('player_id'))))
    # Include the best opposite-team player even when teammates fill the top ranks.
    selected = eligible[:2]
    for extra in (next((r for r in eligible if r.get('team') != row.get('team')), None),
                  next((r for r in eligible if number(r.get('book_price')) and r['book_price'] >= 100), None)):
        if extra and all(r['player_id'] != extra['player_id'] for r in selected):
            selected.append(extra)
    p = (row.get('forecast') or {}).get('p_estimate')
    for r in selected:
        alt_analysis = r.get('desk_analysis') or {}
        alt_features = alt_analysis.get('features') or {}
        alt_role = 'Recent role unavailable'
        if number(alt_features.get('role_carries')) and number(alt_features.get('role_receptions')):
            alt_role = f"Last two team games: {alt_features['role_carries']:.1f} carries, {alt_features['role_receptions']:.1f} catches/game"
        alternatives.append({'player_id': r['player_id'], 'name': r.get('player_name'), 'team': r.get('team'),
            'probability': r['forecast']['p_estimate'], 'team_implied_points': r.get('team_implied_points'),
            'gap_to_pick': p-r['forecast']['p_estimate'] if number(p) else None, 'price': price_case(r),
            'role': alt_role, 'allocation_probability': (alt_analysis.get('combination') or {}).get('allocation_probability')})
    comparison = ('Ranked by estimated scoring chance after eligibility checks. A larger team total creates more team opportunities, but the player must earn a large enough share.'
        if alternatives else 'An eligible alternative comparison was not captured with these inputs; old picks are not re-ranked using later information.')
    opposite = next((r for r in alternatives if r['team'] != row.get('team')), None)
    if opposite and number(p):
        comparison += f" {row.get('player_name')} is estimated at {p:.0%}; the leading eligible opponent, {opposite['name']}, at {opposite['probability']:.0%}."
    if frozen_inputs_only:
        checks.append('This explanation uses the saved prediction inputs only. It does not reconstruct missing facts from the finished game.')
    checks.extend(a.get('risks') or [])
    gaps += ['Complete routes run, first-read share and current man/zone receiving splits are unavailable.',
             'Specific defensive assignments and a verified upcoming game plan are unavailable.',
             'Defender absences are not translated into verified coverage changes or player probability adjustments.',
             'New players with insufficient history can be absent from the model candidate pool.']
    model_note = 'The listed facts explain the inputs and allocation; they are not proof that each fact caused this probability.'
    model_note += ' New league comparisons, NGS notes and weather are context only. They do not add untested bonuses to the probability.'
    if a.get('matchup_features_active') is False:
        model_note += ' Expanded matchup fields are not active in this saved model.'
    if a.get('free_opportunities_active') is False:
        model_note += ' The new player scoring-area fields remain contextual in this model.'
    sources = []
    for label in a.get('sources') or []:
        if 'nflverse' in label.lower() or 'next gen' in label.lower():
            url = 'https://github.com/nflverse/nflverse-data'
        elif 'sportsdataverse' in label.lower():
            url = 'https://github.com/sportsdataverse/sportsdataverse-data'
        else:
            url = None
        sources.append({'label': label, 'url': url})
    for source in [c.get('availability'), depth]:
        if source and source.get('source'):
            value = source['source']
            sources.append({'label': value, 'url': value if value.startswith('https://') else
                ('https://github.com/nflverse/nflverse-data' if 'nflverse' in value.lower() else None)})
    if ngs_rec or ngs_rush:
        sources.append({'label':'NFL Next Gen Stats via nflverse', 'url':'https://github.com/nflverse/nflverse-data'})
    report = {'version': VERSION, 'headline': channel, 'information_cutoff': (row.get('forecast') or {}).get('data_cutoff'),
        'frozen_inputs_only': frozen_inputs_only, 'role': role, 'matchup': matchup, 'game_script': script,
        'checks': checks, 'gaps': list(dict.fromkeys(gaps)), 'alternatives': alternatives, 'comparison': comparison,
        'price': price_case(row), 'model_note': model_note, 'sources': sources}
    return selection_context(report, row)
