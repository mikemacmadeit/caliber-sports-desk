from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from spe.db.base import as_utc
from spe.models.player_td.scouting_report import build_report, capture_matchup, capture_personnel, price_case
from spe.models.player_td.desk_evidence import EvidenceHistory, current_context
from spe.models.player_td.desk_replay import select_game_scorer

CUTOFF = as_utc('2024-10-02T12:00:00Z')


def candidate(pid='pick', p=.65, team='BUF', **kwargs):
    return {'game_id':'g', 'player_id':pid, 'player_name':pid, 'position':'RB', 'team':team,
        'home_abbr':'BUF', 'opponent':'NYJ', 'league_id':'nfl', 'book_price':-150,
        'forecast':{'p_estimate':p, 'data_cutoff':CUTOFF.isoformat()},
        'desk_analysis':{'features':{'games':6, 'carries':15, 'receptions':2},
            'combination':{'rush_lambda':.8, 'rec_lambda':.1}, 'current_context':{}}, **kwargs}


def test_report_never_uses_outcome_or_changes_probability_and_alternatives_obey_eligibility():
    pick = candidate()
    backup = candidate('backup', .99, position='QB')
    backup['desk_analysis']['current_context']['depth'] = {'rank':2}
    inactive = candidate('out', .9)
    inactive['desk_analysis']['current_context']['availability'] = {'status':'out'}
    rows = [pick, backup, inactive, candidate('teammate', .6), candidate('opponent', .55, 'NYJ'),
            candidate('cheap', .4, book_price=250), candidate('other_game', .9, game_id='other')]
    before = deepcopy(rows)
    report = build_report(pick, rows)
    assert rows == before
    assert {r['name'] for r in report['alternatives']} == {'teammate','opponent','cheap'}
    pick.update(result='blank', actual=0, final_score='0-50', game_status='final')
    for r in rows[1:]:
        r.update(result='scored', actual=3)
    assert build_report(pick, rows) == report
    assert select_game_scorer(rows[:4])['player_id'] == 'pick'
    assert any('not targets' in g for g in report['gaps'])
    assert any('active status is unconfirmed' in s for s in report['checks'])


def test_a_player_under_forty_percent_is_not_named():
    assert select_game_scorer([candidate("longshot", p=.34, team_implied_points=28)]) is None
    kept = select_game_scorer([
        candidate("longshot", p=.34, team_implied_points=28),
        candidate("real", p=.46, team_implied_points=28),
    ])
    assert kept["player_id"] == "real"


def test_a_blowout_dog_is_not_the_scorer_when_the_favorite_has_a_player():
    dog = candidate('roberts', p=.55, team='MIZ', team_implied_points=16.0)
    favorite = candidate('frazier', p=.40, team='UGA', team_implied_points=32.5)
    assert select_game_scorer([dog, favorite])['player_id'] == 'frazier'
    assert select_game_scorer([dog])['player_id'] == 'roberts'
    close = [candidate('dog', p=.6, team_implied_points=18), candidate('other', p=.4, team_implied_points=19)]
    assert select_game_scorer(close)['player_id'] == 'dog'


def test_price_value_is_separate_from_scorer_ranking():
    expensive = candidate(p=.7, book_price=-300)
    cheap = candidate('cheap', p=.55, book_price=150)
    assert select_game_scorer([expensive, cheap])['player_id'] == 'pick'
    assert price_case(expensive)['estimated_edge'] == pytest.approx(-.05)
    assert price_case(cheap)['estimated_edge'] == pytest.approx(.15)
    assert price_case(candidate(book_price=None))['break_even'] is None
    assert price_case(candidate(p=float('nan')))['fair_american'] is None


def test_team_total_policy_can_bypass_higher_probability_and_report_says_so():
    from spe.models.player_td.scouting_report import selection_context
    higher = candidate('low_total', .8, 'NYJ', team_implied_points=18)
    lower = candidate('preferred_team', .6, team_implied_points=28)
    chosen = select_game_scorer([higher, lower])
    assert chosen['player_id'] == 'preferred_team'
    report = build_report(chosen, [higher,lower])
    assert 'bypass a higher individual' in report['comparison']
    assert report['alternatives'][0]['policy_excluded']
    # Older reports can gain the correct policy wording without changing the snapshot.
    before = deepcopy(report)
    assert selection_context(report,chosen) == report
    assert report == before


def test_benchmarks_exclude_future_games_unobserved_data_and_tiny_samples():
    rows = []
    for i in range(8):
        for day in (7,14):
            rows.append({'team_id':'offense', 'opponent_id':f'd{i}', 'game_id':f'{i}-{day}',
                'kickoff_utc':(CUTOFF-timedelta(days=day)).isoformat(),
                'observed_at':(CUTOFF-timedelta(days=day-2)).isoformat(), 'rush_epa':i/10, 'pass_epa':i/20})
    base = SimpleNamespace(evidence=EvidenceHistory({'teams':rows}))
    expected = capture_matchup(base, 'offense', 'd7', CUTOFF)
    assert len(expected['benchmarks']) == 2
    assert expected['benchmarks'][0]['allowed_percentile'] == pytest.approx(7.5/8)
    rows += [{**rows[-1], 'game_id':'future', 'kickoff_utc':CUTOFF.isoformat(), 'rush_epa':-1000},
             {**rows[-1], 'game_id':'observed_later', 'observed_at':(CUTOFF+timedelta(seconds=1)).isoformat(), 'rush_epa':1000}]
    base.evidence = EvidenceHistory({'teams':rows})
    assert capture_matchup(base, 'offense', 'd7', CUTOFF) == expected
    assert capture_matchup(base, 'offense', 'missing', CUTOFF)['benchmarks'] == []


def test_nfl_depth_and_ngs_use_correct_team_and_observation_cutoff(db):
    from test_free_sources import _game
    from spe.db.models import DepthChartWeek, NgsWeek
    _game(db)
    pid = 'nfl:00-0034796'
    def depth(week, team='nfl:BUF', rank=1, age=1, observed=None, quality='valid'):
        return DepthChartWeek(season_year=2024, week=week, team_id=team, player_id=pid,
            position='QB', formation='offense', depth_position='QB', depth_rank=rank,
            effective_at=CUTOFF-timedelta(days=age), observed_at=observed or CUTOFF-timedelta(hours=1),
            provider='nflverse', provider_record_id=str(week), quality_status=quality)
    db.add_all([depth(1,rank=2), depth(2,team='nfl:NYJ',age=.2),
        depth(3,age=.1,observed=CUTOFF+timedelta(hours=1)), depth(4,age=8), depth(5,age=.3,quality='quarantined')])
    for week, observed, value in ((1,CUTOFF-timedelta(hours=1),.5),(2,CUTOFF+timedelta(hours=1),9.)):
        db.add(NgsWeek(season_year=2024,week=week,player_id=pid,team_id='nfl:BUF',stat_type='rushing',
            metrics={'rush_yards_over_expected_per_att':value}, provider='nflverse',provider_record_id=f'ngs{week}',
            effective_at=CUTOFF-timedelta(days=3-week), observed_at=observed))
    db.flush()
    context = current_context(db, 'g', pid, CUTOFF, team_id='nfl:BUF')
    from spe.models.player_td.nflverse_context import supplemental_context
    batch = supplemental_context(db, [pid], CUTOFF, team_id='nfl:BUF')
    assert current_context(db, 'g', pid, CUTOFF, team_id='nfl:BUF', nfl_supplement=batch[pid]) == context
    assert context['depth']['rank'] == 2
    assert context['depth']['team_id'] == 'nfl:BUF'
    assert context['nflverse_context']['rushing']['metrics']['rush_yards_over_expected_per_att'] == .5
    qb = candidate('qb', .99, position='QB')
    qb['desk_analysis']['current_context'] = context
    assert select_game_scorer([qb,candidate()])['player_id'] == 'pick'
    assert current_context(db, 'g', pid, CUTOFF, team_id='cfb:BUF')['depth'] is None


def test_unmatched_odds_player_does_not_crash_context_refresh(db):
    context = current_context(db, 'g', None, CUTOFF, team_id='nfl:BUF', nfl_supplement={})
    assert context['personnel_alerts'] == []
    assert context['availability'] is None and context['depth'] is None


def test_finished_saved_pick_explanation_does_not_rewrite_snapshot(db, monkeypatch):
    from test_publication_pipeline import seed, NOW
    from test_desk_combined import combined_model
    from spe.db.models import BoardSnapshot
    import spe.models.player_td.desk_replay as replay
    game = seed(db); game.kickoff_utc=NOW-timedelta(hours=5)
    row = candidate(game_id=game.game_id, kickoff_utc=as_utc(game.kickoff_utc).isoformat())
    payload = {'row':row,'evaluated':True}
    original = deepcopy(payload)
    saved = BoardSnapshot(key='scorer-desk-v1:'+game.game_id,payload=payload,updated_at=NOW)
    db.add(saved);db.flush()
    monkeypatch.setattr(replay,'load_combined_model',lambda *a,**k:combined_model())
    monkeypatch.setattr(replay,'load_history',lambda *a,**k:pytest.fail('Frozen pick must not read newer history'))
    report = replay.replay_day(db,'2026-11-15',now=NOW)['rows'][0]['desk_analysis']['scouting_report']
    assert report['frozen_inputs_only'] and not report['alternatives']
    assert saved.payload == original


def test_vacated_work_requires_complete_prior_team_boxes_or_measured_appearances():
    class History:
        teams={'t':[{'game_id':'1','complete_player_box':True},{'game_id':'2','complete_player_box':True}]}
        players={('p','t'):[{'game_id':'1','carries':10,'receptions':2}]}
        recent=staticmethod(lambda rows, cutoff: rows)
    h=History()
    players={'p':SimpleNamespace(name='Runner')}
    facts=capture_personnel(h,'t',CUTOFF,{'p':'out'},players)
    assert facts[0]['carries']==5  # Complete second box supports zero recorded work.
    assert capture_personnel(h,'t',CUTOFF,{'p':'questionable'},players)==[]
    h.teams={'t':[{'game_id':'1'},{'game_id':'2'}]}
    assert capture_personnel(h,'t',CUTOFF,{'p':'out'},players)==[]


def test_cached_upcoming_board_skips_model_run_but_checks_new_inactive_report(db, monkeypatch):
    from test_publication_pipeline import seed, NOW
    from test_desk_combined import combined_model
    from spe.db.models import BoardSnapshot, InjuryEvent, Player
    import spe.models.player_td.desk_replay as replay
    game=seed(db);game.kickoff_utc=NOW+timedelta(hours=2)
    db.add(Player(player_id='pick',name='Runner',position_group='RB',position='RB'))
    db.flush()
    row=candidate(game_id=game.game_id,kickoff_utc=as_utc(game.kickoff_utc).isoformat())
    payload={'row':row,'evaluated':True};original=deepcopy(payload)
    saved=BoardSnapshot(key='scorer-desk-v1:'+game.game_id,payload=payload,updated_at=NOW-timedelta(minutes=5))
    db.add(saved);db.flush()
    monkeypatch.setattr(replay,'load_combined_model',lambda *a,**k:combined_model())
    monkeypatch.setattr(replay,'load_history',lambda *a,**k:pytest.fail('Cached response must not wait on prediction'))
    first=replay.replay_day(db,'2026-11-15',now=NOW,cached_only=True)
    assert first['rows'][0]['player_id']=='pick' and saved.payload==original
    db.add(InjuryEvent(game_id=game.game_id,player_id='pick',designation='out',confidence=1,
        source='official',reported_at=NOW,observed_at=NOW,effective_at=NOW,provider='test',provider_record_id='new-out'))
    db.flush()
    second=replay.replay_day(db,'2026-11-15',now=NOW,cached_only=True)
    assert not second['rows'] and 'withheld' in second['unavailable_games'][0]['reason']
    assert saved.payload==original


def test_cached_board_reports_pending_without_doing_first_prediction(db, monkeypatch):
    from test_publication_pipeline import seed, NOW
    from test_desk_combined import combined_model
    import spe.models.player_td.desk_replay as replay
    game=seed(db);game.kickoff_utc=NOW+timedelta(hours=2);db.flush()
    monkeypatch.setattr(replay,'load_combined_model',lambda *a,**k:combined_model())
    monkeypatch.setattr(replay,'load_history',lambda *a,**k:pytest.fail('First prediction must run in the worker'))
    result=replay.replay_day(db,'2026-11-15',now=NOW,cached_only=True)
    assert not result['rows'] and 'update queued' in result['unavailable_games'][0]['reason']


def test_scorer_refresh_coalesces_requests_and_keeps_failed_state(monkeypatch):
    import spe.models.player_td.scorer_refresh as refresh
    import spe.models.player_td.desk_replay as replay
    import spe.db.session as sessions
    monkeypatch.setattr('spe.ingest.nfl_availability.refresh_nfl_availability',lambda *a,**k:{'status':'ok'})
    jobs=[]
    class Thread:
        def __init__(self, *, target, **kwargs): self.target=target
        def start(self): jobs.append(self.target)
    class Session:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def commit(self):pass
    monkeypatch.setattr(refresh,'_states',{})
    monkeypatch.setattr(refresh.threading,'Thread',Thread)
    monkeypatch.setattr(sessions,'session_factory',Session)
    monkeypatch.setattr(replay,'replay_day',lambda *a: (_ for _ in ()).throw(RuntimeError('unavailable')))
    assert refresh.ensure_refresh('2026-11-15')=='running'
    assert refresh.ensure_refresh('2026-11-15')=='running' and len(jobs)==1
    jobs[0]()
    assert refresh.ensure_refresh('2026-11-15')=='failed' and len(jobs)==1
