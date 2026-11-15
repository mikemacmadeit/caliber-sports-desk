from copy import deepcopy
from datetime import timedelta
import json

import numpy as np
import pandas as pd
import pytest

from spe.models.player_td import scorer_candidate as scorer
from spe.models.player_td.candidate_universe import team_candidates
from spe.models.player_td.desk_combined import AllocationHistory, combined_prediction, load_combined_model
from spe.models.player_td.desk_market import market_features
from spe.models.player_td.desk_opportunity import OpportunityHistory, prepare_history
from spe.research.td_scoring_labels import verified_labels
from test_desk_combined import CUTOFF, combined_model, allocator, allocate
from test_desk_opportunity import history_row


def tree_model():
    return {'features':scorer.FEATURES,'medians':[0.]*len(scorer.FEATURES),
        'intercept':0.,'learning_rate':.1,'tree_weight':.75,'calibration_intercept':0.,
        'calibration_slope':1.,'uses_market_totals':True,
        'trees':[{'left':[1,-1,-1],'right':[2,-1,-1],
            'feature':[scorer.FEATURES.index('team_implied_points'),-2,-2],
            'threshold':[25.,-2.,-2.],'value':[0.,-1.,1.]}]}


def test_training_pool_does_not_depend_on_target_participation_or_results():
    rows=[history_row(player=p,week=w) for p in ('starter','absent') for w in (1,2,3)]
    def pool(records):
        h=OpportunityHistory(prepare_history(records));a=AllocationHistory(h)
        return list(team_candidates(h,a,'home','away',CUTOFF,True))
    before=pool(rows)
    target=history_row(player='starter',week=4,carries=100,rush_td=10)
    assert pool(rows+[target])==before
    assert {r['player_id'] for r in before}=={'starter','absent'}
    assert pool(rows+[dict(target,player_id='future-only')])==before


def label_fixture():
    game=dict(game_id='espn:1',team_id='fbs:10',home_team_id='fbs:10',away_team_id='fbs:20')
    records=[{**game,'player_id':'fbs:100','rush_td':0,'rec_td':1},
        {**game,'player_id':'fbs:101','rush_td':0,'rec_td':0}]
    play=dict(game_id=1,id=1,pos_team_id=10,homeTeamId=10,awayTeamId=20,
        rush_td=False,pass_td=True,rusher_player_id=None,receiver_player_id=100,
        **{'type.text':'Passing Touchdown'},status_type_completed=True,
        penalty_no_play=False,penalty_negated_play=False)
    other={**play,'id':2,'pos_team_id':20,'pass_td':False,'receiver_player_id':None,'type.text':'Rush'}
    return records,[play,other]


def test_passing_td_labels_receiver_only_and_unknown_identity_blocks_zero_labels():
    records,plays=label_fixture()
    labels=verified_labels(pd.DataFrame(plays),records,{}, {'100':'fbs:100','101':'fbs:101'})
    assert labels['espn:1']['scorers']==['fbs:100']
    plays[0]['receiver_player_id']=None
    assert verified_labels(pd.DataFrame(plays),records,{}, {'100':'fbs:100'})=={}


def test_disagreement_partial_game_or_nullified_td_cannot_establish_a_missing_player_loss():
    records,plays=label_fixture()
    records[0]['rec_td']=2
    assert verified_labels(pd.DataFrame(plays),records,{}, {'100':'fbs:100'})=={}
    records[0]['rec_td']=1
    assert verified_labels(pd.DataFrame(plays[:1]),records,{}, {'100':'fbs:100'})=={}
    plays[0]['penalty_negated_play']=True
    assert verified_labels(pd.DataFrame(plays),records,{}, {'100':'fbs:100'})=={}


def test_context_total_can_feed_new_scorer_without_changing_original_model_inputs():
    f=market_features({'home_handicap':-14.,'total':56.},True,False)
    assert f['market_game_total']==0 and f['market_total_known']==0
    assert scorer.vector(f)[scorer.FEATURES.index('team_implied_points')]==35
    unknown=scorer.vector(market_features({'home_handicap':-14},True,False))
    assert np.isnan(unknown[scorer.FEATURES.index('team_implied_points')])


def test_names_prices_passing_touchdowns_and_result_are_not_probability_features():
    model=tree_model();f=market_features({'home_handicap':-10,'total':50},True,False)
    before=scorer.predict(model,f,0.)
    assert scorer.predict(model,{**f,'pass_td':100,'result':'scored','book_price':-10000,
        'player_name':'winner','actual':1},0.)==before


@pytest.mark.parametrize('change',['cycle','nan','feature','shape'])
def test_malformed_tree_artifacts_fail_closed(change):
    m=tree_model();assert scorer.validate(m)
    if change=='cycle':m['trees'][0]['left'][0]=0
    elif change=='nan':m['medians'][0]=float('nan')
    elif change=='feature':m['trees'][0]['feature'][0]=10000
    elif change=='shape':m['trees'][0]['right'].pop()
    assert not scorer.validate(m)


def test_new_combined_path_uses_exported_scorer_and_preserves_original_comparison():
    engine=allocator();model=combined_model()
    f=engine.history.features('p','home','away','RB',CUTOFF,True)
    f.update(market_features({'home_handicap':-10,'total':50},True,False))
    original=combined_prediction(model,f,allocate(engine,model),'p')
    model['scorer_model']=tree_model()
    result=combined_prediction(model,f,allocate(engine,model),'p')
    assert result['baseline_combined_probability']==original['probability']
    assert result['probability']!=original['probability']
    assert result['probability']==result['combined_probability']


def test_live_loader_requires_both_validation_gate_and_valid_scorer(db):
    from spe.config import settings
    m=combined_model('fbs');m.update(version='desk_combined_v8',scorer_model=tree_model(),validation={'promotion_gate_passed':False})
    path=settings.parquet_dir/'models/fbs_desk_combined_v8.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps(m))
    assert load_combined_model('fbs',version='desk_combined_v8') is None
    m['validation']['promotion_gate_passed']=True;path.write_text(json.dumps(m))
    assert load_combined_model('fbs',version='desk_combined_v8')
    m['scorer_model']['trees'][0]['left'][0]=0;path.write_text(json.dumps(m))
    assert load_combined_model('fbs',version='desk_combined_v8') is None


def test_exported_tree_predictions_match_training_library():
    sklearn=pytest.importorskip('sklearn.ensemble')
    from scripts.train_scorer_candidate import export_trees
    # Nontrivial learned splits plus missing inputs; export is served without sklearn.
    rng=np.random.default_rng(7);raw=rng.normal(size=(150,len(scorer.FEATURES)))
    raw[:,scorer.FEATURES.index('rush_pbp_known')]=0
    raw[:,scorer.FEATURES.index('target_pbp_known')]=0
    raw[:,scorer.FEATURES.index('market_spread_known')]=0
    rows=[]
    for row in raw:
        f=dict(zip(scorer.FEATURES,row));f['context_game_total']=None
        rows.append(f)
    x=np.array([scorer.vector(f) for f in rows]);medians=np.zeros(x.shape[1])
    z=scorer.matrix(x,medians);y=(raw[:,0]+raw[:,1]>0).astype(int)
    estimator=sklearn.GradientBoostingClassifier(n_estimators=7,max_depth=2,random_state=1).fit(z,y)
    m=export_trees(estimator,medians);m.update(tree_weight=1.,calibration_intercept=0.,calibration_slope=1.)
    assert scorer.validate(m)
    np.testing.assert_allclose([scorer.predict(m,f,0.) for f in rows],estimator.predict_proba(z)[:,1],rtol=1e-7,atol=1e-7)


def test_shadow_comparison_preserves_official_chance_and_excludes_documented_inactive(monkeypatch):
    from spe.models.player_td.scorer_shadow import compare
    active=combined_model('fbs')
    candidate={**deepcopy(active),'version':'desk_combined_v8','scorer_model':tree_model(),
        'baseline_artifact_sha256':'fixture','artifact_sha256':'challenger','live_enabled':False}
    monkeypatch.setattr('spe.models.player_td.scorer_shadow.load_combined_model',lambda *a,**k:candidate)
    f=market_features({'home_handicap':-10,'total':50},True,False)
    rows=[{'player_id':p,'position':'RB','forecast':{'p_estimate':prob},
        'desk_analysis':{'features':f,'current_context':{'availability':{'status':status}}}}
        for p,prob,status in [('a',.6,'available'),('b',.99,'out')]]
    before=[r['forecast'].copy() for r in rows]
    comparison=compare(rows,rows[0],active,CUTOFF)
    assert comparison['selected_player_id']=='a'
    assert comparison['promotion_gate_passed'] is False
    assert [r['forecast'] for r in rows]==before
    assert all('challenger_forecast' in r for r in rows)
    active['artifact_sha256']='different'
    assert compare(rows,rows[0],active,CUTOFF) is None


def test_frozen_candidate_record_keeps_challenger_and_cannot_be_replaced_after_kickoff(db):
    from spe.models.player_td.pregame_candidates import save_candidates
    from spe.db.models import BoardSnapshot
    from test_publication_pipeline import seed, NOW
    game=seed(db);game.kickoff_utc=NOW+timedelta(minutes=5)
    rows=[{'player_id':'p','forecast':{'p_estimate':.6},'challenger_forecast':{'p_estimate':.5}}]
    challenger={'selected_player_id':'p','status':'shadow_comparison'}
    assert save_candidates(db,game,rows,rows[0],NOW,'sha',challenger)
    db.flush();snap=db.get(BoardSnapshot,'td-pregame-candidates-v1:'+game.game_id)
    before=deepcopy(snap.payload)
    assert before['challenger']==challenger and before['candidates'][0]['challenger_forecast']['p_estimate']==.5
    assert not save_candidates(db,game,rows,None,NOW+timedelta(minutes=6),'new',None)
    assert snap.payload==before


def test_scorer_first_policy_continues_on_future_days(db):
    from spe.models.player_td.desk_replay import revised_policy_requested
    assert revised_policy_requested('2026-11-15')
    assert revised_policy_requested('2026-11-21')
    assert not revised_policy_requested('2026-09-01')


def test_evaluation_tie_break_matches_serving_scorer_selection():
    from scripts.train_scorer_candidate import metrics
    from spe.models.player_td.desk_replay import select_game_scorer
    rows=[{'game_id':'g','player_id':p,'label':label,'forecast':{'p_estimate':.5}}
          for p,label in [('a',0),('z',1)]]
    assert select_game_scorer(rows)['player_id']=='z'
    assert metrics(rows,np.array([.5,.5]))['one_per_game']['hits']==1
