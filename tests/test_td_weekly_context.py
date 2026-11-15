from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace

import pytest

from spe.models.player_td.availability_status import resolved_reports
from spe.models.player_td.weekly_context import scenarios, position_group, VERSION
from spe.models.player_td.gameflow_calibration import load_calibration, VERSION as CAL_VERSION
from spe.models.player_td import gameflow_stage as stage
from spe.simulate.desk_gameflow import simulate
from test_td_gameflow_stage import setup, NOW, CONFIG


def contexts():
    inputs,rows,model,game=setup()
    inputs['information_cutoff']=NOW.isoformat()
    for tid,team in inputs['teams'].items():
        for p in team['players']:
            r=next(r for r in rows if r['player_id']==p['id'])
            r.update(team=tid,position=p['position'])
            r['desk_analysis'].update(features={},current_context={})
    return inputs,rows,model,game


@pytest.mark.parametrize('position,group',[('RG:6','offensive_line'),('SLB:7','defensive_front'),
    ('LDE','defensive_front'),('RDT','defensive_front'),('LCB:8','secondary'),('NB:12','secondary')])
def test_real_depth_chart_slot_names_identify_personnel_groups(position,group):
    assert position_group(position)==group


def test_practice_survives_a_later_active_designation_but_does_not_clear_out():
    def report(status,hours,practice=None):
        return SimpleNamespace(player_id='p',designation=status,reported_at=NOW+timedelta(hours=hours),
            observed_at=NOW,source='official',practice_status=practice)
    rows=[report('Out',-3),report('Limited Participation in Practice',-2,'Limited Participation in Practice')]
    assert resolved_reports(rows)['p']['status']=='out'
    rows.append(report('active',-1))
    result=resolved_reports(rows)['p']
    assert result['status']=='active' and result['practice_status']=='Limited Participation in Practice'
    assert result==resolved_reports(rows[::-1])['p']


def test_limited_workload_preserves_td_shares_and_unassigned_work():
    inputs,rows,_,_=contexts();original=deepcopy(inputs)
    pid=rows[0]['player_id']
    rows[0]['desk_analysis']['current_context']['availability']={
        'status':'active','practice_status':'Limited Participation in Practice','practice_reported_at':NOW.isoformat()}
    runs,info=scenarios(inputs,rows)
    limited=next(data for name,data,_ in runs if name=='limited_workload')
    assert inputs==original
    for tid,team in limited['teams'].items():
        for channel,shares in team['scorer_shares'].items():
            assert sum(shares)==pytest.approx(sum(inputs['teams'][tid]['scorer_shares'][channel]))
            assert min(shares)>=0
    assert info['players'][pid]['verified_snap_limit'] is None
    assert simulate(limited,draws=1000)['mechanics']['team_td_mass_conserved']


@pytest.mark.parametrize('roof,expected',[('open',True),('closed',False),('retractable',False)])
def test_weather_requires_outdoor_exposure_and_never_increases_rushing(roof,expected):
    inputs,rows,_,_=contexts()
    rows[0]['desk_analysis']['current_context']['weather']={'roof':roof,'wind_mps':12,'precip_probability':.8}
    runs,info=scenarios(inputs,rows)
    weather=[data for name,data,_ in runs if name=='adverse_weather']
    assert bool(weather)==expected
    if weather:
        for tid,team in weather[0]['teams'].items():
            assert team['expected_tds']['rush']==inputs['teams'][tid]['expected_tds']['rush']
            assert team['expected_tds']['rec']==pytest.approx(inputs['teams'][tid]['expected_tds']['rec']*.85)


def test_stale_practice_and_unknown_participation_do_not_manufacture_snap_limits():
    inputs,rows,_,_=contexts()
    rows[0]['desk_analysis']['current_context']['availability']={'status':'active',
        'practice_status':'Did Not Participate','practice_reported_at':(NOW-timedelta(days=8)).isoformat()}
    runs,info=scenarios(inputs,rows)
    assert 'limited_workload' not in [name for name,_,_ in runs]
    assert info['players'][rows[0]['player_id']]['practice_status'] is None
    assert info['weather_status']=='missing'


def test_personnel_stress_is_group_bounded_and_does_not_mutate_baseline():
    inputs,rows,_,_=contexts();before=deepcopy(inputs)
    rows[0]['desk_analysis']['current_context']['personnel']={'absences':[
        {'team':'A','group':'offensive_line','depth_rank':1},
        {'team':'A','group':'offensive_line','depth_rank':1},
        {'team':'B','group':'secondary','depth_rank':2}]}
    runs,_=scenarios(inputs,rows)
    changed=next(data for name,data,_ in runs if name=='personnel_absences')
    assert changed['teams']['A']['expected_tds']['rush']==pytest.approx(inputs['teams']['A']['expected_tds']['rush']*.9)
    assert changed['teams']['B']==inputs['teams']['B']
    assert inputs==before


def test_weekly_scenarios_change_ranges_without_pretending_to_refit_probability(monkeypatch):
    inputs,rows,model,game=contexts()
    rows[0]['desk_analysis']['current_context']['weather']={'roof':'open','wind_mps':15}
    monkeypatch.setattr(stage,'build_inputs',lambda *a:inputs)
    baseline,_=stage.apply(rows,model,game,NOW,CONFIG)
    updated,run=stage.apply(rows,model,game,NOW,{**CONFIG,'weekly_context':VERSION})
    assert run['total_simulated_games']==70000
    for old,new in zip(baseline,updated):
        assert old['forecast']['p_estimate']==new['forecast']['p_estimate']
        assert 'adverse_weather' in new['desk_analysis']['simulation']['scenario_probabilities']
        assert new['desk_analysis']['simulation']['calibration']['simulation_specific'] is False


@pytest.mark.parametrize('fault',['gate','hash','future','model'])
def test_unvalidated_or_mismatched_calibration_never_publishes(db,fault):
    from spe.config import settings
    path=settings.parquet_dir/'models/test-calibration.json';path.parent.mkdir(parents=True,exist_ok=True)
    artifact={'version':CAL_VERSION,'base_model_sha256':'model','promotion_gate_passed':True,
        'trained_at':(NOW-timedelta(days=1)).isoformat(),'evaluation_end':'2026-11-01',
        'coefficients':[0,.5,.5],'validation':{}}
    if fault=='gate':artifact['promotion_gate_passed']=False
    if fault=='future':artifact['trained_at']=(NOW+timedelta(hours=1)).isoformat()
    if fault=='model':artifact['base_model_sha256']='different'
    path.write_text(json.dumps(artifact))
    config={'file':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    if fault=='hash':config['sha256']='wrong'
    fitted,status=load_calibration(config,'model',NOW)
    assert fitted is None and status['simulation_specific'] is False
    assert status['requested_calibration_unavailable']


def test_saved_card_withholds_a_new_absence_without_altering_saved_pick_or_started_game(db):
    from test_publication_pipeline import seed, NOW as moment
    from spe.db.models import BoardSnapshot, InjuryEvent, Player
    from spe.models.player_td.desk_replay import saved_day_card, day_card_key
    game=seed(db)
    game.kickoff_utc=moment+timedelta(hours=1)
    db.add(Player(player_id='candidate',name='Candidate',position='RB',position_group='RB'));db.flush()
    row={'game_id':game.game_id,'player_id':'candidate','player_name':'Candidate','kickoff_utc':game.kickoff_utc.isoformat()}
    key=day_card_key('2026-11-15','nfl')
    db.add(BoardSnapshot(key=key,payload={'rows':[row]},updated_at=moment))
    db.add(InjuryEvent(game_id=game.game_id,player_id='candidate',designation='out',reported_at=moment,
        observed_at=moment,effective_at=moment,source='official',confidence=1,provider='test',provider_record_id='out'))
    db.flush()
    card=saved_day_card(db,'2026-11-15','nfl',now=moment)
    assert not card['rows'] and 'withheld' in card['unavailable_games'][0]['reason']
    assert db.get(BoardSnapshot,key).payload['rows'][0]['player_id']=='candidate'
    game.status='in_progress';db.flush()
    assert saved_day_card(db,'2026-11-15','nfl',now=moment+timedelta(hours=2))['rows'][0]['player_id']=='candidate'


def test_background_slate_update_refreshes_league_tabs_without_recomputing_picks(db):
    from spe.db.models import BoardSnapshot
    from spe.models.player_td.desk_replay import _store_day_card, day_card_key
    nfl={'game_id':'nfl:one','league_id':'nfl','player_id':'new','result':'pending','retrospective':False,
        'decision_results':{'most_likely':{'result':'pending'}}}
    college={'game_id':'fbs:one','league_id':'fbs','player_id':'college','result':'scored','retrospective':False,
        'decision_results':{'most_likely':{'result':'scored'}}}
    _store_day_card(db,{'date':'2026-11-15','league':'nfl','rows':[{**nfl,'player_id':'old'}]})
    db.flush()
    _store_day_card(db,{'date':'2026-11-15','league':'all','rows':[nfl,college],'unavailable_games':[]})
    db.flush()
    saved=db.get(BoardSnapshot,day_card_key('2026-11-15','nfl')).payload
    assert saved['rows']==[nfl] and saved['games']==1
    assert saved['decision_record']['most_likely']['pending']==1
    saved=db.get(BoardSnapshot,day_card_key('2026-11-15','fbs')).payload
    assert saved['rows']==[college] and saved['wins']==1
