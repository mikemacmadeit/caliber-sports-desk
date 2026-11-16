from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from spe.models.player_td.folds import fit_upstream, split_calibration
from spe.models.player_td.model import fit_player_td_model, predict_frame
from spe.models.player_td.shares import attach_shares, default_weights


def frames():
    rng = np.random.default_rng(42)
    dates = pd.date_range('2023-01-01', periods=150, freq='7D', tz='UTC')
    team = pd.DataFrame({'game_id': [f'g{i}' for i in range(150)], 'team_id': 'T', 'kickoff_utc': dates,
                         'possessions': 12, 'off_plays': 65, 'rz_plays': 8, 'scoring_drives': 4, 'prior_off_td': rng.uniform(1, 4, 150), 'opp_prior_off_td_allowed': 2.5, 'home_field': 0,
                         'off_td': rng.poisson(3, 150), 'pass_td': rng.poisson(2, 150), 'rush_td': rng.poisson(1, 150)})
    p = pd.DataFrame({'game_id': team.game_id, 'team_id': 'T', 'kickoff_utc': dates, 'position_group': 'RB',
                      'player_id': 'p', 'prior_carries': 10, 'prior_receptions': 2, 'prior_targets': 3,
                      'prior_rush_td': .3, 'prior_rec_td': .1, 'rush_td': rng.binomial(1,.3,150), 'rec_td': 0})
    p['anytime_tds'] = p.rush_td + p.rec_td
    return team, p


def test_future_outcomes_do_not_change_upstream_fit_or_test_probabilities():
    team, p = frames()
    args = (p.iloc[:90], p.iloc[90:120], p.iloc[120:], 'nfl', p.kickoff_utc.iloc[90])
    a = fit_upstream(team, *args)
    mutated = team.copy()
    mutated.loc[90:, ['off_td','pass_td','rush_td']] = 99
    b = fit_upstream(mutated, *args)
    assert a[-1]['team_artifact_sha256'] == b[-1]['team_artifact_sha256']
    assert np.allclose(a[2].lambda_player_td, b[2].lambda_player_td)
    assert a[-1]['team_last_training_kickoff'] < a[-1]['team_fit_cutoff']


def test_calibration_is_independent_and_overlap_rejected():
    team, p = frames()
    fit, cal, test, w, meta = fit_upstream(team, p.iloc[:90], p.iloc[90:120], p.iloc[120:], 'nfl', p.kickoff_utc.iloc[90])
    raw = fit_player_td_model(fit, 'nfl', meta['team_fit_cutoff'], w)
    assert (raw.platt_a, raw.platt_b) == (0, 1)
    assert np.allclose(predict_frame(raw, test).p_atd, 1 - np.exp(-test.lambda_player_td))
    with pytest.raises(ValueError, match='strictly after'):
        fit_player_td_model(fit, 'nfl', meta['team_fit_cutoff'], w, calibration=fit)
    with pytest.raises(ValueError, match='overlap'):
        fit_upstream(team, fit, test, cal, 'nfl', p.kickoff_utc.iloc[90])


def test_split_keeps_game_and_simultaneous_kickoffs_together():
    _, p = frames()
    repeated = pd.concat([p,p], ignore_index=True).sample(frac=1, random_state=1)
    fit, cal = split_calibration(repeated)
    assert set(fit.game_id).isdisjoint(cal.game_id)
    assert fit.kickoff_utc.max() < cal.kickoff_utc.min()


def test_inactive_teammate_removed_before_allocation():
    _, p = frames()
    sample = pd.concat([p.iloc[:1], p.iloc[:1]], ignore_index=True)
    sample['player_id'] = ['starter','backup']
    sample['availability_status'] = ['expected_active','expected_active']
    before = attach_shares(sample, default_weights('nfl'))
    sample.loc[0, 'availability_status'] = 'out'
    after = attach_shares(sample, default_weights('nfl'))
    assert after.loc[0,'rush_td_share'] == after.loc[0,'rec_td_share'] == 0
    assert after.loc[1,'rush_td_share'] > before.loc[1,'rush_td_share']
    sample['availability_status'] = 'out'
    empty = attach_shares(sample, default_weights('nfl'))
    assert empty.rush_td_share.sum() == empty.rec_td_share.sum() == 0


def test_game_detail_reads_exact_published_probability_and_quote(monkeypatch):
    from app.queries import published_game_props
    from spe.props import board_cache
    game = SimpleNamespace(game_id='g', league_id='fbs', kickoff_utc=datetime.now(UTC) + timedelta(days=1))
    row = {'game_id':'g','forecast':{'p_estimate':.43,'model_version':'desk-v2','data_cutoff':'snapshot'},
           'book_price':200,'desk_selected':True}
    monkeypatch.setattr(board_cache, 'load_slate_props', lambda *a: [row, {'game_id':'other'}])
    assert published_game_props(None, game) == [row]


def test_started_game_uses_recorded_call(monkeypatch):
    from app import queries
    game = SimpleNamespace(game_id='g', league_id='fbs', kickoff_utc=datetime.now(UTC)-timedelta(hours=1))
    frozen = {'game_id':'g','locked':True,'forecast':{'p_estimate':.38}}
    monkeypatch.setattr(queries, 'list_called_scorers', lambda *a: [frozen])
    assert queries.published_game_props(None, game) == [frozen]


def test_holdout_labels_cannot_select_role_adjustments(tmp_path, monkeypatch):
    from spe.shadow import atd_role_calibration as roles
    monkeypatch.setattr(roles, 'artifact_path', lambda: tmp_path / 'role.json')
    n = 500
    frame = pd.DataFrame({'game_id':[f'g{i}' for i in range(n)],
        'kickoff_utc':pd.date_range('2024-01-01',periods=n,freq='D',tz='UTC'),
        'position_group':'RB','prior_carries':15,'p_blend':.2,'y':np.tile([0,1],n//2)})
    a = roles.fit_role_calibration(frame)
    frame.loc[350:, 'y'] = 0
    b = roles.fit_role_calibration(frame)
    assert a['logit_shifts'] == b['logit_shifts']
    assert b['holdout']['brier_after'] > b['holdout']['brier_before']


def test_out_player_probability_zero_even_without_active_probability():
    team, p = frames()
    fit, cal, test, w, meta = fit_upstream(team, p.iloc[:90], p.iloc[90:120], p.iloc[120:], 'nfl', p.kickoff_utc.iloc[90])
    model = fit_player_td_model(fit, 'nfl', meta['team_fit_cutoff'], w)
    test['availability_status'] = 'out'
    result = predict_frame(model, test)
    assert (result.p_atd == 0).all()
    assert (result.uncertainty_status == 'unvalidated').all()


def test_anytime_identity_can_be_scored_without_a_legacy_forecast(db):
    from test_publication_pipeline import seed
    from spe.db.models import Player, Market, Outcome
    from app.queries import prop_row
    game = seed(db)
    game.kickoff_utc = datetime.now(UTC) + timedelta(hours=2)
    db.add(Player(player_id='p', name='New starter', position_group='RB', position='RB'))
    db.add(Market(market_id='anytime_td', sport='football', market_type='player_yesno', period='game', stat='td', grading_rule='yes'))
    db.flush()
    outcome = Outcome(outcome_id='new', game_id='g', player_id='p', market_id='anytime_td', side='yes', line=.5, description='New starter')
    db.add(outcome)
    db.flush()
    r = prop_row(db, outcome, roster_index={'p':SimpleNamespace(team_id='a')})
    assert r is not None
    assert r['forecast']['p_estimate'] is None
    assert r['forecast']['desk_requires_opportunity_score']
    assert r['team_id'] == 'a' and r['opponent_id'] == 'b'


def test_training_runner_never_loads_current_upstream_model(db, monkeypatch):
    from spe.models.player_td import train as runner
    team, p = frames()
    p['season_year'] = pd.to_datetime(p.kickoff_utc).dt.year
    p['prior_td_rate'] = .3
    copies = []
    for i in range(5):
        part = p.copy()
        part['player_id'] = f'p{i}'
        copies.append(part)
    p = pd.concat(copies, ignore_index=True).sort_values('kickoff_utc')
    def base(*args, **kwargs):
        assert kwargs['attach_fitted'] is False
        return p, default_weights('nfl')
    monkeypatch.setattr(runner, 'build_player_td_frame', base)
    monkeypatch.setattr(runner, 'build_team_td_frame', lambda *a:team)
    end = p.kickoff_utc.max() + pd.Timedelta(days=7)
    monkeypatch.setattr(runner, 'training_cutoff', lambda *a, **k:{'leagues':{'nfl':{'train_cutoff':end.isoformat()}}})
    boundary = team.kickoff_utc.iloc[120].to_pydatetime()
    monkeypatch.setattr(runner, 'EVAL_WINDOWS', [('synthetic',boundary,boundary,end.to_pydatetime())])
    result = runner.train_player_td_models(db, leagues=('nfl',), now=end.to_pydatetime(), persist=True)
    league = result['leagues']['nfl']
    assert league['status'] == 'ok'
    assert league['windows'][0]['upstream']['calibration_n'] > 0
    assert league['windows'][0]['metrics']['n'] > 0
    assert result['m3_m4_untouched']
    saved = runner.load_player_td_model('nfl')
    assert saved.upstream_team_model == league['upstream']['team_model']


def test_current_injury_overrides_stale_forecast_and_future_report_is_ignored(db, monkeypatch):
    from test_publication_pipeline import seed
    from test_desk_opportunity import history_row, model, td_row
    from spe.models.player_td import desk_live as live
    from spe.db.models import InjuryEvent, Player
    seed(db)
    db.add(Player(player_id='p',name='Player',position_group='RB',position='RB'))
    db.flush()
    now = datetime(2026,9,27,18,tzinfo=UTC)
    for i, (stamp, status) in enumerate([(now-timedelta(hours=1),'out'),(now+timedelta(hours=1),'active')]):
        db.add(InjuryEvent(game_id='g', player_id='p', designation=status, reported_at=stamp,
            source='test',confidence=1,provider='test',provider_record_id=str(i),observed_at=stamp,effective_at=stamp))
    db.flush()
    monkeypatch.setattr(live,'load_model',lambda *a,**k:model())
    monkeypatch.setattr(live,'load_history',lambda *a:[history_row(week=i) for i in (1,2,3)])
    r = td_row()
    r['kickoff_utc'] = '2026-09-28T18:00:00Z'
    r['price_freshness']['observed_at'] = now.isoformat()
    live.enrich_desk_rows(db,[r],now=now)
    assert r['forecast']['availability_status'] == 'out'
    assert r['forecast']['p_estimate'] == 0
    assert not r['desk_selected']
