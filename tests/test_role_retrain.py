import numpy as np
import pytest
from spe.models.player_td.role_features import recent_role, interactions
from spe.models.player_td.calibration import stable_platt
from spe.models.player_td.desk_opportunity import OpportunityHistory, prepare_history
from test_desk_opportunity import history_row
from test_desk_combined import CUTOFF, allocate, allocator, combined_model, records


def test_role_tracks_team_games_not_only_player_appearances():
    rows=[history_row(player='p',week=1,carries=20)]
    rows += [history_row(player='teammate',week=w,carries=10) for w in (1,2,3)]
    h=OpportunityHistory(prepare_history(rows))
    f=recent_role(h,'p','home',CUTOFF)
    assert f['role_carries']==0
    assert f['role_recent_appearance_rate']==0
    rows.append(history_row(player='p',week=4,carries=1000))
    assert recent_role(OpportunityHistory(prepare_history(rows)),'p','home',CUTOFF)==f


def test_favored_role_interaction_changes_sign_without_changing_role():
    f={'role_carry_share':.5,'role_receiving_share':.2,'market_team_spread':-14}
    assert interactions(f)['favorite_carry_share']==.5
    assert interactions({**f,'market_team_spread':14})['favorite_carry_share']==-.5


def test_missing_college_box_targets_do_not_erase_measured_pbp_targets():
    from spe.models.player_td.desk_combined import opportunity_vector
    f={'target_pbp_known':1,'free_targets':10,'free_inside20_targets':2,
       'targets':0,'role_targets':0,'carries':0,'receptions':5,'role_receptions':6}
    assert opportunity_vector(f,'rec','fbs',True,True).tolist()==[8,2,0]


def test_role_allocation_conserves_team_tds_and_redistributes_excluded_player():
    m=combined_model();m['role_features']=True
    full=allocate(allocator(records()),m)
    reduced=allocate(allocator(records()),m,unavailable={'unpriced'})
    assert reduced['players']['p']['rush_lambda']>full['players']['p']['rush_lambda']
    assert sum(v['rush_lambda'] for v in reduced['players'].values())+reduced['unassigned_expected_tds']['rush']==pytest.approx(reduced['team_expected_tds']['rush'])


def test_calibration_handles_overconfident_predictions_without_divergence():
    p=np.array([1e-6]*80+[1-1e-6]*20)
    y=np.array(([0,1]*40)+([0,1]*10))
    a,b=stable_platt(p,y)
    z=a+b*np.log(p/(1-p));q=1/(1+np.exp(-z))
    assert np.isfinite(q).all()
    assert np.max(np.abs(q-.5))<.01


def test_requested_retrain_fails_closed_on_wrong_artifact(db, monkeypatch):
    from test_publication_pipeline import seed
    import spe.models.player_td.desk_replay as replay
    from datetime import timedelta
    from spe.db.base import as_utc
    now=as_utc('2026-11-14T23:00:00Z')
    game=seed(db);game.kickoff_utc=now-timedelta(hours=1);db.flush()
    monkeypatch.setattr(replay,'requested_retrain',lambda *a:{'version':'desk_combined_v6','artifact_sha256':'expected'})
    monkeypatch.setattr(replay,'load_combined_model',lambda *a,**k:combined_model())
    result=replay.replay_day(db,'2026-11-14',now=now)
    assert result['rows']==[]
    assert len(result['unavailable_games'])==1
