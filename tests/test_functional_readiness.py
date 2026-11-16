from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from spe.db.models import BoardSnapshot, JobRun, PublishedPick
from spe.publishing.pipeline import job_status


def test_empty_saved_card_keeps_unavailable_reasons(db):
    from spe.models.player_td.desk_replay import _store_day_card, saved_day_card
    payload = {"date": "2026-11-16", "league": "nfl", "rows": [],
               "unavailable_games": [{"game_id": "g", "reason": "Starter ruled out"}]}
    _store_day_card(db, payload); db.flush()
    card = saved_day_card(db, "2026-11-16", "nfl")
    assert card["unavailable_games"] == payload["unavailable_games"]
    assert card["updated_at"]
    _store_day_card(db, {**payload, "unavailable_games": []}); db.flush()
    assert saved_day_card(db, "2026-11-16", "nfl")["unavailable_games"] == []


def test_history_uses_two_queries_for_a_hundred_picks(db):
    from test_publication_pipeline import seed, row, NOW
    from spe.publishing.ledger import history
    seed(db)
    for i in range(100):
        db.add(PublishedPick(pick_id=str(i), game_id="g", league_id="nfl", market_id=f"test_{i}",
            published_at=NOW, kickoff_utc=NOW+timedelta(hours=1), snapshot=row(p=.6)))
    db.commit()
    statements = []
    def observed(conn, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(db.get_bind(), "before_cursor_execute", observed)
    try:
        result = history(db, limit=10)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", observed)
    assert result["total"] == 100 and len(result["rows"]) == 10
    assert len(statements) == 2


def test_slate_dates_includes_td_only_days(db):
    from spe.publishing.ledger import slate_dates
    db.add(BoardSnapshot(key="atd-desk-card-v1:2026-11-16:nfl", payload={}, updated_at=datetime.now(UTC)))
    db.flush()
    assert "2026-11-16" in slate_dates(db)


def test_stuck_worker_is_not_reported_as_updating(db):
    now = datetime.now(UTC)
    db.add(JobRun(job_name="production_touchdowns", started_at=now-timedelta(minutes=10), status="running", summary={}))
    db.flush()
    assert job_status(db, "production_touchdowns", now) == "stalled"


def test_result_matching_uses_provider_mapping_and_rejects_ambiguity(db):
    from spe.db.models import ProviderIdMap
    from spe.identity.results import espn_player_ids, match_result
    people = [{"name": "Alex Smith", "espn_id": "123"}, {"name": "Alex Smith", "espn_id": "456"}]
    db.add(ProviderIdMap(provider="espn", entity_type="player", canonical_id="cfbd:88", external_id="456", confidence=1))
    db.flush()
    ids = espn_player_ids(db, {"cfbd:88", "cfbd:123"})
    assert match_result(people, "cfbd:88", "Alex Smith", ids)["espn_id"] == "456"
    assert match_result(people, "cfbd:123", "Alex Smith", ids) is None
    assert match_result(people[:1], "cfbd:88", "Alex Smith", ids) is None


def test_watchlists_are_authenticated_isolated_and_idempotent(db):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.deps import db_session
    from spe.accounts.auth import register_user, open_session
    first = register_user(db, "first@example.com", "long-password")
    second = register_user(db, "second@example.com", "long-password")
    one, two = open_session(db, first), open_session(db, second)
    db.commit()
    app.dependency_overrides[db_session] = lambda: db
    try:
        client = TestClient(app)
        assert client.get("/v1/watchlist").status_code == 401
        entry = {"id": "a"*24, "player": "Player", "pick": "Anytime TD", "kickoff": "2026-11-17T18:00:00Z", "game_id": "g"}
        headers = {"authorization": f"Bearer {one}"}
        for _ in range(2):
            response = client.post("/v1/watchlist", headers=headers, json={"action": "save", "id": entry["id"], "selection": entry})
            assert response.status_code == 200
            assert len(response.json()["saved"]) == 1
        assert client.get("/v1/watchlist", headers={"authorization": f"Bearer {two}"}).json()["saved"] == []
        client.post("/v1/watchlist", headers={"authorization": f"Bearer {two}"}, json={"action": "remove", "id": entry["id"]})
        assert len(client.get("/v1/watchlist", headers=headers).json()["saved"]) == 1
        assert client.get("/v1/ops/metrics", headers=headers).status_code == 403
        first.is_admin = True; db.commit()
        metrics = client.get("/v1/ops/metrics", headers=headers)
        assert metrics.status_code == 200 and metrics.json()["routes"]
        assert "X-Request-ID" in metrics.headers
        assert client.post("/v1/client-metrics", json={"route": "/props", "kind": "error", "value": 1}).status_code == 202
        assert client.post("/v1/client-metrics", json={"route": "/account?email=private", "kind": "error", "value": 1}).status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_worker_supervisor_stops_its_child():
    import sys
    from worker.supervise import run
    assert run([sys.executable, "-c", "import time; time.sleep(30)"], .1) == 124


def test_public_availability_uses_a_typed_timestamp(db):
    from spe.ingest.public_availability import store_reports
    assert store_reports(db, []) == {"inserted": 0, "unmatched": 0, "duplicates": 0}


def test_history_get_cannot_publish_or_settle(db, monkeypatch):
    from app.main import published_history
    def forbidden(*args, **kwargs):
        raise AssertionError("A read attempted to mutate the published record")
    monkeypatch.setattr("spe.publishing.ledger.settle", forbidden)
    monkeypatch.setattr("spe.publishing.ledger.record_frozen_desk_picks", forbidden)
    assert published_history(None, None, 0, 30, db)["rows"] == []


def test_failed_scoreboard_does_not_discard_the_other_league(db):
    from spe.models.player_td.desk_replay_results import refresh_results
    calls = []
    def feed(league, day):
        calls.append(league)
        if league == "fbs":
            raise TimeoutError("provider unavailable")
        return []
    result = refresh_results(db, "2026-11-16", fetch=feed)
    assert calls == ["nfl", "fbs"]
    assert result["status"] == "degraded" and result["failures"][0]["league"] == "fbs"


def test_ats_day_archive_keeps_started_selection(db):
    from spe.models.ats.board_cache import archive_days, load_board
    now = datetime.now(UTC)
    kickoff = now+timedelta(hours=1)
    from app.queries import kickoff_day
    original = {"game_id": "g", "kickoff_utc": kickoff.isoformat(), "ats_side": "home", "ats_line": -3.5}
    archive_days(db, {"as_of": now.isoformat(), "rows": [original]}, now=now)
    db.commit()
    archive_days(db, {"as_of": (now+timedelta(hours=2)).isoformat(), "rows": [{**original, "ats_side": "away", "ats_line": 4.5}]}, now=now+timedelta(hours=2))
    db.commit()
    card = load_board(db, date=kickoff_day(kickoff))
    assert card["rows"][0]["ats_side"] == "home" and card["rows"][0]["ats_line"] == -3.5
    assert card["rows"][0]["record_status"] == "saved_pregame"


def test_compact_td_card_preserves_explanation_but_omits_feature_matrix():
    from spe.models.player_td.presentation import card_payload
    candidate = {"player_id": "p", "name": "Player", "probability": .5, "customer": {"why": "Recent goal-line work."},
                 "eligibility_inputs": {"features": list(range(1000))}, "research": {"opponent_games": 6, "matrix": list(range(1000))}}
    original = {"rows": [{"player_id": "p", "decision": {"most_likely": candidate, "alternatives": [candidate]},
        "desk_analysis": {"scouting_report": {"customer": candidate["customer"], "role": ["full details"]}}}]}
    compact = card_payload(original)["rows"][0]
    assert compact["decision"]["most_likely"]["probability"] == .5
    assert "eligibility_inputs" not in compact["decision"]["most_likely"]
    assert "matrix" not in compact["decision"]["most_likely"]["research"]
    assert compact["desk_analysis"]["scouting_report"]["customer"] == candidate["customer"]
    assert original["rows"][0]["decision"]["alternatives"]


def test_confirmed_inactive_is_void_but_listed_zero_touch_is_a_loss(db):
    from test_live_td_mark import _seed, _final_box, _athlete, NOW
    from spe.db.models import Player, InjuryEvent, Game
    from spe.desk.day_results import apply_recorded_results, _CHECKED
    _CHECKED.clear(); _seed(db)
    gid = "nfl:2026_10_NE_DET"
    db.get(Game, gid).status = "final"
    db.add(Player(player_id="inactive", name="Unavailable Player", position_group="skill", position="WR")); db.flush()
    db.add(InjuryEvent(game_id=gid, player_id="inactive", designation="inactive", reported_at=NOW,
        source="official_inactives", confidence=1, provider="test", provider_record_id="i", observed_at=NOW, effective_at=NOW))
    db.commit()
    rows = [{"game_id": gid, "player_id": pid, "player_name": name, "market_id": "receptions", "side": "over", "line": .5}
        for pid, name in [("inactive", "Unavailable Player"), ("listed", "Listed Player")]]
    box = _final_box([("rushing", ["CAR", "YDS", "TD"], [_athlete("listed", "Listed Player", ["5", "20", "0"])])])
    apply_recorded_results(db, [], rows, fetch=lambda *args: box)
    assert rows[0]["result"] == "void"
    assert rows[1]["result"] == "loss" and rows[1]["actual"] == 0


def test_qb_passing_touchdowns_do_not_grade_as_anytime_scores(db):
    from test_publication_pipeline import seed, NOW
    from spe.db.models import Player, PlayerGameStat
    from app.queries import _attach_prop_results
    game = seed(db); game.status = "final"
    db.add(Player(player_id="qb", name="Quarterback", position_group="QB", position="QB")); db.flush()
    db.add(PlayerGameStat(game_id="g", player_id="qb", pass_td=4, rush_td=0, rec_td=0,
        provider="test", provider_record_id="qb-box", observed_at=NOW, effective_at=NOW))
    db.flush()
    rows = [{"game_id": "g", "player_id": "qb", "market_id": "anytime_td", "side": "yes"}]
    _attach_prop_results(db, rows)
    assert rows[0]["actual"] == 0 and rows[0]["result"] == "loss"


def test_unconfirmed_participation_withdraws_a_grade_without_erasing_history(db):
    from test_publication_pipeline import seed, row, NOW
    from spe.db.models import PublishedResult, PublishedCorrection, PublishedStat
    from spe.publishing.ledger import publish, settle, history
    game = seed(db)
    publish(db, [row()], NOW); db.commit()
    pick = db.query(PublishedPick).one()
    game.status = "final"
    db.add(PublishedResult(pick_id=pick.pick_id, result="loss", actual=0, units=-1, settled_at=NOW, evidence={}))
    db.add(PublishedStat(key="g|p|rushing_yards", game_id="g", player_id="p", market_id="rushing_yards",
        actual=0, observed_at=NOW, source="espn.summary.participation_unconfirmed"))
    db.commit(); settle(db); db.commit(); settle(db); db.commit()
    assert db.query(PublishedResult).one().result == "loss"
    assert db.query(PublishedCorrection).one().result == "pending"
    result = history(db)
    assert result["rows"][0]["result"] == "pending" and result["rows"][0]["units"] is None
    assert result["summaries"][0]["accuracy"]["graded"] == 0
