"""Observed weekly personnel and explicitly uncalibrated context stress tests.

These are sensitivity cases, not estimated injury/weather coefficients. Markets
may already account for these facts. Never silently multiply the point estimate.
"""
from copy import deepcopy
from datetime import timedelta
import math

from spe.db.base import as_utc

VERSION = 'td_weekly_context_v1'
OL = {'C', 'G', 'T', 'OT', 'OG', 'LT', 'LG', 'RG', 'RT', 'OL'}
FRONT = {'DE', 'DT', 'NT', 'DL', 'LB', 'ILB', 'OLB', 'EDGE', 'MLB'}
SECONDARY = {'CB', 'DB', 'S', 'FS', 'SS', 'NB'}


def position_group(value):
    position = (value or '').upper().split(':')[0]
    if position in OL:return 'offensive_line'
    if position in FRONT or position.endswith(('DE','DT','LB')):return 'defensive_front'
    if position in SECONDARY or position.endswith('CB'):return 'secondary'
    return 'skill'


def capture_personnel(db, game, cutoff):
    """Resolve team and position using a fresh, observed depth chart, not current rosters."""
    from spe.db.models import DepthChartWeek, InjuryEvent, Player
    from spe.models.player_td.availability_status import resolved_reports, INACTIVE
    cutoff = as_utc(cutoff)
    result = {'as_of':cutoff.isoformat(), 'source':'stored game reports and observed weekly depth',
              'absences':[], 'unmatched_report_ids':[], 'probability_effect':'sensitivity_only'}
    if game.league_id != 'nfl':
        return {**result, 'status':'unsupported_league'}
    depth = db.query(DepthChartWeek).filter(
        DepthChartWeek.team_id.in_((game.home_team_id, game.away_team_id)),
        DepthChartWeek.observed_at <= cutoff, DepthChartWeek.effective_at <= cutoff,
        DepthChartWeek.effective_at >= cutoff-timedelta(days=7),
        DepthChartWeek.quality_status.notin_(('invalid','quarantined'))).all()
    identities = {}
    latest = {tid:max(as_utc(r.effective_at) for r in depth if r.team_id==tid) for tid in {r.team_id for r in depth}}
    for r in sorted(depth, key=lambda r:(as_utc(r.effective_at), -(r.depth_rank or 99))):
        if as_utc(r.effective_at) != latest[r.team_id] or (r.position or '').upper() in {'KR','PR','K','PK','P','LS','H'}:
            continue
        identities[r.player_id] = r
    reports = db.query(InjuryEvent).filter(InjuryEvent.game_id == game.game_id,
        InjuryEvent.reported_at <= cutoff, InjuryEvent.observed_at <= cutoff,
        InjuryEvent.quality_status.notin_(('invalid','quarantined'))).all()
    for pid, report in resolved_reports(reports).items():
        if report['status'] not in INACTIVE | {'doubtful'}:
            continue
        row = identities.get(pid)
        if row is None:
            result['unmatched_report_ids'].append(pid)
            continue
        position = (row.position or row.depth_position or '').upper().split(':')[0]
        group = position_group(position)
        player = db.get(Player, pid)
        result['absences'].append({'player_id':pid, 'name':player.name if player else pid,
            'team':row.team_id.removeprefix('nfl:'), 'position':position, 'group':group,
            'depth_rank':row.depth_rank, 'status':report['status'], 'report':report,
            'depth_observed_at':as_utc(row.observed_at).isoformat(),
            'depth_effective_at':as_utc(row.effective_at).isoformat()})
    result['status'] = 'available' if depth else 'missing_depth'
    return result


def _finite(value):
    return isinstance(value, (int,float)) and math.isfinite(value)


def _scale_budget(team, channel, factor):
    old = team['expected_tds'][channel]
    team['expected_tds'][channel] *= factor
    # Preserve the model's FG budget when stress-testing TD efficiency.
    team['implied_points'] += 7*(team['expected_tds'][channel]-old)


def _limited_work(inputs, limited):
    modified = deepcopy(inputs)
    for team in modified['teams'].values():
        for channel in ('rush','rec'):
            original = team['scorer_shares'][channel][:]
            weights = original[:]
            for i, player in enumerate(team['players']):
                if player['id'] not in limited:
                    continue
                removed = original[i]*.5
                weights[i] -= removed
                peers = [j for j,p in enumerate(team['players']) if p['position']==player['position'] and p['id'] not in limited]
                total = sum(original[j] for j in peers)
                weights[-1] += removed*(.5 if total else 1.)
                if total:
                    for j in peers:
                        weights[j] += .5*removed*original[j]/total
            team['scorer_shares'][channel] = weights
    return modified


def scenarios(inputs, rows):
    """Return measured triggers, missingness and bounded what-if simulations.

The 50% workload and 10–15% channel stresses are declared assumptions. They
are never mislabeled as learned effects or probabilities of the scenarios.
"""
    cutoff = as_utc(inputs['information_cutoff'])
    details, limited = {}, set()
    people = next(((r.get('desk_analysis') or {}).get('current_context', {}).get('personnel')
                   for r in rows if (r.get('desk_analysis') or {}).get('current_context', {}).get('personnel')), {})
    weather = next(((r.get('desk_analysis') or {}).get('current_context', {}).get('weather')
                   for r in rows if (r.get('desk_analysis') or {}).get('current_context', {}).get('weather')), None)
    for r in rows:
        analysis = r.get('desk_analysis') or {}
        current = analysis.get('current_context') or {}
        availability = current.get('availability') or {}
        practice = availability.get('practice_status')
        practice_at = as_utc(availability.get('practice_reported_at'))
        if not practice_at or not cutoff-timedelta(days=7) <= practice_at <= cutoff:
            practice = None
        f = analysis.get('features') or {}
        flags = []
        if availability.get('status') in ('questionable','uncertain'):
            flags.append('Questionable game status; full workload is unconfirmed')
        if practice and any(s in practice.lower() for s in ('limited','did not','dnp')):
            flags.append('Recent limited or missed practice; no verified snap limit')
        if (_finite(f.get('role_recent_appearance_rate')) and f['role_recent_appearance_rate'] < 1
                and f.get('recent_measured_opportunity_games',0) >= 2):
            flags.append('Missed a recent observed team game; returning workload may differ')
        if flags:
            limited.add(r['player_id'])
        details[r['player_id']] = {'role_flags':flags, 'practice_status':practice,
            'depth':current.get('depth'), 'snap_share':f.get('snap_share'),
            'last_snap_share':f.get('last_snap_share'), 'snap_trend':f.get('snap_trend'),
            'verified_snap_limit':None, 'upcoming_play_calls':None,
            'coverage_assignments':None, 'charted_routes':None}
    runs = [('recent_roles_all_positions', inputs, {'recent_role':'all'})]
    assumptions = {'recent_roles_all_positions':'Last-game opportunity share shrunk by eight opportunities; all skill positions, not verified future workload.'}
    if limited:
        runs.append(('limited_workload', _limited_work(inputs,limited), {}))
        assumptions['limited_workload'] = 'Flagged players receive 50% of baseline scoring share; half the freed share goes to measured same-position peers, half to unassigned players. Not a predicted snap limit.'
    # Known roof closure removes weather exposure. Missing/unknown roof remains unknown.
    weather_status = 'missing'
    if weather and weather.get('roof') in ('closed','dome','indoors'):
        weather_status = 'indoors'
    elif weather and weather.get('roof') == 'open':
        weather_status = 'forecast_available'
        wind, rain = weather.get('wind_mps'), weather.get('precip_probability')
        if (_finite(wind) and wind >= 6.7) or (_finite(rain) and rain >= .6):
            altered = deepcopy(inputs)
            for team in altered['teams'].values():_scale_budget(team,'rec',.85)
            runs.append(('adverse_weather',altered,{}))
            assumptions['adverse_weather'] = 'Outdoor forecast triggers a 15% receiving-TD-budget reduction stress test; no fitted weather coefficient and no automatic rushing boost.'
            weather_status = 'sensitivity_active'
    absent = [p for p in people.get('absences',[]) if p.get('depth_rank') == 1 and p['group'] != 'skill']
    if absent:
        altered = deepcopy(inputs)
        for tid, team in altered['teams'].items():
            own = {p['group'] for p in absent if p['team']==tid}
            opp = {p['group'] for p in absent if p['team']!=tid and p['team'] in altered['teams']}
            if 'offensive_line' in own:
                for channel in ('rush','rec'):_scale_budget(team,channel,.9)
            if 'defensive_front' in opp:_scale_budget(team,'rush',1.1)
            if 'secondary' in opp:_scale_budget(team,'rec',1.1)
        runs.append(('personnel_absences',altered,{}))
        assumptions['personnel_absences'] = 'Documented absent starters: OL stresses own TD channels -10%; defensive front stresses opponent rushing +10%; secondary stresses receiving +10%. Once per group. Market totals may already reflect these absences; central forecast is unchanged.'
    assessment = {'version':VERSION, 'players':details, 'personnel':people,
        'weather_status':weather_status, 'scenario_assumptions':assumptions,
        'point_estimate_effect':'none_without_validated_calibration',
        'missing':['complete current routes','current man/zone and individual assignments',
                   'verified upcoming play calls','verified player snap limits'],
        'note':'Context stresses widen scenario ranges and price checks; they are not confidence intervals or fitted causal effects.'}
    return runs, assessment
