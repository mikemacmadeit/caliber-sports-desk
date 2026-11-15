from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from spe.models.player_td import gameflow_stage as stage
from spe.simulate.desk_gameflow_inputs import InputsUnavailable, build_inputs
from test_desk_gameflow import fixture

NOW = datetime(2026, 11, 15, 16, tzinfo=UTC)
CONFIG = {'version':stage.VERSION, 'draws':10000, 'seed':20261115}


def setup():
    inputs = fixture()
    inputs['source_pbp_sha256'] = 'fixture-source'
    rows = []
    for tid, team in inputs['teams'].items():
        for player in team['players']:
            rows.append({'player_id':player['id'], 'forecast':{'p_estimate':.45},
                'desk_analysis':{'combination':{'probability':.45, 'combined_probability':.45,
                    'scoring_paths':{'baseline_probability':.4, 'probability_low':.35, 'probability_high':.55}}}})
    model = {'artifact_sha256':'fixture', 'scoring_paths':{'calibration':[0., .6, .4]}}
    game = SimpleNamespace(game_id='nfl:game', league_id='nfl', status='scheduled',
        kickoff_utc=NOW+timedelta(hours=1), home_team_id='nfl:B', away_team_id='nfl:A')
    return inputs, rows, model, game


def test_simulation_updates_every_candidate_once_without_mutating_model_inputs(monkeypatch):
    inputs, rows, model, game = setup()
    monkeypatch.setattr(stage, 'build_inputs', lambda *a:inputs)
    before = deepcopy(rows)
    updated, run = stage.apply(rows, model, game, NOW, CONFIG)
    assert rows == before
    assert run['status'] == 'active' and run['total_simulated_games'] == 50000
    assert updated[0]['forecast']['p_estimate'] != .45
    for row in updated:
        p = row['forecast']['p_estimate']
        s = row['desk_analysis']['simulation']
        assert s['adjusted_probability'] == p
        assert row['desk_analysis']['combination']['probability'] == p
        assert s['baseline_probability'] == .45
        assert s['probability_low'] <= min(s['scenario_probabilities'].values())
        assert s['probability_high'] >= max(s['scenario_probabilities'].values())
        assert row['forecast']['play_enabled'] is False
    repeated, repeated_run = stage.apply(rows, model, game, NOW, CONFIG)
    assert repeated == updated and repeated_run['results'] == run['results']
    with pytest.raises(ValueError, match='already applied'):
        stage.apply(updated, model, game, NOW, CONFIG)


@pytest.mark.parametrize('change', ['started', 'final', 'college', 'disabled'])
def test_started_games_and_college_are_never_simulated(monkeypatch, change):
    _, rows, model, game = setup()
    config = CONFIG
    if change == 'started':game.kickoff_utc = NOW
    if change == 'final':game.status = 'final'
    if change == 'college':game.league_id = 'fbs'
    if change == 'disabled':config = None
    monkeypatch.setattr(stage, 'build_inputs', lambda *a:pytest.fail('Protected game reached simulator'))
    updated, run = stage.apply(rows, model, game, NOW, config)
    assert updated is rows and run is None


def test_missing_source_retains_original_estimates_and_explicitly_labels_fallback(monkeypatch):
    _, rows, model, game = setup()
    def missing(*args):raise InputsUnavailable('No prior plays')
    monkeypatch.setattr(stage, 'build_inputs', missing)
    before = deepcopy(rows)
    updated, run = stage.apply(rows, model, game, NOW, CONFIG)
    assert rows == before
    assert run['status'] == 'unavailable' and run['baseline_retained']
    assert all(r['forecast']['p_estimate'] == .45 for r in updated)
    assert all(r['desk_analysis']['simulation']['status'] == 'unavailable' for r in updated)


def test_builder_rejects_after_kickoff_information_before_reading_source(tmp_path):
    with pytest.raises(InputsUnavailable, match='before kickoff'):
        build_inputs({'kickoff':NOW.isoformat()}, [], NOW, tmp_path/'missing.parquet')


def test_research_default_is_pinned_and_available_to_hosted_refresh(db, monkeypatch):
    import json
    from spe.config import settings
    from spe.models.player_td.desk_replay import requested_retrain
    path=settings.parquet_dir/'models/desk_retrain_runs.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'research_defaults':{'nfl':{'effective_from':'2026-11-15',
        'version':'desk_combined_v11','artifact_sha256':'fixed','simulation':CONFIG}}}))
    monkeypatch.setattr(settings,'app_env','prod')
    assert requested_retrain('2026-11-14','nfl') is None
    assert requested_retrain('2026-11-15','fbs') is None
    assert requested_retrain('2026-11-16','nfl')['simulation']==CONFIG
    assert requested_retrain('2026-11-16','nfl')['artifact_sha256']=='fixed'


def test_saved_candidate_snapshot_keeps_simulation_provenance(db):
    from spe.db.models import BoardSnapshot
    from spe.models.player_td.pregame_candidates import save_candidates
    from test_publication_pipeline import seed, NOW as cutoff
    game=seed(db)
    row={'player_id':'p','forecast':{'p_estimate':.5,'data_cutoff':cutoff.isoformat()},
         'desk_analysis':{'simulation':{'status':'active','baseline_probability':.48,'raw_probability':.6}}}
    run={'status':'active','input_sha256':'frozen','inputs':{'historical_games':['prior']}}
    assert save_candidates(db,game,[row],None,cutoff,'fixed',simulation=run)
    db.flush()
    saved=db.get(BoardSnapshot,'td-pregame-candidates-v1:'+game.game_id).payload
    assert saved['simulation']==run
    assert saved['candidates'][0]['desk_analysis']['simulation']['baseline_probability']==.48
    assert not save_candidates(db,game,[row],None,game.kickoff_utc,'changed',simulation={'bad':True})
    assert db.get(BoardSnapshot,'td-pregame-candidates-v1:'+game.game_id).payload==saved
