import copy
from datetime import timedelta

import pytest

from spe.db.base import as_utc
from spe.models.player_td.desk_evidence import EvidenceHistory, METRICS, RICH_FEATURES, TEAM_RICH_FEATURES
from spe.models.player_td.desk_combined import team_vector, combined_prediction
from test_desk_combined import allocator, allocate, combined_model, CUTOFF


def rows():
    return [{"game_id":str(i),"team_id":"home","opponent_id":"away",
             "kickoff_utc":f"2026-09-{day:02}T18:00:00Z", "observed_at":"2026-09-20T18:00:00Z",
             **{k:float(i) for k in METRICS},"pass_allowed_residual":.2,"rush_allowed_residual":.1}
            for i,day in enumerate((1,8,15),1)]


def test_future_and_late_retrieved_inputs_do_not_enter_live_features():
    source=rows()
    cutoff=as_utc('2026-09-19T18:00:00Z')
    live=EvidenceHistory({'teams':source})
    assert live.features('home','away',cutoff)['offense_evidence_known']==0
    replay=EvidenceHistory({'teams':source},replay=True)
    before=replay.features('home','away',cutoff)
    source.append({**source[-1],"kickoff_utc":"2026-09-19T18:00:00Z","pass_epa":999})
    assert EvidenceHistory({'teams':source},replay=True).features('home','away',cutoff)==before
    assert before['defense_pass_schedule_residual']==pytest.approx(.2)


def test_season_reset_stale_history_and_wrong_team_cannot_supply_features():
    engine=EvidenceHistory({'teams':rows()},replay=True)
    assert engine.features('home','away',as_utc('2027-09-01'))['offense_evidence_known']==0
    assert engine.features('home','away',as_utc('2026-11-01'))['offense_evidence_known']==0
    assert engine.features('new_team','away',CUTOFF)['offense_evidence_known']==0


def test_missing_college_targets_are_unknown_not_zero_or_catches():
    engine=EvidenceHistory({'teams':rows()},replay=True)
    f=engine.features('home','away',CUTOFF,'player')
    assert f['player_pbp_known']==0
    assert f['measured_targets'] is None


def test_verified_zero_touch_appearance_stays_in_player_average():
    team=[{**r,'identity_coverage':1.,'field_coverage':1.} for r in rows()[:2]]
    player=[{**team[0],'player_id':'p',**{k:4. for k in ('targets','inside20_carries','inside5_carries','inside20_targets','inside5_targets')}}]
    engine=EvidenceHistory({'teams':team,'players':player},replay=True)
    f=engine.features('home','away',CUTOFF,'p',team)
    assert f['player_pbp_known']==1
    assert f['measured_targets']==pytest.approx(4*.8/1.8)
    incomplete=[{**r,'player_id':'player','identity_coverage':.5,'targets':10} for r in rows()]
    f=EvidenceHistory({'players':incomplete},replay=True).features('home','away',CUTOFF,'player')
    assert f['measured_targets'] is None


def test_matchup_features_change_both_components_and_preserve_team_mass():
    engine=allocator()
    engine.history.evidence=EvidenceHistory({'teams':rows()},replay=True)
    m=copy.deepcopy(combined_model())
    m['rich_features']=True
    base=m['player_model']
    base['features']+=RICH_FEATURES
    for key,value in (('coefficients',0.),('center',0.),('scale',1.)):
        base[key]+=[value]*len(RICH_FEATURES)
    base['coefficients'][base['features'].index('defense_rush_epa')]=.1
    for kind in ('rush','rec'):
        m['team_coefficients'][kind]+=[0.]*len(TEAM_RICH_FEATURES)
    m['team_coefficients']['rush'][6+TEAM_RICH_FEATURES.index('defense_rush_epa')]=.1
    f=engine.history.features('p','home','away','RB',CUTOFF,True)
    original=combined_prediction(m,f,allocate(engine,m),'p')
    assert original
    weak=copy.deepcopy(rows())
    for r in weak: r['rush_epa']+=1
    engine.history.evidence=EvidenceHistory({'teams':weak},replay=True)
    f=engine.history.features('p','home','away','RB',CUTOFF,True)
    allocation=allocate(engine,m)
    changed=combined_prediction(m,f,allocation,'p')
    assert changed['player_probability']>original['player_probability']
    assert changed['allocation_probability']>original['allocation_probability']
    for kind in ('rush','rec'):
        assert sum(p[kind+'_lambda'] for p in allocation['players'].values())+allocation['unassigned_expected_tds'][kind]==pytest.approx(allocation['team_expected_tds'][kind])


def test_empty_context_is_explicit(db):
    from spe.models.player_td.desk_evidence import current_context
    context=current_context(db,'missing','missing',CUTOFF)
    assert context['weather'] is None and context['availability'] is None
    assert 'charted_routes' in context['missing']


def test_unsupported_college_stat_types_do_not_invent_targets(db):
    from spe.ingest.cfbd_play_stats import ingest_play_stats
    result=ingest_play_stats(db,[{'gameId':123,'playId':456,'athleteId':789,'statType':'Passing Yards'}])
    assert result['updated']==0 and result['unresolved_or_unsupported']==1


def test_export_excludes_defensive_touchdowns_and_preserves_unknown_identity(db, tmp_path):
    import json
    import sqlite3
    from spe.db.models import Play
    from spe.ingest.desk_evidence import export
    from test_publication_pipeline import seed, NOW
    game=seed(db)
    game.status='final'
    game.kickoff_utc=NOW-timedelta(days=7)
    for i in (1,2):
        db.add(Play(game_id=game.game_id,play_id=str(i),period=1,clock_seconds=800-i*25,
            offense_id=game.home_team_id,defense_id=game.away_team_id,play_type='pass',yards=0,
            yardline=10,epa=-.3,is_pass=True,is_scrimmage=True,is_td=i==2,is_turnover=i==2,
            drive_id='drive',game_seconds_remaining=3500-i*25,provider='test',provider_record_id=str(i),
            observed_at=NOW,effective_at=NOW))
    db.flush()
    raw=db.connection().connection.driver_connection
    previous=raw.row_factory
    raw.row_factory=sqlite3.Row
    try: report=export(raw,tmp_path/'evidence')
    finally: raw.row_factory=previous
    data=json.loads((tmp_path/'evidence/nfl.json').read_text())
    assert report['nfl']['team_games']==1
    assert data['teams'][0]['red_zone_td_rate']==0
    assert data['teams'][0]['pace']==25
    assert data['players']==[]


def test_forecast_must_have_been_received_before_pick(db):
    from spe.db.models import WeatherSnapshot
    from spe.models.player_td.desk_evidence import current_context
    from test_publication_pipeline import seed, NOW
    game=seed(db)
    forecast=WeatherSnapshot(venue_id=game.venue_id,game_id=game.game_id,forecast_for=game.kickoff_utc,
        issued_at=NOW-timedelta(hours=1),observed_at=NOW-timedelta(minutes=30),
        ingested_at=NOW+timedelta(minutes=1),effective_at=NOW-timedelta(hours=1),
        provider='nws',provider_record_id='weather',quality_status='nws_forecast',wind_mps=10)
    db.add(forecast);db.flush()
    assert current_context(db,game.game_id,'p',NOW)['weather'] is None
    forecast.ingested_at=NOW-timedelta(minutes=30);db.flush()
    assert current_context(db,game.game_id,'p',NOW)['weather']['wind_mps']==10


def test_late_nfl_context_not_used_for_earlier_pick():
    from spe.models.player_td.nflverse_context import _available
    row={'kickoff_utc':CUTOFF-timedelta(days=7),'nflverse_context':{'team':{
        'effective_at':CUTOFF-timedelta(days=5),'observed_at':CUTOFF+timedelta(hours=1),'metrics':{'plays':70}}}}
    assert _available(row,'team',CUTOFF) is None
