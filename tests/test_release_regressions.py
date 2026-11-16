"""Behavioral regressions for the production recheck, without external providers."""
import json
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session
from spe.config import settings
from spe.db.models import BoardSnapshot, InputArtifact, PublishedStat, Play
from test_publication_pipeline import NOW, seed


@pytest.mark.parametrize('source,expected', [('participation_unconfirmed', 'awaiting_box'),
    ('absent', 'awaiting_box'), ('confirmed_inactive', 'void'), ('played', 'blank')])
def test_td_card_and_both_lanes_agree_on_participation(db, source, expected):
    from spe.models.player_td.desk_replay import grade, _lane_result
    game = seed(db); game.status = 'final'
    db.add(PublishedStat(key='g|p|anytime_td', game_id='g', player_id='p', market_id='anytime_td',
        actual=0, source='espn.summary.'+source, observed_at=NOW)); db.flush()
    assert grade(db, game, 'p') == expected
    for lane in ('most_likely', 'value_candidate'):
        result = _lane_result(db, game, lane, {'player_id':'p', 'american':155, 'price_supported':True})
        assert result['result'] == expected
        if expected != 'blank':
            assert result['hypothetical_unit_profit'] is None
    game.status = 'canceled'
    assert grade(db, game, 'p') == 'void'


def test_live_td_is_highlighted_but_not_a_settled_lane_win(db):
    from spe.models.player_td.desk_replay import grade, _lane_result
    game = seed(db); game.status = 'in_progress'
    db.add(PublishedStat(key='g|p|anytime_td', game_id='g', player_id='p', market_id='anytime_td',
        actual=1, source='espn.live', observed_at=NOW)); db.flush()
    assert grade(db, game, 'p') == 'scored'
    assert _lane_result(db, game, 'most_likely', {'player_id':'p'})['result'] == 'pending'


def test_shared_input_reaches_independent_worker(db, tmp_path, monkeypatch):
    from spe.ingest.shared_inputs import publish_inputs, hydrate_inputs
    from spe.models.player_td.desk_evidence import load_evidence
    producer, consumer = tmp_path/'producer', tmp_path/'consumer'
    monkeypatch.setattr(settings, 'parquet_dir', producer)
    path = producer/'desk_evidence'/'fbs.json'; path.parent.mkdir(parents=True)
    payload = {'teams':[], 'players':[], 'sources':[{'observed_at':NOW.isoformat()}]}
    path.write_text(json.dumps(payload))
    publish_inputs(db, ('fbs.json',), now=NOW); db.commit()
    monkeypatch.setattr(settings, 'parquet_dir', consumer)
    with Session(db.get_bind()) as other:
        versions = hydrate_inputs(other)
    copied = consumer/'desk_evidence'/'fbs.json'
    assert json.loads(copied.read_text()) == payload
    assert copied.stat().st_mtime == pytest.approx(NOW.timestamp())
    assert versions['fbs.json']['refreshed_at'] == NOW.isoformat()
    assert load_evidence('fbs') is not None
    db.get(InputArtifact, 'fbs.json').checksum = 'invalid'; db.commit()
    with pytest.raises(ValueError, match='integrity'):
        hydrate_inputs(db)
    assert json.loads(copied.read_text()) == payload


def test_exporter_uses_measured_plays_on_shared_database(db, tmp_path):
    from spe.ingest.desk_evidence import export
    game = seed(db); game.status = 'final'
    for i, (rush, yardline) in enumerate(((True, 4), (False, 15), (True, 30))):
        db.add(Play(game_id='g', play_id=str(i), period=1, clock_seconds=800-i*25,
            offense_id='a', defense_id='b', play_type='rush' if rush else 'pass', yards=4,
            epa=.2, is_rush=rush, is_pass=not rush, is_td=i==0, is_turnover=False,
            is_scrimmage=True, yardline=yardline, drive_id='1', game_seconds_remaining=3500-i*25,
            rusher_player_id='p' if rush else None, receiver_player_id=None if rush else 'p',
            observed_at=NOW, effective_at=NOW, provider='test', provider_record_id=str(i)))
    db.flush()
    report = export(db.connection(), tmp_path)
    assert report['nfl']['team_games'] == 1
    payload = json.loads((tmp_path/'nfl.json').read_text())
    assert payload['players'][0]['inside5_carries'] == 1
    assert payload['players'][0]['inside20_targets'] == 1
    assert payload['teams'][0]['pace'] == 25
    assert payload['teams'][0]['rush_success'] == 1


def test_nested_input_failure_is_degraded(db, monkeypatch):
    from spe.ingest.free_desk_inputs import refresh_free_desk_inputs
    monkeypatch.setattr('spe.ingest.free_college.refresh_free_college', lambda *a, **k:{'status':'unavailable'})
    monkeypatch.setattr('spe.ingest.college_situations.refresh_college_situations', lambda *a, **k:{})
    monkeypatch.setattr('spe.ingest.desk_evidence.refresh_local_evidence', lambda *a, **k:{})
    monkeypatch.setattr('spe.ingest.college_rosters.refresh_college_rosters', lambda *a, **k:{'errors':['provider']})
    monkeypatch.setattr('spe.ingest.public_availability.refresh_public_availability', lambda *a, **k:{})
    monkeypatch.setattr('spe.ingest.recent_td_boxes.refresh_recent_boxes', lambda *a, **k:{'failed_games':1})
    result = refresh_free_desk_inputs(db, NOW)
    assert result['status'] == 'degraded'
    assert result['sources']['college']['status'] == 'degraded'
    assert len(result['warnings']) == 3
    assert db.query(InputArtifact).count() == 0


def test_empty_raw_play_database_retains_prior_measured_history(db, tmp_path):
    from spe.ingest.desk_evidence import export
    prior = {'teams':[{'game_id':'old', 'kickoff_utc':NOW.isoformat(), 'observed_at':NOW.isoformat()}],
             'players':[{'game_id':'old','player_id':'p','inside5_carries':2}]}
    (tmp_path/'nfl.json').write_text(json.dumps(prior))
    export(db.connection(), tmp_path)
    after = json.loads((tmp_path/'nfl.json').read_text())
    assert after['teams'] == prior['teams'] and after['players'] == prior['players']


def scenario_payload():
    return {'captured_at':NOW.isoformat(), 'market_context':{'home_implied_points':30., 'away_implied_points':17.},
        'candidates':[{'player_id':'p'+str(i), 'player_name':'Player '+str(i), 'team':team,
            'forecast':{'p_estimate':p}, 'position':'RB'}
            for i,(team,p) in enumerate((('A',.65),('A',.3),('B',.25)))]}


def test_scenario_preserves_baseline_and_applies_scoring_overrides():
    from app.scenario import simulate_saved
    payload = scenario_payload()
    baseline, _ = simulate_saved(payload, 'A', 'B', 'nfl', 50000, 42, {})
    raised, _ = simulate_saved(payload, 'A', 'B', 'nfl', 50000, 42, {'home_points':45})
    for r in payload['candidates']:
        assert baseline.player_td_prob[r['player_id']] == pytest.approx(r['forecast']['p_estimate'], abs=.01)
    assert raised.player_td_prob['p0'] > baseline.player_td_prob['p0'] + .1
    assert raised.player_td_prob['p2'] == baseline.player_td_prob['p2']
    assert baseline.research_only


@pytest.mark.parametrize('extra', [{'draw_count':-1}, {'draw_count':50001}, {'seed':-1},
    {'scenario_overrides':{'wind_mps':35}}, {'scenario_overrides':{'home_points':float('nan')}},
    {'scenario_overrides':{'away_points':0}}])
def test_scenarios_reject_invalid_requests(extra):
    from app.schemas import SimulationCreate
    with pytest.raises(ValidationError):
        SimulationCreate(game_id='g', **extra)


def test_scenario_requires_actual_saved_analysis(db):
    from app.scenario import run_scenario
    from app.schemas import SimulationCreate
    game = seed(db)
    with pytest.raises(HTTPException) as missing:
        run_scenario(db, game, SimulationCreate(game_id='g'))
    assert missing.value.status_code == 409
    db.add(BoardSnapshot(key='td-pregame-candidates-v1:g', payload=scenario_payload(), updated_at=NOW)); db.flush()
    result, names, provenance = run_scenario(db, game, SimulationCreate(game_id='g'))
    assert set(names) == {'p0', 'p1', 'p2'} and result.draw_count == 10000
    assert provenance['market_context']['home_implied_points'] == 30


def test_completed_td_game_is_durable_before_caller_commit(db, monkeypatch):
    from spe.models.player_td import desk_replay as replay
    from test_desk_combined import combined_model
    seed(db)
    monkeypatch.setattr(replay, 'load_replay_model', lambda *a:combined_model())
    monkeypatch.setattr(replay, 'load_combined_model', lambda *a, **k:combined_model())
    monkeypatch.setattr(replay, 'load_history', lambda *a:[])
    monkeypatch.setattr('spe.desk.day_results.refresh_live_touchdowns', lambda *a:None)
    db.commit()
    replay.replay_day(db, '2026-11-15', now=NOW, checkpoint=True)
    db.rollback()
    with Session(db.get_bind()) as observer:
        saved = observer.get(BoardSnapshot, replay.day_card_key('2026-11-15', 'all'))
        assert saved is not None and saved.payload['games'] == 1
        assert saved.payload['unavailable_games']


def test_year_excludes_archives_and_unpriced_returns(db, monkeypatch):
    from spe.publishing import ledger
    monkeypatch.setattr(ledger, 'slate_dates', lambda *a, **k:['2026-01-01','2026-01-02','2025-12-31'])
    monkeypatch.setattr(ledger, 'ledger_day', lambda db, date, league:{
        'record_status':'archived_unverified' if date.endswith('02') else 'saved_pregame',
        'rows':[{'result':'win','units':None,'market_id':'anytime_td'},
                {'result':'loss','units':-1.,'market_id':'receptions'}]})
    year = ledger.ledger_year(db, 2026)
    assert year['days'] == 1 and year['win'] == 1 and year['loss'] == 1
    assert year['units'] == -1 and year['priced'] == 1


def test_deployed_artifacts_resolve_both_leagues(monkeypatch):
    from pathlib import Path
    from spe.models.player_td.desk_combined import load_combined_model
    from spe.models.player_td.desk_replay import requested_retrain
    monkeypatch.setattr(settings, 'parquet_dir', Path(__file__).resolve().parents[1]/'deploy/runtime-data/parquet')
    for league in ('nfl', 'fbs'):
        assert load_combined_model(league) is not None
        requested = requested_retrain('2026-11-20', league)
        if requested:
            artifact = load_combined_model(league, research_candidate=not requested.get('use_validated_model'), version=requested['version'])
            assert artifact and artifact['artifact_sha256'] == requested['artifact_sha256']


def test_coordinate_directory_failure_does_not_block_existing_weather(db, monkeypatch):
    from spe.adapters.base import AdapterError
    from spe.ingest.open_meteo_history import _venue_records
    monkeypatch.setattr(settings, 'cfbd_api_key', 'test-key')
    def fail(*a, **kw):
        raise AdapterError('Rate limited')
    monkeypatch.setattr('spe.ingest.open_meteo_history.CfbdAdapter.fetch', fail)
    assert _venue_records(None, refresh=True) is None


def test_gameflow_source_reaches_other_worker(db, tmp_path, monkeypatch):
    import pandas as pd
    from spe.ingest.shared_inputs import publish_gameflow, hydrate_inputs
    from spe.simulate.desk_gameflow_inputs import _source, COLUMNS
    source = tmp_path/'plays.parquet'
    row = {c:None for c in COLUMNS}
    row.update(game_id='g', play_id=1, game_date='2026-11-01', posteam='A', defteam='B', rush_attempt=1)
    pd.DataFrame([row]).to_parquet(source)
    publish_gameflow(db, source, now=NOW); db.commit()
    monkeypatch.setattr(settings, 'parquet_dir', tmp_path/'other_worker')
    hydrate_inputs(db)
    path = settings.parquet_dir/'desk_evidence/nfl_gameflow.json'
    frame, checksum = _source(str(path), path.stat().st_mtime_ns)
    assert list(frame.game_id) == ['g'] and frame.iloc[0].rush_attempt == 1
    assert path.stat().st_mtime == pytest.approx(NOW.timestamp())


def test_input_providers_can_checkpoint_without_invalidating_each_other(db, monkeypatch):
    from spe.publishing.pipeline import inputs
    def provider(name):
        def run(db):
            db.add(BoardSnapshot(key=name, payload={}, updated_at=NOW))
            db.commit()
            assert db.get(BoardSnapshot, name) is not None
            return {'status':'ok'}
        return run
    monkeypatch.setattr('spe.ingest.nfl_availability.refresh_nfl_availability', provider('nfl'))
    monkeypatch.setattr('spe.ingest.espn_availability.ingest_espn_availability', provider('espn'))
    monkeypatch.setattr('spe.ingest.api_sports_injuries.ingest_api_sports_injuries', lambda *a, **k:{'status':'needs_key'})
    assert inputs(db)['status'] == 'ok'
    assert db.get(BoardSnapshot,'nfl') is not None and db.get(BoardSnapshot,'espn') is not None
