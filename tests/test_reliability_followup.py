"""Public reads stay bounded and preserve the corrected record."""
import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Event

import httpx
from sqlalchemy import event, text

from spe.db.models import BoardSnapshot, Game, PublishedPick, PublishedResult, PublishedCorrection, PublishedStat


def seed_records(db, count=35):
    from test_publication_pipeline import seed
    from spe.models.player_td.desk_replay import day_card_key
    game = seed(db)
    db.delete(game)
    db.flush()
    start = datetime(2025, 12, 20, 3, tzinfo=UTC)
    for i in range(count):
        kick = start + timedelta(days=i)
        from app.queries import kickoff_day
        day, gid = kickoff_day(kick), f"day-{i}"
        db.add(Game(game_id=gid, season_id="season", league_id="nfl", home_team_id="a", away_team_id="b",
                    venue_id="v", kickoff_utc=kick, data_tier="historical", status="final"))
        db.flush()
        db.add(PublishedPick(pick_id=gid, game_id=gid, league_id="nfl", market_id="receptions",
                            published_at=kick-timedelta(hours=2), kickoff_utc=kick,
                            snapshot={"game_id":gid, "market_id":"receptions", "league_id":"nfl",
                                      "player_name":"Receiver", "book_price":110}))
        db.flush()
        db.add(PublishedResult(pick_id=gid, result="win", actual=6, units=1.1, settled_at=kick+timedelta(hours=4), evidence={}))
        if i % 2 == 0:
            db.add(PublishedCorrection(pick_id=gid, result="loss", actual=0, units=-1,
                                       settled_at=kick+timedelta(hours=5), evidence={}))
        row = {"game_id":gid, "league_id":"nfl", "player_id":"main", "player_name":"Main",
               "kickoff_utc":kick.isoformat(), "game_status":"final", "retrospective":i % 5 == 0,
               "decision":{"most_likely":{"player_id":"main", "price_supported":False},
                           "value_candidate":{"player_id":"alt", "name":"Alternate", "american":155, "price_supported":True}}}
        for league in ("all", "nfl"):
            db.add(BoardSnapshot(key=day_card_key(day,league), payload={"rows":[deepcopy(row)]}, updated_at=kick))
        for player, actual in (("main",1), ("alt",0)):
            source = "espn.summary.confirmed_inactive" if i % 7 == 0 else "espn.summary:test"
            db.add(PublishedStat(key=f"{gid}|{player}|anytime_td", game_id=gid, player_id=player,
                                 market_id="anytime_td", actual=actual, source=source, observed_at=kick))
    db.commit()


def test_record_range_matches_every_daily_card_and_has_bounded_queries(db):
    from spe.publishing import ledger
    seed_records(db)
    original = {s.key:deepcopy(s.payload) for s in db.query(BoardSnapshot)}
    for league in ("all", "nfl", "fbs"):
        dates = ledger.slate_dates(db)
        expected = {day:ledger.ledger_day(db,day,league)["rows"] for day in dates}
        db.expire_all()
        queries = []
        def counted(*args): queries.append(args[2])
        event.listen(db.get_bind(), "before_cursor_execute", counted)
        try:
            actual = ledger._ledger_days(db,dates,league)
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", counted)
        assert actual == expected
        assert len(queries) <= 5, len(queries)
        for month in ("2025-12", "2026-01"):
            expected_units = {}
            for day, rows in expected.items():
                priced = [r for r in rows if r['units'] is not None and r['result'] in {'win','loss','push'}]
                if day.startswith(month) and priced:
                    expected_units[day] = round(sum(r['units'] for r in priced),4)
            assert ledger.ledger_calendar(db,month,league)['units'] == expected_units
        rows = [r for day, values in expected.items() if day.startswith('2026-') for r in values]
        year = ledger.ledger_year(db,2026,league)
        for key,value in ledger._ledger_summary(rows).items():
            assert year[key] == value
    assert {s.key:s.payload for s in db.query(BoardSnapshot)} == original
    # A correction written after one read is visible immediately on the next read.
    before = ledger.ledger_year(db,2026)['units']
    pick = db.get(PublishedPick, 'day-20')
    db.add(PublishedCorrection(pick_id=pick.pick_id, result='win',actual=7,units=1.1,
                              settled_at=pick.kickoff_utc+timedelta(days=3),evidence={}))
    db.commit()
    assert ledger.ledger_year(db,2026)['units'] == round(before+2.1,4)


def test_shared_boards_use_the_request_connection(db, monkeypatch):
    from spe.publishing import store
    from spe.props.board_cache import load_props, load_slate_props, WARM_MARKETS, entry_key
    from spe.models.ats.board_cache import load_board
    from spe.publishing.ledger import slate_dates
    from spe.config import settings
    from test_publication_pipeline import row
    from spe.publishing import policy
    monkeypatch.setattr(settings, 'app_env', 'production')
    now = datetime.now(UTC)
    annotate = policy.annotate
    monkeypatch.setattr(policy, 'annotate', lambda row: annotate(row, now=now))
    for market in WARM_MARKETS:
        store.write('props:'+entry_key(market,'nfl','2026-11-16'), {'rows':[row(market_id=market)]}, db)
    store.write('ats', {'rows':[{'game_id':'g','kickoff_utc':now.isoformat()}]}, db)
    db.commit()
    # Holding a connection must not cause a nested checkout, even for fallback ATS.
    db.execute(text('SELECT 1'))
    def forbidden(): raise AssertionError('Nested snapshot connection')
    monkeypatch.setattr(store, 'session_factory', forbidden)
    assert load_board(db, date='2026-11-16')['rows']
    assert slate_dates(db)
    expected = [r for m in WARM_MARKETS for r in load_props(db,m,None,None,'nfl','2026-11-16')]
    count = []
    def counted(*args): count.append(args[2])
    event.listen(db.get_bind(), 'before_cursor_execute', counted)
    try:
        actual = load_slate_props(db,'nfl','2026-11-16')
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', counted)
    assert actual == expected
    assert len(count) == 1


def test_production_alias_does_not_build_an_empty_ats_board(db, monkeypatch):
    from spe.models.ats import board_cache
    from spe.config import settings
    monkeypatch.setattr(settings, 'app_env', 'production')
    def forbidden(*args,**kwargs): raise AssertionError('Production request built a model')
    monkeypatch.setattr(board_cache,'refresh_board',forbidden)
    assert board_cache.load_board(db)['degraded'] is True


def test_billing_webhook_does_not_block_other_requests(monkeypatch):
    from fastapi import FastAPI
    from app import auth_routes
    from app.deps import db_session
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.dependency_overrides[db_session] = lambda: None
    @app.get('/probe')
    async def probe(): return {'ok':True}
    entered, release = Event(), Event()
    monkeypatch.setattr(auth_routes, 'construct_event', lambda *args:{'type':'test'})
    def slow_billing(*args):
        entered.set()
        release.wait(2)
    monkeypatch.setattr(auth_routes,'apply_stripe_event',slow_billing)
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
            pending = asyncio.create_task(client.post('/v1/billing/webhook',content=b'{}'))
            try:
                assert await asyncio.to_thread(entered.wait,1)
                assert not pending.done()
                response = await asyncio.wait_for(client.get('/probe'),1)
                assert response.json() == {'ok':True}
            finally:
                release.set()
            assert (await pending).status_code == 200
    asyncio.run(check())


def test_ats_record_filters_preserve_first_eligible_freeze(db):
    from test_publication_pipeline import seed
    from spe.db.models import AtsPrediction, AtsSettlement
    from spe.db.base import as_utc
    from spe.models.ats.research_bundle import CLEAN_PROSPECTIVE_COHORT
    from spe.publishing.ledger import ats_history, ledger_day, _ledger_days
    from app.queries import kickoff_day
    game = seed(db)
    kick = as_utc(game.kickoff_utc)
    common = dict(game_id=game.game_id,league_id='nfl',home_team_id='a',away_team_id='b',side='home',
                  spread_line=-3,price_american=-110,predicted_margin=5,predicted_home_score=28,
                  predicted_away_score=23,p_cover=.6,margin_sigma=13,model_version_id='test',
                  components={'eligibility':'clean_prospective','cohort_version':CLEAN_PROSPECTIVE_COHORT})
    for ident, changes in [
        ('legacy',{'components':{}}), ('unpriced',{'price_american':None}),
        ('bad-price',{'price_american':-50}), ('late',{'predicted_at':kick+timedelta(seconds=1)}),
        ('first',{}), ('later',{}),
    ]:
        when = kick-timedelta(minutes=10 if ident=='later' else 20)
        db.add(AtsPrediction(**{**common,'ats_id':ident,'predicted_at':when,**changes}))
    db.flush()
    db.add(AtsSettlement(ats_id='first',result='cover',settled_at=kick+timedelta(hours=4),official_source='test'))
    db.commit()
    history = ats_history(db)
    assert [r['pick_id'] for r in history['rows']] == ['first']
    assert history['rows'][0]['result'] == 'win'
    day = kickoff_day(kick)
    assert _ledger_days(db,[day],'all')[day] == ledger_day(db,day)['rows']
