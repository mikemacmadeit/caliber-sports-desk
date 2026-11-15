import json
from datetime import timedelta
from types import SimpleNamespace
from spe.db.base import as_utc
from spe.ingest.recent_td_boxes import parse_box,overlay_history,retain_revisions
from spe.config import settings

NOW=as_utc('2026-11-14T17:00:00Z')


def fixture():
    game=SimpleNamespace(game_id='espn:fbs:1',home_team_id='fbs:1',away_team_id='fbs:2',kickoff_utc=NOW-timedelta(days=7))
    players={'fbs:10':SimpleNamespace(position_group='RB',name='Runner')}
    block=lambda team:{'team':{'id':team},'statistics':[{'name':'rushing','labels':['CAR','YDS','TD'],'athletes':[{'athlete':{'id':'10'},'stats':['18','100','2']}] if team=='1' else []},{'name':'receiving','labels':['REC','YDS','TD'],'athletes':[]}]}
    payload={'header':{'competitions':[{'id':'1','status':{'type':{'completed':True}}}]},'boxscore':{'players':[block('1'),block('2')]}}
    return payload,game,players


def test_complete_prior_box_requires_final_identity_and_both_teams():
    p,g,players=fixture();rows=parse_box(p,g,players,{},NOW)
    assert rows[0]['carries']==18 and rows[0]['rush_td']==2
    p['boxscore']['players'].pop()
    assert parse_box(p,g,players,{},NOW)==[]


def test_recovered_boxes_never_enter_strict_earlier_cutoff(db):
    p,g,players=fixture();rows=parse_box(p,g,players,{},NOW)
    path=settings.parquet_dir/'desk_evidence'/'recent_complete_boxes.json';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'games':{g.game_id:{'rows':rows,'observed_at':NOW.isoformat()}}}))
    assert overlay_history([],NOW-timedelta(hours=1))==[]
    assert len(overlay_history([],NOW-timedelta(hours=1),reconstructed=True))==1
    assert overlay_history([],g.kickoff_utc,reconstructed=True)==[]


def test_reconstruction_can_use_public_report_published_before_kickoff_but_not_after(db):
    from spe.db.models import Player,InjuryEvent
    from spe.models.player_td.desk_evidence import current_context
    from test_publication_pipeline import seed
    game=seed(db);db.add(Player(player_id='p',name='Player',position='WR',position_group='WR'));db.flush()
    report=InjuryEvent(game_id=game.game_id,player_id='p',designation='out',confidence=1.0,reported_at=NOW-timedelta(hours=2),observed_at=NOW+timedelta(hours=1),source='official',effective_at=NOW-timedelta(hours=2),provider='conference_public',provider_record_id='1',quality_status='official_public_report')
    db.add(report);db.flush()
    assert current_context(db,game.game_id,'p',NOW)['availability'] is None
    found=current_context(db,game.game_id,'p',NOW,reconstructed=True)['availability']
    assert found['status']=='out' and found['recovered_after_cutoff']
    report.reported_at=NOW+timedelta(seconds=1);db.flush()
    assert current_context(db,game.game_id,'p',NOW,reconstructed=True)['availability'] is None


def test_two_complete_boxes_without_role_require_confirmed_return():
    from test_selection_safety import candidate
    from spe.models.player_td.desk_replay import select_game_scorer
    r=candidate();r['desk_analysis']['features'].update(role_complete_boxes=2,role_recent_appearance_rate=0)
    r['desk_analysis']['current_context']['availability']=None
    assert select_game_scorer([r]) is None
    r['desk_analysis']['current_context']['availability']={'status':'available'}
    assert select_game_scorer([r]) is not None


def test_unchanged_refresh_preserves_first_observation():
    p,g,players=fixture()
    old={'rows':parse_box(p,g,players,{},NOW),'observed_at':NOW.isoformat()}
    later=NOW+timedelta(hours=6)
    new={'rows':parse_box(p,g,players,{},later),'observed_at':later.isoformat()}
    assert retain_revisions(old,new)==old


def test_revised_box_keeps_the_version_known_at_cutoff(db):
    p,g,players=fixture()
    earlier=NOW-timedelta(hours=2)
    old={'rows':parse_box(p,g,players,{},earlier),'observed_at':earlier.isoformat()}
    p['boxscore']['players'][0]['statistics'][0]['athletes'][0]['stats'][0]='19'
    later=NOW+timedelta(hours=1)
    new={'rows':parse_box(p,g,players,{},later),'observed_at':later.isoformat()}
    entry=retain_revisions(old,new)
    path=settings.parquet_dir/'desk_evidence'/'recent_complete_boxes.json';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'games':{g.game_id:entry}}))
    assert overlay_history([],NOW)[0]['carries']==18
    assert overlay_history([],NOW,reconstructed=True)[0]['carries']==19
