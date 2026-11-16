from datetime import UTC, datetime, timedelta
from copy import deepcopy

import pandas as pd
import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session


def test_depth_file_filters_recent_rows_before_full_materialization(tmp_path, monkeypatch):
    from spe.ingest import nflverse_historical as ingest
    now = datetime(2026, 11, 16, tzinfo=UTC)
    frame = pd.DataFrame({"dt": ["2026-01-01T00:00:00Z"] * 17000 +
        ["2026-11-08T23:30:00-06:00", "2026-11-09T01:00:00+06:00", "invalid"],
        "gsis_id": [str(i) for i in range(17003)], "unused": ["x"] * 17003})
    frame.to_parquet(tmp_path / "depth_charts_2026.parquet")
    monkeypatch.setattr(ingest, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(ingest, "download_file", lambda *a, **k: None)
    result = ingest.load_asset("depth_charts", 2026, since=now-timedelta(days=7), columns=("dt", "gsis_id"))
    assert result.gsis_id.tolist() == ["17000"]
    assert list(result.columns) == ["dt", "gsis_id"]


def test_existing_nfl_players_do_not_query_once_per_row(db):
    from spe.db.models import Player, ProviderIdMap
    from spe.ingest.nflverse_historical import ingest_players
    for i in range(200):
        db.add(Player(player_id=f"nfl:{i}", name=f"Player {i}", position_group="RB", position="RB", aliases=[]))
        db.add(ProviderIdMap(provider="espn", entity_type="player", external_id=str(i), canonical_id=f"nfl:{i}", confidence=1))
    db.commit()
    statements = []
    def observe(conn, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(db.get_bind(), "before_cursor_execute", observe)
    try:
        result = ingest_players(db, pd.DataFrame([{"gsis_id": str(i), "display_name": f"Player {i}", "espn_id": str(i)}
            for i in range(200)]), observed_at=datetime.now(UTC))
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", observe)
    assert result == (0, 200)
    assert len([s for s in statements if s.startswith("SELECT")]) <= 5


def test_completed_nfl_sources_survive_later_worker_interruption(db, monkeypatch):
    from test_nflverse_desk import _game
    from test_nflverse_desk import play
    from spe.db.models import NflverseGameContext
    from spe.ingest import nflverse_desk as ingest
    _game(db)
    monkeypatch.setattr(ingest, "ingest_nfl_history", lambda *a, **k: {})
    def fetch(key, *a, **k):
        if key == "pbp":
            return pd.DataFrame([play()])
        raise KeyboardInterrupt("worker terminated")
    monkeypatch.setattr(ingest, "load_asset", fetch)
    with pytest.raises(KeyboardInterrupt):
        ingest.refresh_nflverse_desk(db, year=2024, now=datetime(2024, 10, 1, tzinfo=UTC), force=True)
    db.rollback()
    with Session(db.get_bind()) as observer:
        assert observer.query(NflverseGameContext).count() == 2


def test_td_card_grades_batch_sources_and_match_individual_grading(db):
    from test_publication_pipeline import seed, NOW
    from spe.db.models import Game, PublishedStat
    from spe.models.player_td.desk_replay import grade, _refresh_card_grades
    seed(db)
    game = db.get(Game, "g")
    game.status = "final"
    rows = []
    statuses = ["espn.summary", "espn.summary.confirmed_inactive", "espn.summary.participation_unconfirmed"]
    for i in range(60):
        pid = f"p{i}"
        db.add(PublishedStat(key=f"g|{pid}|anytime_td", game_id="g", player_id=pid,
            market_id="anytime_td", actual=i % 2, source=statuses[i % 3], observed_at=NOW))
        rows.append({"game_id": "g", "player_id": pid, "decision": {"most_likely": {"player_id": pid},
            "value_candidate": {"player_id": f"p{(i + 1) % 60}", "price_supported": True, "american": 155}}})
    db.commit()
    expected = [grade(db, game, row["player_id"]) for row in rows]
    card = {"rows": deepcopy(rows)}
    statements = []
    def observe(conn, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(db.get_bind(), "before_cursor_execute", observe)
    try:
        _refresh_card_grades(db, card)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", observe)
    assert [r["result"] for r in card["rows"]] == expected
    assert [r["decision_results"]["value_candidate"]["result"] for r in card["rows"]] == expected[1:] + expected[:1]
    assert len(statements) == 2


@pytest.mark.parametrize("path", [
    "/v1/published/day?date=2026-02-31", "/v1/published/calendar?month=2026-13",
    "/v1/slate?date=garbage", "/v1/desk?date=20261116", "/v1/props?date=2026-02-29",
    "/v1/parlays/card?date=2026-04-31", "/v1/best-bets?league=invalid",
])
def test_invalid_board_queries_are_rejected_without_running_handlers(db, path):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.deps import db_session
    app.dependency_overrides[db_session] = lambda: db
    try:
        response = TestClient(app).get(path)
        assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_parlay_rejects_duplicate_players_unsupported_lines_and_invalid_prices():
    from app.schemas import ParlayEvaluate
    from pydantic import ValidationError
    valid = {"game_id": "g", "legs": [{"player_id": "a", "market_id": "anytime_td"},
        {"player_id": "b", "market_id": "anytime_td"}], "book_american": 155}
    assert len(ParlayEvaluate(**valid).legs) == 2
    invalid = [{**valid, "legs": [valid["legs"][0]] * 2}, {**valid, "legs": valid["legs"] * 20},
        {**valid, "book_american": 0}, {**valid, "book_decimal": float("inf")},
        {**valid, "legs": [{**leg, "side": "no"} for leg in valid["legs"]]},
        {**valid, "legs": [{**leg, "market_id": "receptions", "line": 4.5} for leg in valid["legs"]]}]
    for payload in invalid:
        with pytest.raises(ValidationError):
            ParlayEvaluate(**payload)


def test_subscription_is_checked_once_and_database_failures_are_retryable(db, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import OperationalError
    from app.main import app
    from app.deps import db_session
    calls = []
    def subscribed(*args):
        calls.append(1)
        return True
    monkeypatch.setattr("app.access.request_is_subscribed", subscribed)
    monkeypatch.setattr("spe.models.ats.board_cache.load_board", lambda *a, **k: {"rows": []})
    app.dependency_overrides[db_session] = lambda: db
    try:
        client = TestClient(app)
        assert client.get("/v1/ats", headers={"Authorization": "Bearer test"}).status_code == 200
        assert len(calls) == 1
        def unavailable(*args):
            raise OperationalError("query", {}, Exception("unavailable"))
        monkeypatch.setattr("app.access.request_is_subscribed", unavailable)
        response = client.post("/v1/simulations", headers={"Authorization": "Bearer test"}, json={})
        assert response.status_code == 503 and response.headers["retry-after"] == "5"
        assert "subscription_required" not in response.text
    finally:
        app.dependency_overrides.clear()


def test_isolated_input_failure_is_reported_and_other_feeds_still_run(db, monkeypatch):
    from contextlib import nullcontext
    import subprocess
    from spe.config import settings
    from spe.publishing import pipeline
    monkeypatch.setattr(settings, "app_env", "prod")
    monkeypatch.setattr(pipeline, "init_db", lambda: None)
    monkeypatch.setattr(pipeline, "lane_lock", lambda lane: nullcontext(True))
    monkeypatch.setattr(pipeline, "session_factory", lambda: Session(db.get_bind()))
    completed = []
    def child(command, *, check):
        assert check
        completed.append(command[-1])
        if command[-1] == "production_nflverse":
            raise subprocess.CalledProcessError(137, command)
    monkeypatch.setattr(subprocess, "run", child)
    with pytest.raises(RuntimeError, match="production_nflverse"):
        pipeline.production_cycle("inputs")
    assert completed == ["production_inputs", "production_weather", "production_nflverse", "production_free_college"]
