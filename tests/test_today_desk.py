from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from spe.publishing.desk import build, changes_for, selection_key
from spe.publishing.ledger import calibration_summary
from spe.publishing.policy import assess
from test_publication_pipeline import NOW, row, seed


def test_selected_book_reprices_value_and_never_borrows_another_books_price():
    original = row(offers=[
        {"bookmaker": "A", "book_price": 110, "price_freshness": row()["price_freshness"]},
        {"bookmaker": "B", "book_price": -200, "price_freshness": row()["price_freshness"]},
    ])
    best = build([original], now=NOW)
    assert best["rows"][0]["book"] == "A"
    assert best["qualified"] == 1
    other = build([original], now=NOW, book="B")
    assert other["rows"][0]["price"] == -200
    assert other["rows"][0]["break_even"] == pytest.approx(2 / 3)
    assert other["qualified"] == 0
    assert other["rows"][0]["conservative_ev"] < 0
    assert build([original], now=NOW, book="Unknown")["rows"] == []


def test_fresh_book_beats_a_better_but_expired_price():
    original = row(offers=[
        {"bookmaker": "Stale", "book_price": 200, "price_freshness": {"observed_at": (NOW - timedelta(hours=1)).isoformat()}},
        {"bookmaker": "Fresh", "book_price": 110, "price_freshness": row()["price_freshness"]},
    ])
    assert build([original], now=NOW)["rows"][0]["book"] == "Fresh"


def test_blocked_research_visible_without_promoting_it():
    original = row(forecast={"availability_status": "unknown"})
    result = build([original], now=NOW)
    assert result["total"] == 1 and result["qualified"] == 0
    assert "Player availability is not confirmed." in result["rows"][0]["risks"]
    assert build([original], now=NOW + timedelta(hours=2))["total"] == 0
    assert build([row(game_status="final")], now=NOW)["total"] == 0


def test_filter_pagination_and_saved_ids_do_not_change_totals():
    rows = [row(player_id=f"p{i}", player_name=f"Runner {i}", outcome_id=f"o{i}") for i in range(4)]
    result = build(rows, now=NOW, limit=2, offset=2)
    assert result["total"] == 4 and len(result["rows"]) == 2
    assert build(rows, now=NOW, search="RUNNER 3")["total"] == 1
    assert build(rows, now=NOW, saved={selection_key(rows[1])})["rows"][0]["player"] == "Runner 1"
    assert build(rows, now=NOW, saved=set())["total"] == 0


def test_changes_compare_like_for_like_and_expire():
    original = row()
    changed = row(book_price=120)
    changes = changes_for(changed, original, NOW)
    assert changes[0]["kind"] == "price" and changes[0]["before"] == 110
    assert changes_for(row(bookmaker="Different", book_price=120), original, NOW) == []
    assert changes_for(changed, {**changed, "changes": changes}, NOW + timedelta(minutes=5)) == changes
    assert changes_for(changed, {**changed, "changes": changes}, NOW + timedelta(days=2)) == []
    assert changes_for(changed, None, NOW) == []


def test_quote_expires_at_transition_to_near_kickoff():
    original = row(kickoff_utc=(NOW + timedelta(hours=2, minutes=5)).isoformat(),
                   price_freshness={"observed_at": (NOW - timedelta(minutes=20)).isoformat()})
    check = assess(original, NOW)
    assert check["eligible"]
    assert check["valid_until"] == (NOW + timedelta(minutes=5)).isoformat()
    assert not assess(original, NOW + timedelta(minutes=5))["eligible"]


def test_accuracy_scores_real_frozen_probabilities():
    result = calibration_summary([(.6, 1), (.4, 0)])
    assert result["brier_score"] == pytest.approx(.16)
    assert result["actual_hit_rate"] == .5
    assert result["predicted_hit_rate"] == .5
    assert calibration_summary([])["brier_score"] is None


def test_desk_route_reads_only_snapshots_and_removes_canceled_games(db):
    from app.main import app
    from app.deps import db_session
    from app.queries import kickoff_day
    from spe.db.models import Game
    from spe.props.board_cache import write_entry, WARM_MARKETS

    now = datetime.now(UTC)
    game = seed(db)
    game.kickoff_utc = now + timedelta(hours=1)
    db.commit()
    date = kickoff_day(game.kickoff_utc)
    original = row(kickoff_utc=game.kickoff_utc.isoformat(),
                   forecast={"prediction_as_of": now.isoformat(), "data_cutoff": now.isoformat()},
                   price_freshness={"observed_at": now.isoformat()})
    app.dependency_overrides[db_session] = lambda: db
    try:
        client = TestClient(app)
        assert client.get(f"/v1/desk?date={date}&league=nfl").json()["status"] == "warming"
        for market in WARM_MARKETS:
            write_entry(market, "nfl", date, [original] if market == "rushing_yards" else [])
        result = client.get(f"/v1/desk?date={date}&league=nfl").json()
        assert result["total"] == 1 and result["status"] == "ready"
        assert client.get("/v1/desk?date=garbage").status_code == 422
        assert client.get("/v1/desk?league=invalid").status_code == 422
        game.status = "canceled"
        db.commit()
        assert client.get(f"/v1/desk?date={date}&league=nfl").json()["rows"] == []
    finally:
        app.dependency_overrides.clear()


def test_bulk_reader_is_bounded_and_does_not_leak_cache(db):
    from app.board_batch import board_batch
    from app.queries import latest_forecast, latest_tick
    from spe.db.models import Market, Outcome, Player

    seed(db)
    db.add(Market(market_id="rushing_yards", sport="football", market_type="player_over_under", period="game", stat="rush_yards", grading_rule="over"))
    db.add(Player(player_id="p", name="Player", position_group="RB", position="RB"))
    db.flush()
    outcomes = [Outcome(outcome_id=f"o{i}", game_id="g", player_id="p", market_id="rushing_yards", side="over", line=40.5 + i, description="Player") for i in range(40)]
    db.add_all(outcomes)
    db.commit()
    calls = []
    def count(*args): calls.append(1)
    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        with board_batch(db, outcomes):
            for outcome in outcomes:
                assert latest_forecast(db, outcome.outcome_id) is None
                assert latest_tick(db, outcome.outcome_id) is None
            assert len(calls) <= 10
        assert "board_batch" not in db.info
        with pytest.raises(RuntimeError):
            with board_batch(db, outcomes):
                raise RuntimeError("intentional")
        assert "board_batch" not in db.info
    finally:
        event.remove(engine, "before_cursor_execute", count)


def test_batch_latest_forecast_and_best_fresh_book(db):
    from app.board_batch import board_batch
    from app.queries import latest_forecast, latest_tick, forecast_payload
    from spe.db.models import Market, Outcome, Player, Bookmaker, OddsTick, Forecast, ModelVersion

    game = seed(db)
    now = datetime.now(UTC)
    game.kickoff_utc = now + timedelta(hours=1)
    db.add(Market(market_id="rushing_yards", sport="football", market_type="player_over_under", period="game", stat="rush_yards", grading_rule="over"))
    db.add(Player(player_id="p", name="Player", position_group="RB", position="RB"))
    db.add_all([Bookmaker(bookmaker_id=b, name=b) for b in ("A", "B")])
    db.add(ModelVersion(model_version_id="model", code_sha="test", training_cutoff=now - timedelta(days=1), data_snapshot="test", hyperparameters={}, calibration_version="test", league_id="nfl", family="test"))
    db.flush()
    outcome = Outcome(outcome_id="o", game_id="g", player_id="p", market_id="rushing_yards", side="over", line=40.5, description="Player")
    db.add(outcome)
    db.flush()
    for i, (book, price, age) in enumerate((("A", 500, 25), ("A", 110, 1), ("B", 200, 30))):
        stamp = now - timedelta(minutes=age)
        db.add(OddsTick(tick_id=f"t{i}", outcome_id="o", bookmaker_id=book, price_american=price, captured_at=stamp, source_at=stamp, observed_at=stamp, effective_at=stamp, provider="test", provider_record_id=f"t{i}"))
    for i, codes in enumerate(([], ["IDENTITY_CONFLICT"])):
        db.add(Forecast(forecast_id=f"f{i}", outcome_id="o", as_of=now - timedelta(seconds=2-i), model_version_id="model", p=.6, p_low=.55, p_high=.65, sim_count=0, data_cutoff=now - timedelta(days=1), availability_status="active", reason_codes=codes, components={}, data_tier="research"))
    db.commit()
    with board_batch(db, [outcome]):
        forecast, tick = latest_forecast(db, "o"), latest_tick(db, "o")
        assert forecast.forecast_id == "f0"
        assert tick.tick_id == "t1"
        assert forecast_payload(db, forecast, tick)["book_price"] == 110
