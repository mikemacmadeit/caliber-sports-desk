from copy import deepcopy
from datetime import timedelta

import pytest

from spe.models.player_td.matchup_research import build, contextual_share, case_score
from spe.models.player_td.scoring_decision import choose
from spe.db.base import as_utc
from test_td_scoring_paths import priced, path_history, path_model
from test_desk_combined import CUTOFF
from spe.models.player_td.desk_combined import AllocationHistory
from spe.models.player_td.scoring_paths import CONTEXT_VERSION


def split_series(total, rz, qb='q'):
    return {'version':2,'buckets':{'rec_redzone':{'opportunities':rz},'rec_other':{'opportunities':total-rz},'rec_inside20':{'opportunities':rz}},
        'passers':{qb:{'rec_redzone':{'opportunities':rz},'rec_other':{'opportunities':total-rz},'rec_inside20':{'opportunities':rz}}}}


def test_current_passer_specific_role_changes_share_and_excludes_absent_qb():
    offense=[split_series(30,10)]*3
    players={'a':{'position':'TE','series':[split_series(10,7)]*3},
        'b':{'position':'WR','series':[split_series(20,3)]*3}}
    facts=build(offense,offense,players)
    assert facts['a']['quarterback']['redzone_team_rank']==1
    assert facts['a']['quarterback']['player_redzone_targets']==21
    assert facts['a']['quarterback']['usable']
    assert contextual_share(.3,'rec_redzone',players['a']['series'],offense,facts['a'])>.3
    assert contextual_share(.3,'rush_5',players['a']['series'],offense,facts['a'])==.3
    absent=build(offense,offense,players,{'nfl:q'})
    assert not absent['a']['quarterback']['usable']
    assert contextual_share(.3,'rec_redzone',players['a']['series'],offense,absent['a'])==.3
    assert not build(offense[:1],offense,{'a':{'position':'TE','series':players['a']['series'][:1]}})['a']['quarterback']['usable']


def test_unknown_and_new_qb_does_not_borrow_previous_quarterbacks_split():
    offense=[split_series(30,10)]*3
    players={'a':{'position':'WR','series':[split_series(10,7)]*3}}
    facts=build(offense,offense,players,context={'starting_qb_id':'nfl:new'})
    assert not facts['a']['quarterback']['usable']
    assert facts['a']['quarterback']['player_targets']==0
    assert contextual_share(.3,'rec_redzone',players['a']['series'],offense,facts['a'])==.3


def test_football_advantage_can_select_alternative_ahead_of_higher_probability():
    main=priced('main',.65,-250)
    volume=priced('volume',.40,250,.35)
    matchup=priced('matchup',.36,300,.30)
    volume['desk_analysis']['combination']['scoring_paths']['paths'][0]['opponent_rate_ratio']=1
    pick=choose([main,volume,matchup])
    assert pick['player_name']=='main'
    assert pick['decision']['value_candidate']['name']=='matchup'
    assert 'opponent_scoring_area_weakness' in pick['decision']['value_candidate']['research_rank']['advantages']


def test_expired_quote_retains_same_football_candidate_without_value_claim():
    main=priced('main',.65,-250)
    alt=priced('alternative',.4,250,.35)
    fresh=choose([main,alt])['decision']['value_candidate']
    assert fresh['price_supported']
    for r in (main,alt):
        old=(as_utc(r['forecast']['data_cutoff'])-timedelta(hours=12)).isoformat()
        r['source_timestamp']=r['price_source_at']=old
    stale=choose([main,alt])['decision']['value_candidate']
    assert stale['name']==fresh['name']
    assert stale['research_selected'] and not stale['price_supported'] and not stale['bet_qualified']
    assert 'price needs refreshing' in stale['customer']['price']
    assert stale['minimum_american']>=185
    for r in (main,alt):
        r['book_price']=None
    assert choose([main,alt])['decision']['value_candidate']['name']=='alternative'


def test_future_quote_never_becomes_reference_or_actionable():
    r=priced('p',.4,250,.35)
    r['price_source_at']=(as_utc(r['forecast']['data_cutoff'])+timedelta(hours=1)).isoformat()
    from spe.models.player_td.scorer_value import quote_value,reference_quote
    assert quote_value(r) is None and reference_quote(r) is None


def test_new_allocator_conserves_team_scoring_budget_and_rejects_future_history():
    history=path_history();model=path_model();model['scoring_paths']['version']=CONTEXT_VERSION
    allocation=AllocationHistory(history).allocate(model,'home','away',CUTOFF,True)
    for kind in ('rush','rec'):
        assert sum(r[kind+'_lambda'] for r in allocation['scoring_paths']['players'].values())<=allocation['team_expected_tds'][kind]+1e-10
    for rows in history.players.values():
        for r in rows:
            r['nflverse_context']['team']['observed_at']=(CUTOFF+timedelta(days=1)).isoformat()
    assert AllocationHistory(history).allocate(model,'home','away',CUTOFF,True)['scoring_paths'] is None


def test_no_outcome_is_used_to_choose_or_write_evidence():
    main=priced('main',.65,-250);alt=priced('alt',.4,250,.35)
    before=choose([main,alt])
    main.update(result='blank',actual=0,final_score='0-40')
    alt.update(result='scored',actual=3,final_score='0-40')
    after=choose([main,alt])
    assert before['decision']==after['decision']


def test_future_research_does_not_silently_change_champion_probabilities():
    from spe.models.player_td.matchup_research import attach_unfitted_research
    base={'probability':.4,'player_probability':.35}
    allocation={'scoring_paths':{'players':{'p':{'probability':.7,'paths':[],'research':{'source':'PBP'}}}}}
    result=attach_unfitted_research(base,allocation,'p','digest')
    assert result['probability']==.4 and result['scoring_paths']['probability']==.7
    assert result['scoring_paths']['status']=='research_only'
    assert result['scoring_paths']['research_artifact_sha256']=='digest'
    assert base=={'probability':.4,'player_probability':.35}


def test_standing_local_model_is_explicit_hash_pinned_and_never_production(db, monkeypatch):
    import json
    from spe.config import settings
    from spe.models.player_td.desk_replay import requested_retrain
    p=settings.parquet_dir/'models/desk_retrain_runs.json';p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps({'local_defaults':{'nfl':{'effective_from':'2026-11-16','version':'desk_combined_v11','artifact_sha256':'pin'}},
        '2026-11-17':{'nfl':{'version':'explicit','artifact_sha256':'date-pin'}}}))
    monkeypatch.setattr(settings,'app_env','dev')
    assert requested_retrain('2026-11-16','nfl')['artifact_sha256']=='pin'
    assert requested_retrain('2026-11-16','nfl')['standing_local']
    assert requested_retrain('2026-11-15','nfl') is None
    assert requested_retrain('2026-11-16','fbs') is None
    assert requested_retrain('2026-11-17','nfl')['version']=='explicit'
    monkeypatch.setattr(settings,'app_env','prod')
    assert requested_retrain('2026-11-16','nfl') is None


def test_standing_model_update_never_replaces_a_started_game_snapshot(db, monkeypatch):
    import json
    from spe.config import settings
    from spe.db.models import BoardSnapshot,Player,PublishedStat
    from test_publication_pipeline import NOW,seed
    from test_desk_combined import combined_model
    import spe.models.player_td.desk_replay as replay
    game=seed(db);game.kickoff_utc=NOW-timedelta(hours=5);game.status='final'
    db.add(Player(player_id='original',name='Original',position_group='RB',position='RB'));db.flush()
    db.add(PublishedStat(key='g|original|anytime_td',game_id='g',player_id='original',market_id='anytime_td',actual=1,source='espn.summary:test',observed_at=NOW))
    pick=choose([priced('original',.5,200)])
    pick.update(game_id='g',kickoff_utc=game.kickoff_utc.isoformat(),retrospective=False)
    original=deepcopy(pick)
    db.add(BoardSnapshot(key='scorer-desk-v1:g',updated_at=NOW-timedelta(hours=6),payload={'row':pick,'evaluated':True}));db.flush()
    p=settings.parquet_dir/'models/desk_retrain_runs.json';p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps({'local_defaults':{'nfl':{'effective_from':'2026-11-15','version':'desk_combined_v11','artifact_sha256':'new-version'}}}))
    monkeypatch.setattr(settings,'app_env','dev')
    monkeypatch.setattr(replay,'load_combined_model',lambda *a,**kw:{**combined_model(),'artifact_sha256':'new-version'})
    monkeypatch.setattr(replay,'load_history',lambda *a,**kw:(_ for _ in ()).throw(AssertionError('Started game must stay frozen')))
    board=replay.replay_day(db,'2026-11-15',now=NOW,cached_only=True)
    assert board['rows'][0]['player_id']=='original' and board['rows'][0]['forecast']==original['forecast']
    assert board['rows'][0]['result']=='scored'
    assert db.get(BoardSnapshot,'scorer-desk-v1:g').payload['row']==original


def test_receiving_touchdown_never_becomes_a_passers_anytime_touchdown():
    from spe.analytics.nflverse_opportunities import pbp_context
    import pandas as pd
    plays=pd.DataFrame([{'game_id':'2026_01_A_B','play_id':1,'posteam':'A','defteam':'B','play_type':'pass',
        'yardline_100':8,'air_yards':9,'receiver_player_id':'r','rusher_player_id':None,'passer_player_id':'q',
        'pass_touchdown':1,'rush_touchdown':0}])
    rows=pbp_context(plays,{'r':'TE','q':'QB'})
    receiver=next(r for r in rows if r['dataset']=='pbp_player')
    assert receiver['entity_id']=='nfl:r'
    paths=receiver['metrics']['scoring_paths']
    assert paths['passers']['q']['rec_inside20']['tds']==1
    assert not any(r['entity_id']=='nfl:q' for r in rows if r['dataset']=='pbp_player')
