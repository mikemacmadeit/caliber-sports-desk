from datetime import UTC, datetime, timedelta
from pathlib import Path
import time

from fastapi.testclient import TestClient

from spe.config import settings
from spe.db.models import BoardSnapshot, DeskDecision, Player, Market, Outcome, Bookmaker, OddsTick, RosterWeek
from spe.models.player_td import desk_opportunity
from spe.publishing.local_board import ensure_local_board, scoring_signature
from spe.publishing.store import write
from test_publication_pipeline import seed

BUNDLED = Path(__file__).resolve().parents[1] / 'deploy/runtime-data/parquet/models'


def test_local_loads_shipped_weights_without_copying_them(db, monkeypatch):
    monkeypatch.setattr(desk_opportunity, 'BUNDLED_MODEL_DIR', BUNDLED)
    for league in ('nfl','fbs'):
        assert not desk_opportunity.artifact_path(league).exists()
        loaded = desk_opportunity.load_model(league)
        assert loaded and loaded['version'] == desk_opportunity.VERSION
        assert loaded['artifact_sha256']


def test_present_invalid_custom_weights_do_not_silently_use_shipped_weights(db, monkeypatch):
    monkeypatch.setattr(desk_opportunity, 'BUNDLED_MODEL_DIR', BUNDLED)
    path=desk_opportunity.artifact_path('fbs')
    path.parent.mkdir(parents=True)
    path.write_text('{"research_approved":false}')
    assert desk_opportunity.load_model('fbs') is None


def test_freeze_and_snapshot_share_one_atomic_sqlite_transaction(db):
    seed(db)
    db.add(DeskDecision(game_id='g',decided_at=datetime.now(UTC),payload={'row':None}))
    db.flush()  # This connection now holds SQLite's writer lock.
    write('test-atomic', {'rows':[{'game_id':'g'}]}, db=db)
    assert db.get(BoardSnapshot,'test-atomic') is not None
    db.rollback()
    assert db.get(BoardSnapshot,'test-atomic') is None
    assert db.query(DeskDecision).count() == 0


def test_today_route_rebuilds_old_empty_board_with_real_scorer(db, monkeypatch):
    from app.main import app
    from app.deps import db_session
    from app.queries import kickoff_day
    from spe.models.player_td import desk_live
    from spe.props.board_cache import WARM_MARKETS, write_entry
    from test_desk_opportunity import history_row
    monkeypatch.setattr(settings,'app_env','dev')
    monkeypatch.setattr(desk_opportunity,'BUNDLED_MODEL_DIR',BUNDLED)
    now=datetime.now(UTC)
    game=seed(db)
    game.kickoff_utc=now+timedelta(hours=8)
    db.add(Player(player_id='p',name='Test runner',position_group='RB',position='RB'))
    db.add(Market(market_id='anytime_td',sport='football',market_type='player_yesno',period='game',stat='td',grading_rule='yes'))
    db.add(Bookmaker(bookmaker_id='book',name='Book'))
    db.flush()
    db.add(RosterWeek(player_id='p',team_id='a',season_year=2026,week=1,status='ACT',
        provider='test',provider_record_id='roster',observed_at=now,effective_at=now))
    db.add(Outcome(outcome_id='o',game_id='g',player_id='p',market_id='anytime_td',side='yes',line=.5,description='Test runner'))
    db.flush()
    db.add(OddsTick(outcome_id='o',bookmaker_id='book',price_american=500,captured_at=now,source_at=now,
        provider='test',provider_record_id='quote',observed_at=now,effective_at=now))
    db.commit()
    history=[history_row(week=i, team_id='a',home_team_id='a',away_team_id='b',
        kickoff_utc=(now-timedelta(days=(4-i)*7)).isoformat()) for i in (1,2,3)]
    monkeypatch.setattr(desk_live,'load_history',lambda *a:history)
    day=kickoff_day(game.kickoff_utc)
    for market in WARM_MARKETS:write_entry(market,'nfl',day,[])
    app.dependency_overrides[db_session]=lambda:db
    try:
        client=TestClient(app)
        url=f'/v1/desk?league=nfl&date={day}&market=anytime_td'
        first=client.get(url).json()
        assert first['refresh_status']=='running'
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            result=client.get(url).json()
            if result['refresh_status']=='ready':break
            assert result['refresh_status']!='failed'
            time.sleep(.05)
        assert result['refresh_status']=='ready'
        assert result['total']==1
        assert result['rows'][0]['model'].startswith('desk_opportunity_v2-nfl-')
        assert result['market_counts']['anytime_td']==1
        assert db.query(DeskDecision).count()==1
        cached=db.get(BoardSnapshot,'props:anytime_td|nfl|'+day)
        db.refresh(cached)
        assert cached.payload['scoring_signature']==scoring_signature()
        stored = next(r for r in cached.payload['rows'] if r['market_id'] == 'anytime_td')
        assert stored['forecast']['p_estimate'] == result['rows'][0]['probability']
    finally:app.dependency_overrides.clear()


def test_production_api_never_starts_a_local_worker(db, monkeypatch):
    monkeypatch.setattr(settings,'app_env','prod')
    assert ensure_local_board(db,'2026-11-14')=='worker_managed'
    assert db.query(BoardSnapshot).count()==0
