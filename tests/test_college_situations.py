import json
from datetime import timedelta
import pandas as pd
import pytest
from spe.db.base import as_utc
from spe.ingest.college_situations import summarize
from spe.ingest.college_rosters import normalize
from spe.models.player_td.desk_evidence import EvidenceHistory, personnel_context
from spe.models.player_td.situational import features

NOW=as_utc('2026-11-14T23:00:00Z')

def play(**kw):
    return {'game_id':1,'id':1,'pos_team_id':10,'homeTeamId':10,'awayTeamId':20,
        'status_type_completed':True,'penalty_no_play':False,'penalty_negated_play':False,
        'kneel_down':False,'type.text':'Rush','rush':True,'pass':False,'sack':False,
        'start.pos_score_diff':10,'start.down':1,'start.distance':2,'start.yardsToEndzone':4,
        'period':4,'statYardage':3,'passer_player_id':None,'pass_attempt':False,**kw}


def test_situations_exclude_dead_plays_and_use_pregame_side_of_each_play():
    game={'game_id':'espn:fbs:1','team_id':'fbs:10','opponent_id':'fbs:20',
          'kickoff_utc':'2026-11-01T18:00:00Z','observed_at':NOW.isoformat()}
    rows=[play(),play(),play(id=2,penalty_no_play=True),play(id=3,kneel_down=True),
          play(id=4,**{'type.text':'Two-Point Rush'}),play(id=5,status_type_completed=False),
          play(id=6,rush=False,sack=True,**{'pass':True,'start.pos_score_diff':-10,'statYardage':-5}),
          play(id=7,pass_attempt=True,passer_player_id=99,rush=False,**{'pass':True,'start.pos_score_diff':-10})]
    result=summarize(pd.DataFrame(rows),{('1','fbs:10'):game},{})[0]
    assert result['leading_pass_rate_d']==1 and result['leading_pass_rate_n']==0
    assert result['trailing_pass_rate_d']==2 and result['trailing_pass_rate_n']==2
    assert result['sack_rate_d']==2 and result['sack_rate_n']==1
    assert result['goal_line_pass_rate_d']==3 and result['goal_line_pass_rate_n']==2
    assert result['primary_passer']=='99' and result['passer_coverage']==1


def test_current_season_refresh_retains_prior_situations_without_old_parquets(db, tmp_path, monkeypatch):
    from spe.config import settings
    from spe.ingest.college_situations import refresh_college_situations
    monkeypatch.setattr(settings, 'parquet_dir', tmp_path/'parquet')
    evidence=settings.parquet_dir/'desk_evidence'; evidence.mkdir(parents=True)
    cache=tmp_path/'cache'/'sportsdataverse'; cache.mkdir(parents=True)
    prior={'game_id':'old', 'team_id':'fbs:10', 'kickoff_utc':'2025-11-01T18:00:00Z'}
    game={'game_id':'espn:fbs:1','team_id':'fbs:10','opponent_id':'fbs:20',
          'kickoff_utc':'2026-11-01T18:00:00Z','observed_at':NOW.isoformat()}
    (evidence/'fbs_situations.json').write_text(json.dumps({'teams':[prior]}))
    (evidence/'fbs_free_opportunities.json').write_text(json.dumps({'opportunity_games':[game],
        'sources':[{'source':f'https://source/play_by_play_{y}.parquet'} for y in (2025,2026)]}))
    pd.DataFrame([play()]).to_parquet(cache/'play_by_play_2026.parquet')
    refresh_college_situations(db, years=(2026,))
    result=json.loads((evidence/'fbs_situations.json').read_text())
    assert prior in result['teams']
    assert {r['game_id'] for r in result['teams']} == {'old','espn:fbs:1'}


def test_situation_features_reject_future_games_observations_and_tiny_samples():
    rows=[{'team_id':'t','opponent_id':'o','kickoff_utc':f'2026-11-{d:02}T18:00:00Z',
           'observed_at':NOW.isoformat(),'primary_passer':str(d),'primary_passer_share':1,'passer_coverage':1,
           'leading_pass_rate_n':3,'leading_pass_rate_d':10,'goal_line_pass_rate_n':1,'goal_line_pass_rate_d':1}
          for d in (1,8,15)]
    h=EvidenceHistory({'situations':rows},replay=True)
    f=h.features('t','o',NOW)
    assert f['offense_leading_pass_rate']==pytest.approx(.3)
    assert f['offense_goal_line_pass_rate'] is None
    assert f['historical_qb_change']==1
    assert EvidenceHistory({'situations':rows}).features('t','o',NOW-timedelta(hours=1))['offense_situation_known']==0


def test_rosters_are_identity_only_and_validate_team_and_season():
    payload={'team':{'id':'10'},'season':{'year':2026},'athletes':[{'items':[
        {'id':'99','displayName':'New Runner','position':{'abbreviation':'RB'}}]}]}
    row=normalize(payload,'fbs:10',NOW)[0]
    assert row['player_id']=='fbs:99' and 'rank' not in row and 'available' not in row
    with pytest.raises(ValueError):normalize(payload,'fbs:20',NOW)
    payload['season']['year']=2025
    with pytest.raises(ValueError):normalize(payload,'fbs:10',NOW)


def test_personnel_context_never_backdates_roster_observations(db):
    from spe.config import settings
    path=settings.parquet_dir/'desk_evidence/fbs_rosters.json';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'players':[
        {'player_id':'p','team_id':'t','position':'RB','name':'Runner','observed_at':NOW.isoformat()},
        {'player_id':'lineman','team_id':'t','position':'OT','name':'Blocker','observed_at':NOW.isoformat()}]}))
    reports={'lineman':{'status':'out','source':'official'}}
    assert not personnel_context('g','p',NOW-timedelta(hours=1),reports)
    alerts=personnel_context('g','p',NOW,reports)
    assert alerts[0]['side']=='teammate' and alerts[0]['probability_effect']=='not_fitted'
