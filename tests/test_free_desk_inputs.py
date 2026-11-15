import copy
from datetime import timedelta

import pandas as pd
import pytest

from spe.db.base import as_utc
from spe.ingest.free_college import summarize
from spe.ingest.public_availability import normalize_reports, status_code
from spe.models.player_td.desk_evidence import EvidenceHistory
from spe.models.player_td.desk_combined import opportunity_vector

NOW=as_utc('2026-11-14T22:00:00Z')


def play(**kw):
    return {'game_id':123,'id':1,'pos_team_id':1,'homeTeamId':1,'awayTeamId':2,
        'rush':True,'target':False,'penalty_no_play':False,'penalty_negated_play':False,
        'kneel_down':False,'type.text':'Rush','rusher_player_id':42,'receiver_player_id':None,
        'start.yardsToEndzone':4,'status_type_completed':True,'period':1,**kw}


def summarized(plays):
    return summarize(pd.DataFrame(plays),{'123':{'game_id':'espn:fbs:123','home':'fbs:1','away':'fbs:2',
        'kickoff_utc':'2026-11-01T18:00:00Z'}},{('123','fbs:42','fbs:1')},{'42':'fbs:42'}, {},NOW.isoformat())[0]


def test_count_standing_touches_only_and_do_not_count_passing_as_qb_touchdown():
    rows=[play(),play(),play(id=2,penalty_no_play=True),play(id=3,kneel_down=True),
          play(id=4,**{'type.text':'Two-Point Rush'}),play(id=5,status_type_completed=False),
          play(id=6,rush=False,target=True,rusher_player_id=None,receiver_player_id=42,
               **{'type.text':'Pass Incompletion','start.yardsToEndzone':12})]
    p=summarized(rows)['players'][0]
    assert p['carries']==1 and p['inside5_carries']==1
    assert p['targets']==1 and p['inside20_targets']==1
    assert 'pass_td' not in p


def test_missing_or_wrong_team_identity_reduces_coverage_not_a_zero():
    result=summarized([play(),play(id=2,rusher_player_id=None),play(id=3,pos_team_id=2)])
    team=next(t for t in result['opportunity_games'] if t['team_id']=='fbs:1')
    assert team['rush_coverage']==.5
    assert len(result['players'])==1
    assert result['players'][0]['identity_coverage']==.5


def test_game_side_mismatch_does_not_join():
    assert summarized([play(homeTeamId=7)])['players']==[]


def test_rushing_can_be_measured_when_receiver_identities_are_incomplete():
    payload={'opportunity_games':[],'players':[]}
    appearances=[]
    for i,day in enumerate((1,8)):
        game={'game_id':f'espn:fbs:{i}','team_id':'fbs:1','opponent_id':'fbs:2',
              'kickoff_utc':f'2026-11-{day:02}T18:00:00Z','observed_at':NOW.isoformat(),
              'rush_coverage':1.,'target_coverage':.5,'field_coverage':1.,'identity_coverage':.7}
        payload['opportunity_games'].append(game)
        payload['players'].append({**game,'player_id':'p','carries':12,'inside5_carries':3,'inside20_carries':5,
                                  'targets':2,'inside20_targets':1,'inside5_targets':0})
        appearances.append({**game,'game_id':f'fbs:{i}'})
    f=EvidenceHistory(payload,replay=True).features('fbs:1','fbs:2',NOW,'p',appearances)
    assert f['rush_pbp_known']==1 and f['target_pbp_known']==0
    assert f['free_inside5_carries']==3 and f['free_targets'] is None
    assert opportunity_vector(f,'rush','fbs',True).tolist()==[9,3,0]
    # First observed after the historical cutoff must never enter live features.
    assert EvidenceHistory(payload).features('fbs:1','fbs:2',NOW-timedelta(hours=1),'p',appearances)['rush_pbp_known']==0
    # An absent touch row is zero only when the team's field data is complete.
    payload['players']=[]
    for game in payload['opportunity_games']:
        game['field_coverage']=.4
    missing=EvidenceHistory(payload,replay=True).features('fbs:1','fbs:2',NOW,'p',appearances)
    assert missing['rush_pbp_known']==0 and missing['free_inside5_carries'] is None


def report():
    return {'r':{'conferenceTimeZone':'CT','publishDate':'2026-11-14','postedTime':'13:45:00',
        'footer':{'date':'2026-11-14'},'games':[
        {'teamDisplayName':'Auburn','rows':[{'name':'RB #1 Runner Jr.','status':'Out'},
                                         {'name':'WR #2 Receiver','status':'Out - (1st Half)'}]},
        {'teamDisplayName':'Mississippi State','rows':[{'name':'QB #3 Passer','status':'Game Time Decision'}]}]}}


def test_public_report_timestamps_statuses_and_no_implicit_healthy_players():
    rows=normalize_reports(report(),'SEC',NOW)
    assert len(rows)==3
    assert rows[0]['name']=='Runner Jr.' and rows[0]['status']=='out'
    assert rows[0]['reported_at']==as_utc('2026-11-14T19:45:00Z')
    assert rows[1]['status']=='uncertain' and rows[2]['status']=='uncertain'
    assert status_code('Not submitted') is None
    assert normalize_reports(report(),'SEC',as_utc('2026-11-14T19:00:00Z'))==[]


def test_public_report_import_is_idempotent_and_respects_first_observation(db):
    from test_publication_pipeline import seed
    from spe.db.models import Player, PlayerGameStat, InjuryEvent, League
    from spe.ingest.public_availability import store_reports
    game=seed(db)
    db.add(League(league_id='fbs',sport_id='s',name='College',classification='fbs'))
    db.flush()
    game.league_id='fbs';game.kickoff_utc=as_utc('2026-11-14T23:00:00Z')
    game.home_team.name='Auburn';game.away_team.name='Mississippi State'
    game.home_team.league_id='fbs';game.away_team.league_id='fbs'
    db.add(Player(player_id='runner',name='Runner Jr.',position='RB',position_group='RB'));db.flush()
    db.add(PlayerGameStat(game_id=game.game_id,team_id=game.home_team_id,player_id='runner',provider='test',
        provider_record_id='box',observed_at=NOW,effective_at=NOW));db.flush()
    rows=normalize_reports(report(),'SEC',NOW)
    result=store_reports(db,rows);db.flush()
    assert result['inserted']==1
    repeated=copy.deepcopy(rows)
    repeated[0]['observed_at']=NOW+timedelta(minutes=10)
    assert store_reports(db,repeated)['duplicates']==1
    event=db.query(InjuryEvent).filter_by(provider='conference_public').one()
    assert as_utc(event.observed_at)==NOW and event.designation=='out'
