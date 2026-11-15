from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

from spe.db.base import as_utc
from spe.ingest.college_market_history import normalize
from spe.models.player_td.current_opportunities import current_features, transform
from spe.models.player_td.desk_combined import opportunity_vector
from spe.models.player_td.desk_evidence import EvidenceHistory
from spe.models.player_td.desk_opportunity import OpportunityHistory,prepare_history
from test_desk_opportunity import history_row
from test_desk_combined import CUTOFF


def test_market_import_rejects_defaults_and_preserves_home_sign_and_observation():
    row={'game_id':123,'odds_source':'core_odds_api','game_spread_available':True,
         'home_team_spread':7.5,'over_under':50.5}
    assert normalize([{**row,'odds_source':'default'},{**row,'game_id':124,'over_under':float('nan')}],'now','url')=={}
    result=normalize([row],'now','url')['123']
    assert result['home_handicap']==7.5 and result['total']==50.5
    assert result['observed_at']=='now' and result['pregame_timestamp_verified'] is False


def test_sacks_cannot_become_rushing_opportunities_even_with_mislabeled_rush_flag():
    from test_free_desk_inputs import play,summarized
    payload=summarized([play(sack=False),play(id=2,sack=True),play(id=3,**{'type.text':'Sack'})])
    assert payload['players'][0]['carries']==1
    assert payload['players'][0]['inside5_carries']==1


def test_channel_transform_accepts_rushing_with_missing_receiving_and_never_mutates_baseline():
    f={'carries':12,'targets':None,'rush_pbp_known':1,'target_pbp_known':0,
       'free_carries':8,'free_inside5_carries':2,'free_inside20_carries':3}
    before=deepcopy(f);new=transform(f,'channels')
    assert new['carries']==8 and new['gl_carries']==2 and new['targets'] is None
    assert transform(f,None)==before and f==before


def evidence_history():
    rows=[history_row(player='p',week=w,carries=18) for w in (1,2,3)]
    h=OpportunityHistory(prepare_history(rows))
    teams=[];players=[]
    for g in h.teams['home']:
        base={**g,'team_id':'home','observed_at':(CUTOFF-timedelta(days=1)).isoformat(),
              'rush_coverage':1.,'target_coverage':.2,'field_coverage':1.,'rushes':30,'targets':20}
        teams.append(base)
        # Earlier player appearances should not conceal his latest zero work.
        players.append({**base,'player_id':'p','carries':10,'inside5_carries':2})
    players.pop()
    h.evidence=EvidenceHistory({'opportunity_games':teams,'players':players},replay=True)
    return h


def test_current_role_uses_two_team_games_and_channel_coverage():
    h=evidence_history();f=current_features(h,'p','home','QB',CUTOFF)
    assert f['current_rush_known']==1 and f['current_carries']==5
    assert f['current_inside5_carries']==1 and f['current_qb_carries']==5
    assert f['current_target_known']==0 and f['current_targets'] is None


def test_current_role_rejects_future_games_and_late_observations_in_strict_mode():
    h=evidence_history();before=current_features(h,'p','home','QB',CUTOFF)
    h.teams['home'].append({'game_id':'future','kickoff_utc':CUTOFF+timedelta(days=1)})
    assert current_features(h,'p','home','QB',CUTOFF)==before
    h.evidence.replay=False
    for r in h.evidence.game_teams.values():r['observed_at']=(CUTOFF+timedelta(seconds=1)).isoformat()
    assert current_features(h,'p','home','QB',CUTOFF)['current_rush_known']==0


def test_current_allocation_can_be_zero_without_reviving_old_starter_workload():
    f={'carries':20,'receptions':0,'current_rush_known':1,'current_carries':0,'current_inside5_carries':0}
    assert opportunity_vector(f,'rush','fbs',input_profile='current').tolist()==[0,0,0]
    assert opportunity_vector(f,'rush','fbs').tolist()==[20,0,0]


def test_scorer_readiness_requires_timely_availability_and_current_qb_role():
    from spe.models.player_td.scorer_readiness import readiness
    from test_selection_safety import candidate, NOW
    r=candidate();r['position']='RB'
    assert readiness(r,NOW)['status']=='availability_checked'
    r['position']='QB'
    assert readiness(r,NOW)['status']=='provisional'
    r['desk_analysis']['current_context']['depth']={'rank':1}
    assert readiness(r,NOW)['status']=='availability_checked'
    r['desk_analysis']['current_context']['availability']['observed_at']=(NOW+timedelta(seconds=1)).isoformat()
    assert readiness(r,NOW)['status']=='provisional'


def test_candidate_universe_is_frozen_after_kickoff_including_nonselected_players(db):
    from spe.models.player_td.pregame_candidates import save_candidates
    from spe.db.models import BoardSnapshot
    from test_publication_pipeline import seed, NOW
    game=seed(db);game.kickoff_utc=NOW+timedelta(minutes=1)
    rows=[{'player_id':'p','forecast':{'p_estimate':.6}}, {'player_id':'q','forecast':{'p_estimate':.4}}]
    assert save_candidates(db,game,rows,rows[0],NOW,'sha')
    db.flush()
    snap=db.get(BoardSnapshot,'td-pregame-candidates-v1:'+game.game_id)
    assert [r['player_id'] for r in snap.payload['candidates']]==['p','q']
    before=deepcopy(snap.payload)
    assert not save_candidates(db,game,[],None,NOW+timedelta(minutes=2),'new-sha')
    assert snap.payload==before


def test_failed_candidate_never_loads_as_active_even_if_copied_to_active_filename(db):
    import json
    from spe.config import settings
    from spe.models.player_td.desk_combined import load_combined_model
    from test_desk_combined import combined_model
    model=combined_model('fbs');model.update(version='desk_combined_v7',live_enabled=False)
    folder=settings.parquet_dir/'models';folder.mkdir(parents=True,exist_ok=True)
    (folder/'fbs_desk_combined_v7.json').write_text(json.dumps(model))
    (folder/'fbs_desk_combined_v7_candidate.json').write_text(json.dumps(model))
    assert load_combined_model('fbs',version='desk_combined_v7') is None
    assert load_combined_model('fbs',version='desk_combined_v7',research_candidate=True)
