import copy
from datetime import timedelta
import pytest
from spe.db.base import as_utc
from spe.models.player_td.selection_safety import assess
from spe.models.player_td.desk_replay import select_replay_candidate

NOW=as_utc('2026-11-14T23:00:00Z')

def candidate():
    return {'player_id':'p','league_id':'fbs','forecast':{'p_estimate':.60},'book_price':110,
        'market_context':{'usable':True},'team_implied_points':28,
        'desk_analysis':{'features':{'games':4,'rush_pbp_known':1,'target_pbp_known':0},
            'combination':{'rush_lambda':.8,'rec_lambda':.1},
            'current_context':{'availability':{'status':'available','reported_at':NOW.isoformat(),'observed_at':NOW.isoformat()}}}}

def test_clear_role_and_fresh_available_report_can_pass():
    r=candidate();r['selection_safety']=assess(r,NOW,True)
    assert r['selection_safety']['eligible']
    assert select_replay_candidate([r])['player_id']=='p'


def test_legacy_fallback_cannot_bypass_a_failed_new_gate():
    from spe.shadow.atd_desk_takes import select_desk_takes
    r=candidate();r.update(game_id='g',selection_safety={'eligible':False})
    assert select_desk_takes([r])==[]

@pytest.mark.parametrize('status',['out','questionable','probable','uncertain',None])
def test_unknown_or_uncertain_health_never_treated_as_active(status):
    r=candidate();r['desk_analysis']['current_context']['availability']['status']=status
    assert not assess(r,NOW,True)['eligible']

@pytest.mark.parametrize('field,offset',[('reported_at',-25),('reported_at',1),('observed_at',1)])
def test_unverifiable_or_stale_reports_block(field,offset):
    r=candidate();r['desk_analysis']['current_context']['availability'][field]=(NOW+timedelta(hours=offset)).isoformat()
    assert not assess(r,NOW,True)['eligible']

def test_price_edge_role_and_model_gate_are_independent():
    r=candidate();r['book_price']=-200
    assert 'no_positive_price_edge' in assess(r,NOW,True)['blockers']
    r=candidate();r['forecast']['p_estimate']=.49
    assert 'chance_below_50_percent' in assess(r,NOW,True)['blockers']
    r=candidate();r['desk_analysis']['combination']={'rec_lambda':.9,'rush_lambda':.01}
    assert 'scoring_role_evidence_incomplete' in assess(r,NOW,True)['blockers']
    assert 'model_failed_validation' in assess(candidate(),NOW,False)['blockers']

def test_guarded_selection_does_not_trade_ten_points_for_cheaper_odds():
    leader=candidate();leader['forecast']['p_estimate']=.80;leader['book_price']=-250
    alternative=copy.deepcopy(leader);alternative['player_id']='cheap';alternative['forecast']['p_estimate']=.71;alternative['book_price']=110
    for r in (leader,alternative):r['selection_safety']=assess(r,NOW,True)
    assert select_replay_candidate([leader,alternative])['player_id']=='p'
    leader['selection_safety']['eligible']=False
    assert select_replay_candidate([leader,alternative])['player_id']=='cheap'

def test_started_guarded_decision_is_frozen(db,monkeypatch):
    from test_publication_pipeline import seed
    from test_desk_combined import combined_model
    from spe.db.models import BoardSnapshot
    import spe.models.player_td.desk_replay as replay
    game=seed(db);game.kickoff_utc=NOW-timedelta(hours=1)
    r=candidate();r.update(game_id=game.game_id,player_name='Saved runner',kickoff_utc=game.kickoff_utc.isoformat())
    db.add(BoardSnapshot(key='guarded-desk-v1:'+game.game_id,updated_at=NOW-timedelta(hours=2),payload={'row':r,'evaluated':True}))
    db.flush()
    monkeypatch.setattr(replay,'load_combined_model',lambda *a,**k:combined_model())
    monkeypatch.setattr(replay,'load_history',lambda *a:pytest.fail('Frozen decision must not rebuild'))
    result=replay.replay_day(db,'2026-11-14',now=NOW)
    assert result['rows'][0]['player_name']=='Saved runner'


def test_requested_revised_replay_keeps_legacy_snapshot_and_uses_new_policy(db,monkeypatch):
    from test_publication_pipeline import seed
    from test_desk_combined import combined_model
    from spe.db.models import BoardSnapshot
    import spe.models.player_td.desk_replay as replay
    game=seed(db);game.kickoff_utc=NOW-timedelta(hours=1)
    model=combined_model()
    key=replay.CACHE_PREFIX+game.game_id+':'+model['artifact_sha256']
    original={'row':{'player_name':'Old pick'},'evaluated':True}
    db.add(BoardSnapshot(key=key,updated_at=NOW,payload=original));db.flush()
    monkeypatch.setattr(replay,'revised_policy_requested',lambda date:True)
    monkeypatch.setattr(replay,'load_combined_model',lambda *a,**k:model)
    monkeypatch.setattr(replay,'load_replay_model',lambda *a:model)
    monkeypatch.setattr(replay,'load_history',lambda *a:[])
    monkeypatch.setattr(replay.AllocationHistory,'allocate',lambda *a,**k:None)
    result=replay.replay_day(db,'2026-11-14',now=NOW)
    db.flush()
    assert result['rows']==[]
    assert db.get(BoardSnapshot,key).payload==original
    revised=db.get(BoardSnapshot,'scorer-replay-v1:'+game.game_id+':'+model['artifact_sha256'])
    assert revised.payload['selection_version']=='highest_chance_scorer_v2'


def test_game_scorer_names_leader_despite_price_and_information_warnings():
    from spe.models.player_td.desk_replay import select_game_scorer
    r=candidate();r['forecast']['p_estimate']=.46;r['book_price']=None
    r['selection_safety']=assess(r,NOW,True)
    alternative=copy.deepcopy(r);alternative['player_id']='other';alternative['forecast']['p_estimate']=.42
    result=select_game_scorer([alternative,r])
    assert result['player_id']=='p'
    assert result['selection_warnings']
    low=copy.deepcopy(r);low['forecast']['p_estimate']=.35
    lower=copy.deepcopy(alternative);lower['forecast']['p_estimate']=.30
    assert select_game_scorer([low, lower]) is None
    r['desk_analysis']['current_context']['availability']['status']='out'
    assert select_game_scorer([r,alternative])['player_id']=='other'


@pytest.mark.parametrize('status', ['out','inactive','confirmed_inactive','suspended','doubtful'])
def test_game_scorer_blocks_all_known_nonplaying_forecast_statuses(status):
    from spe.models.player_td.desk_replay import select_game_scorer
    r=candidate();r['forecast']['availability_status']=status
    assert select_game_scorer([r]) is None


def test_documented_backup_qb_does_not_inherit_starting_rushing_role():
    from spe.models.player_td.desk_replay import select_game_scorer
    r=candidate();r['position']='QB'
    r['desk_analysis']['current_context']['depth']={'rank':2,'source':'official'}
    assert select_game_scorer([r]) is None


def test_passing_only_participant_is_an_atd_zero_not_an_ungraded_player():
    from spe.publishing.boxes import parse
    payload={'boxscore':{'players':[{'statistics':[{'name':'passing','labels':['C/ATT','TD'],
        'athletes':[{'athlete':{'id':'1','displayName':'Played QB'},'stats':['2/2','3']},
                    {'athlete':{'id':'2','displayName':'Unused QB'},'stats':['0/0','0']}]}]}]}}
    rows=parse(payload)
    assert len(rows)==1
    assert rows[0]['name']=='Played QB'
    assert rows[0]['markets']['anytime_td']==0
