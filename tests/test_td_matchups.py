from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from spe.db.base import as_utc
from spe.models.player_td.availability_status import resolved_reports
from spe.models.player_td.desk_opportunity import OpportunityHistory, prepare_history, vector
from spe.models.player_td.matchup_features import FEATURES
from spe.models.player_td.scorer_eligibility import exclusions
from spe.models.player_td.scorer_value import choose, quote_value
from spe.models.player_td.desk_replay import select_game_scorer
from spe.ingest.nfl_availability import parse
from test_desk_opportunity import history_row
from test_td_scouting import candidate

NOW=as_utc('2026-09-27T18:00:00Z')


def report(status, days=0):
    return SimpleNamespace(player_id='p',designation=status,source='test',
        observed_at=NOW,reported_at=NOW-timedelta(days=days))


def test_ir_is_not_cleared_by_newer_practice_report_but_explicit_return_can_clear_it():
    rows=[report('Injured Reserve',2),report('Did Not Participate In Practice')]
    assert resolved_reports(rows)['p']['status']=='injured reserve'
    assert resolved_reports(rows[::-1])==resolved_reports(rows)
    rows.append(report('Active'))
    assert resolved_reports(rows)['p']['status']=='active'


def test_newer_archive_questionable_timestamp_does_not_clear_explicit_out():
    assert resolved_reports([report('Out',1),report('Questionable')])['p']['status']=='out'


def test_active_roster_status_does_not_rescue_displaced_backup_role():
    row=candidate()
    row['desk_analysis']['current_context']={'availability':{'status':'active'},'depth':{'rank':4}}
    row['desk_analysis']['features'].update(recent_measured_opportunity_games=2,recent_measured_touches=0)
    assert exclusions(row)
    row['desk_analysis']['features']['recent_measured_touches']=1
    assert not exclusions(row)


def test_latest_team_games_clear_stale_snap_and_touch_context_without_inventing_snaps():
    from spe.models.player_td.nflverse_context import free_features
    games=[]
    for i in (3,2,1):
        team={'metrics':{'field_position_coverage':1.,'targets':20},'effective_at':(NOW-timedelta(days=i)).isoformat()}
        games.append({'game_id':str(i),'kickoff_utc':NOW-timedelta(days=i+3),'nflverse_context':{'team':team}})
    own=[{**games[0],'nflverse_context':{**games[0]['nflverse_context'],
        'snap':{'metrics':{'offense_snap_share':.9},'effective_at':(NOW-timedelta(days=3)).isoformat()},
        'player':{'metrics':{'carries':20,'targets':2,'inside10_carries':3},'effective_at':(NOW-timedelta(days=3)).isoformat()}}}]
    f=free_features(own,games,games,'RB',NOW)
    assert f['recent_measured_opportunity_games']==2 and f['recent_measured_touches']==0
    assert f['last_snap_share'] is None
    assert not f['free_data_current']


@pytest.mark.parametrize('status',['Injured Reserve','Reserve/Injured','IR','PUP','Reserve/PUP','Out','Suspended','Doubtful'])
def test_all_nonplaying_designations_are_excluded(status):
    row=candidate()
    row['desk_analysis']['current_context']['availability']={'status':status}
    assert exclusions(row)
    assert select_game_scorer([row],value_aware=True) is None


def test_free_injury_parser_uses_explicit_status_and_team_not_comment_inferences():
    data={'injuries':[{'injuries':[
        {'athlete':{'displayName':'Puka Nacua','team':{'abbreviation':'LAR'}},'status':'Injured Reserve','date':'2026-09-25T12:00Z'},
        {'athlete':{'displayName':'Other','team':{'abbreviation':'LAR'}},'status':'Active','date':'2026-09-25T12:00Z','longComment':'Puka Nacua is injured'},
        {'athlete':{'displayName':'Unmapped'},'status':'Out'}]}]}
    rows=parse(data)
    assert len(rows)==2
    assert rows[0]['team']=='nfl:LA' and rows[0]['status']=='injured reserve'
    assert rows[1]['status']=='active'


def fixture_history():
    rows=[]
    for week in (1,2,3):
        rows += [history_row('wr',week,position='WR',targets=8,receptions=5,rec_yards=80,carries=0,gl_carries=0,rz_targets=2,rush_yards=0),
                 history_row('rb',week,targets=2,receptions=1,rec_yards=5,carries=20,gl_carries=2,rz_targets=0,rush_yards=80)]
    return rows


def test_matchup_uses_prior_position_work_and_latest_team_games_without_same_game_leakage():
    raw=fixture_history()
    def compute(rows):
        h=OpportunityHistory(prepare_history(rows))
        return h.features('wr','home','away','WR',NOW,True)
    before=compute(raw)
    assert before['position_allowed_targets']==pytest.approx(8)
    assert before['position_allowed_yards_per_target']==pytest.approx(10)
    assert before['role_target_share']==pytest.approx(.8)
    assert before['projected_targets']==pytest.approx(8)
    assert before['role_red_zone_target_share']==1
    raw += [history_row('wr',4,kickoff_utc=NOW.isoformat(),targets=999,rec_td=10,rec_yards=10000)]
    assert compute(raw)==before
    changed=deepcopy(before)
    changed.update(defense_pass_epa=.2,offense_pass_epa=.1,market_team_spread=-7)
    v=vector(changed,FEATURES)
    assert v[FEATURES.index('target_role_defense_interaction')]==pytest.approx(1.6)


def test_missing_college_targets_do_not_become_measured_zero_or_routes():
    rows=[{**r,'provider':'cfbd'} for r in fixture_history()]
    h=OpportunityHistory(prepare_history(rows))
    f=h.features('wr','home','away','WR',NOW,True)
    assert f['position_allowed_targets'] is None
    assert f['projected_targets'] is None
    assert f['role_goal_line_share'] is None
    assert not f['position_targets_known']
    assert not any('route' in k for k in f)


def priced(pid,p,price,age=0):
    r=candidate(pid,p,book_price=price)
    r['forecast']['data_cutoff']=NOW.isoformat()
    r['source_timestamp']=(NOW-timedelta(minutes=age)).isoformat()
    return r


def test_price_choice_is_bounded_and_stale_future_or_missing_quotes_do_not_change_rank():
    top=priced('top',.65,-250)
    close=priced('close',.62,110)
    longshot=priced('long',.3,900)
    chosen,policy=choose([top,close,longshot])
    assert chosen['player_id']=='close' and policy['chance_given_up']==pytest.approx(.03)
    for age in (121,-1):
        invalid=priced('close',.62,110,age)
        assert quote_value(invalid) is None
        assert choose([top,invalid,longshot])[0]['player_id']=='top'
    missing=priced('close',.62,None)
    assert choose([top,missing])[0]['player_id']=='top'


def test_value_selection_preserves_probability_and_reports_actual_policy():
    rows=[priced('top',.65,-250),priced('close',.62,110)]
    before=deepcopy(rows)
    selected=select_game_scorer(rows,value_aware=True)
    assert rows==before
    assert selected['player_id']=='close'
    assert selected['selection_version']=='matchup_value_scorer_v3'
    assert selected['forecast']['p_estimate']==.62
