from datetime import UTC, datetime, timedelta
import pytest
from spe.db.models import (
    Sport,
    League,
    Season,
    Team,
    Venue,
    Game,
    PublishedPick,
    PublishedStat,
    PublishedResult,
    JobRun,
)
from spe.publishing.policy import assess
from spe.db.models import DeskDecision
from spe.publishing.ledger import publish, current_picks, day_picks, settle, history, record_frozen_desk_picks
from spe.publishing.pipeline import run_step, health
from spe.publishing.store import read, write
from spe.publishing.boxes import parse

NOW = datetime(2026, 11, 15, 17, tzinfo=UTC)


def row(**overrides):
    data = {
        "game_id": "g",
        "league_id": "nfl",
        "market_id": "rushing_yards",
        "outcome_id": "o",
        "player_id": "p",
        "player_name": "Player",
        "team": "A",
        "away_abbr": "A",
        "home_abbr": "B",
        "bookmaker": "Book",
        "book_price": 110,
        "side": "over",
        "line": 60.5,
        "kickoff_utc": (NOW + timedelta(hours=1)).isoformat(),
        "data_tier": "upcoming",
        "forecast": {
            "p_estimate": 0.6,
            "ev_after_haircut": 0.12,
            "availability_status": "active",
            "model_version": "m1",
            "prediction_as_of": NOW.isoformat(),
            "data_cutoff": NOW.isoformat(),
        },
        "price_freshness": {
            "status": "fresh",
            "verification": "observed",
            "is_current_opportunity": True,
            "observed_at": NOW.isoformat(),
        },
    }
    for key, value in overrides.items():
        data[key] = {**data[key], **value} if isinstance(value, dict) else value
    return data


def seed(db):
    db.add(Sport(sport_id="s", name="Football"))
    db.flush()
    db.add(League(league_id="nfl", sport_id="s", name="NFL", classification="pro"))
    db.flush()
    db.add(
        Season(
            season_id="season",
            league_id="nfl",
            year=2026,
            start_at=NOW - timedelta(days=50),
            end_at=NOW + timedelta(days=50),
        )
    )
    db.add(Venue(venue_id="v", name="Field", lat=0, lon=0, roof_type="open", surface="grass"))
    db.add_all(
        [
            Team(team_id="a", league_id="nfl", name="A", abbreviation="A"),
            Team(team_id="b", league_id="nfl", name="B", abbreviation="B"),
        ]
    )
    db.flush()
    game = Game(
        game_id="g",
        season_id="season",
        league_id="nfl",
        home_team_id="a",
        away_team_id="b",
        venue_id="v",
        kickoff_utc=NOW + timedelta(hours=1),
        data_tier="upcoming",
        status="scheduled",
    )
    db.add(game)
    db.commit()
    return game


@pytest.mark.parametrize(
    "change",
    [
        {"forecast": {"ev_after_haircut": -0.1}},
        {"forecast": {"ev_after_haircut": 0.039}},
        {"forecast": {"p_estimate": float("nan")}},
        {"forecast": {"availability_status": "unknown"}},
        {"forecast": {"availability_status": "out"}},
        {"book_price": 0},
        {"bookmaker": None},
        {"price_freshness": {"observed_at": None}},
        {"price_freshness": {"observed_at": (NOW - timedelta(minutes=13)).isoformat()}},
        {"price_freshness": {"observed_at": (NOW + timedelta(seconds=1)).isoformat()}},
        {"forecast": {"data_cutoff": (NOW + timedelta(seconds=1)).isoformat()}},
        {"kickoff_utc": NOW.isoformat()},
        {"forecast": {"reason_codes": ["IDENTITY_CONFLICT"]}},
    ],
)
def test_policy_fails_closed(change):
    assert not assess(row(**change), NOW)["eligible"]


def test_expiry_uses_timestamps_not_cached_fresh_flag():
    quote = row()
    assert assess(quote, NOW)["eligible"]
    assert not assess(quote, NOW + timedelta(minutes=13))["eligible"]
    minimum = assess(quote, NOW)["minimum_american_odds"]
    assert minimum == -105


def test_publication_freezes_and_grades_original_price(db):
    game = seed(db)
    assert publish(db, [row()], NOW)["published"] == 1
    db.commit()
    first = db.query(PublishedPick).one()
    original = first.snapshot.copy()
    assert publish(db, [row(book_price=150)], NOW + timedelta(seconds=1))["published"] == 0
    assert first.snapshot == original
    assert current_picks(db, [row(book_price=150)], NOW) == []
    assert current_picks(db, [row()], NOW)[0]["pick_id"] == first.pick_id
    assert current_picks(db, [row()], NOW + timedelta(minutes=13)) == []
    stayed = day_picks(db, "nfl", "2026-11-15")
    assert len(stayed) == 1
    assert stayed[0]["player_name"] == "Player"
    assert stayed[0]["book_price"] == 110
    assert stayed[0]["result"] == "pending"
    assert day_picks(db, "fbs", "2026-11-15") == []
    # Missing stats remain pending, even once the game has finished.
    game.status = "final"
    db.commit()
    assert settle(db, NOW + timedelta(hours=5))["pending"] == 1
    assert current_picks(db, [row()], NOW) == []
    db.add(
        PublishedStat(
            key="g|p|rushing_yards",
            game_id="g",
            player_id="p",
            market_id="rushing_yards",
            actual=80,
            observed_at=NOW + timedelta(hours=5),
            source="test.official",
        )
    )
    db.commit()
    assert settle(db)["settled"] == 1
    db.commit()
    assert settle(db)["settled"] == 0
    graded = day_picks(db, "nfl", "2026-11-15")
    assert graded[0]["result"] == "win"
    assert graded[0]["actual"] == 80
    result = history(db)
    assert result["rows"][0]["book_price"] == 110
    assert result["summaries"][0]["units"] == pytest.approx(1.1)
    assert result["summaries"][0]["roi"] == pytest.approx(1.1)
    assert history(db, league="fbs")["total"] == 0


def test_postkick_publication_and_rewrites_rejected(db):
    seed(db)
    assert publish(db, [row()], NOW + timedelta(hours=2))["published"] == 0
    publish(db, [row()], NOW)
    db.commit()
    record = db.query(PublishedPick).one()
    record.snapshot = {"changed": True}
    with pytest.raises(ValueError, match="append-only"):
        db.flush()
    db.rollback()


def test_shared_cache_visible_across_sessions_and_empty_replaces_old(db):
    write("props:test", {"rows": [{"id": 1}]})
    assert read("props:test")["rows"] == [{"id": 1}]
    write("props:test", {"rows": []})
    assert read("props:test")["rows"] == []


def test_failed_job_is_durable_and_degraded_summary_is_not_success(db):
    def fail(session):
        raise RuntimeError("simulated")

    with pytest.raises(RuntimeError):
        run_step("production_odds", fail)
    db.expire_all()
    assert db.query(JobRun).filter_by(job_name="production_odds").one().status == "failed"
    with pytest.raises(RuntimeError):
        run_step("production_inputs", lambda s: {"status": "needs_key"})
    assert health(db)["ok"] is False


def test_box_parser_does_not_invent_yardage_for_returner():
    payload = {
        "boxscore": {
            "players": [
                {
                    "statistics": [
                        {
                            "name": "kickReturns",
                            "labels": ["NO", "YDS", "TD"],
                            "athletes": [
                                {
                                    "athlete": {"id": "1", "displayName": "Player"},
                                    "stats": ["1", "90", "1"],
                                }
                            ],
                        },
                        {
                            "name": "receiving",
                            "labels": ["REC", "YDS", "TD"],
                            "athletes": [
                                {
                                    "athlete": {"id": "2", "displayName": "Receiver"},
                                    "stats": ["5", "60", "0"],
                                }
                            ],
                        },
                    ]
                }
            ]
        }
    }
    parsed = {r["name"]: r["markets"] for r in parse(payload)}
    assert parsed["Player"] == {"anytime_td": 1}
    assert parsed["Receiver"] == {"anytime_td": 0, "receptions": 5, "receiving_yards": 60}


@pytest.mark.parametrize(
    "actual,result,units", [(60.5, "push", 0), (20, "loss", -1), (70, "win", 1.1)]
)
def test_settlement_results(db, actual, result, units):
    game = seed(db)
    publish(db, [row()], NOW)
    db.commit()
    game.status = "final"
    db.add(
        PublishedStat(
            key="g|p|rushing_yards",
            game_id="g",
            player_id="p",
            market_id="rushing_yards",
            actual=actual,
            observed_at=NOW,
            source="test",
        )
    )
    db.commit()
    settle(db)
    db.commit()
    entry = db.query(PublishedResult).one()
    assert entry.result == result
    assert entry.units == pytest.approx(units)


def test_api_returns_saved_pick_and_history_then_expires_it(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.deps import db_session
    from spe.props.board_cache import write_entry, WARM_MARKETS
    from spe.config import settings

    # Use real current times to exercise the route's actual clock and cache reads.
    now = datetime.now(UTC)
    seed(db)
    game = db.get(Game, "g")
    game.kickoff_utc = now + timedelta(hours=1)
    db.commit()
    quote = row(
        kickoff_utc=game.kickoff_utc.isoformat(),
        forecast={"prediction_as_of": now.isoformat(), "data_cutoff": now.isoformat()},
        price_freshness={"observed_at": now.isoformat()},
    )
    publish(db, [quote], now)
    db.commit()
    from app.queries import kickoff_day

    date = kickoff_day(game.kickoff_utc)
    for market in WARM_MARKETS:
        write_entry(market, "nfl", date, [quote] if market == "rushing_yards" else [])
    monkeypatch.setattr(settings, "app_env", "prod")
    app.dependency_overrides[db_session] = lambda: db
    try:
        client = TestClient(app)
        response = client.get(f"/v1/props/picks?league=nfl&date={date}")
        assert response.status_code == 200
        assert len(response.json()["picks"]) == 1
        assert client.get("/v1/published/history").json()["total"] == 1
        assert client.get("/ready").status_code == 503
        quote["price_freshness"]["observed_at"] = (now - timedelta(minutes=20)).isoformat()
        write_entry("rushing_yards", "nfl", date, [quote])
        expired = client.get(f"/v1/props/picks?league=nfl&date={date}").json()
        assert expired["picks"] == []
        assert len(expired["day"]) == 1
        assert expired["day"][0]["locked"] is True
        assert client.get("/v1/published/history").json()["total"] == 1
    finally:
        app.dependency_overrides.clear()


def test_cancellation_void_and_summary_counts_all_pages(db):
    game = seed(db)
    publish(db, [row()], NOW)
    db.commit()
    game.status = "canceled"
    db.commit()
    settle(db)
    db.commit()
    result = history(db, offset=1, limit=1)
    assert result["rows"] == []
    assert result["total"] == 1
    assert result["summaries"][0]["void"] == 1
    assert result["summaries"][0]["roi"] is None


def test_pipeline_lock_prevents_overlap(db):
    from spe.publishing.pipeline import lane_lock

    with lane_lock("odds") as acquired:
        assert acquired
        with lane_lock("odds") as second:
            assert not second
    with lane_lock("odds") as released:
        assert released


def test_job_becomes_overdue_after_success(db):
    from spe.publishing.pipeline import JOBS

    for name in JOBS:
        run_step(name, lambda s: {"status": "ok"})
    assert health(db)["ok"]
    assert not health(db, datetime.now(UTC) + timedelta(hours=4))["ok"]


def test_ats_history_uses_frozen_line_and_price(db):
    from spe.db.models import AtsPrediction
    from spe.models.ats.research_bundle import CLEAN_PROSPECTIVE_COHORT
    from spe.shadow.ats import settle_ats_predictions
    from spe.publishing.ledger import ats_history

    game = seed(db)
    db.add(
        AtsPrediction(
            ats_id="ats1",
            game_id="g",
            league_id="nfl",
            home_team_id="a",
            away_team_id="b",
            bookmaker_id="Book",
            side="home",
            spread_line=-3.5,
            price_american=-110,
            predicted_margin=7,
            predicted_home_score=28,
            predicted_away_score=21,
            p_cover=0.6,
            margin_sigma=10,
            model_version_id="frozen-model",
            predicted_at=NOW,
            components={
                "cohort_version": CLEAN_PROSPECTIVE_COHORT,
                "eligibility": "clean_prospective",
                "home_handicap": -3.5,
            },
        )
    )
    db.commit()
    game.status, game.home_score, game.away_score = "final", 24, 20
    db.commit()
    settle_ats_predictions(db, NOW + timedelta(hours=5))
    db.commit()
    result = ats_history(db, "nfl")
    assert result["rows"][0]["sentence"] == "a -3.5"
    assert result["rows"][0]["book_price"] == -110
    assert result["rows"][0]["forecast"]["model_version"] == "frozen-model"
    assert result["summaries"][0]["units"] == pytest.approx(100 / 110)


def _desk_row():
    return {
        "game_id": "g",
        "league_id": "nfl",
        "market_id": "anytime_td",
        "player_id": "p",
        "player_name": "Scorer",
        "side": "yes",
        "book_price": 150,
        "bookmaker": "DraftKings",
        "forecast": {"p_estimate": 0.42, "model_version": "desk"},
        "kickoff_utc": NOW.isoformat(),
    }


def test_last_prekick_desk_pick_is_graded_on_the_public_record(db):
    game = seed(db)
    game.kickoff_utc = NOW
    game.status = "final"
    game.home_score = 24
    game.away_score = 17
    db.add(
        DeskDecision(
            game_id="g",
            decided_at=NOW - timedelta(hours=2),
            payload={"row": _desk_row(), "kickoff_utc": NOW.isoformat(), "game_id": "g"},
        )
    )
    db.add(
        DeskDecision(
            game_id="g",
            decided_at=NOW + timedelta(hours=1),
            payload={"row": None, "kickoff_utc": NOW.isoformat(), "game_id": "g"},
        )
    )
    db.commit()
    assert record_frozen_desk_picks(db)["recorded"] == 1
    db.add(
        PublishedStat(
            key="g|p|anytime_td",
            game_id="g",
            player_id="p",
            market_id="anytime_td",
            actual=1,
            observed_at=NOW,
            source="test",
        )
    )
    db.commit()
    settle(db)
    db.commit()
    record = history(db)
    assert record["total"] == 1
    assert record["rows"][0]["result"] == "win"
    assert record["rows"][0]["player_name"] == "Scorer"
    assert record["rows"][0]["sentence"].startswith("Scorer anytime touchdown")


def test_a_recorded_touchdown_grades_the_desk_scorer():
    from spe.shadow.desk_ledger import grade_desk_scorer

    assert grade_desk_scorer("pending", 1) == "win"
    assert grade_desk_scorer("blocked", 0) == "loss"
    assert grade_desk_scorer("win", 0) == "win"
    assert grade_desk_scorer("pending", None) == "pending"


def test_a_finished_box_records_an_omitted_stat_as_zero():
    from spe.publishing.boxes import box_actual

    assert box_actual({"receiving_yards": 32}, "receiving_yards", final=True) == 32
    assert box_actual({"rushing_yards": 89}, "receiving_yards", final=True) == 0
    assert box_actual(None, "anytime_td", final=True) is None
    assert box_actual({"rushing_yards": 43}, "anytime_td", final=True) is None
    assert box_actual(None, "receptions", final=False) is None
    assert box_actual(None, "receptions", final=True) is None
    assert box_actual(None, "receiving_yards", final=True) is None


def test_a_day_card_keeps_spreads_props_and_touchdowns():
    from spe.publishing.ledger import _ledger_rows, _ledger_summary

    rows = _ledger_rows(
        [
            {
                "game_id": "g",
                "league_id": "fbs",
                "ats_pick_label": "Home -3.5",
                "ats_pick_result": "correct",
                "away_team": "Away",
                "home_team": "Home",
                "away_score": 10,
                "home_score": 20,
                "game_status": "final",
                "kickoff_utc": "2026-11-14T17:00:00+00:00",
            }
        ],
        [
            {
                "outcome_id": "o",
                "game_id": "g",
                "player_id": "p",
                "market_id": "receptions",
                "league_id": "fbs",
                "pick": "Runner over 4.5 receptions",
                "bookmaker": "FanDuel",
                "book_price": -110,
                "actual": 6,
                "result": "win",
                "kickoff_utc": "2026-11-14T17:00:00+00:00",
            }
        ],
        [
            {
                "pick_id": "td",
                "game_id": "g2",
                "league_id": "fbs",
                "sentence": "Scorer anytime touchdown.",
                "bookmaker": "DraftKings",
                "book_price": 150,
                "result": "loss",
                "units": -1,
                "kickoff_utc": "2026-11-14T20:00:00+00:00",
            }
        ],
    )
    assert [row["result"] for row in rows] == ["win", "win", "loss"]
    assert rows[0]["sentence"] == "Home -3.5"
    assert "Final 10–20" in rows[0]["detail"]
    assert rows[1]["detail"] == "FanDuel · -110 · Actual 6"
    summary = _ledger_summary(rows)
    assert summary["win"] == 2 and summary["loss"] == 1


def test_a_game_publishes_one_prop_and_keeps_later_markets_off(db):
    seed(db)
    yards = row(
        market_id="receiving_yards",
        outcome_id="yards",
        player_id="best",
        player_name="Best",
        line=33.5,
        book_price=-110,
        forecast={"p_estimate": 0.64, "ev_after_haircut": 0.08},
    )
    catches = row(
        market_id="receptions",
        outcome_id="catches",
        player_id="mid",
        player_name="Mid",
        line=5.5,
        book_price=-102,
        forecast={"p_estimate": 0.58, "ev_after_haircut": 0.05},
    )
    touchdown = row(
        market_id="anytime_td",
        outcome_id="td",
        player_id="td",
        player_name="Touchdown",
        side="yes",
        book_price=205,
        forecast={"p_estimate": 0.44, "ev_after_haircut": 0.06},
    )
    assert publish(db, [yards, catches, touchdown], NOW)["published"] == 1
    db.commit()
    saved = db.query(PublishedPick).one()
    assert saved.snapshot["player_name"] == "Best"
    assert publish(db, [catches, touchdown], NOW + timedelta(seconds=1))["published"] == 0
    assert db.query(PublishedPick).count() == 1


def test_a_desk_touchdown_does_not_stack_on_a_published_prop(db):
    seed(db)
    assert publish(db, [row()], NOW)["published"] == 1
    db.commit()
    db.add(
        DeskDecision(
            game_id="g",
            decided_at=NOW - timedelta(minutes=30),
            payload={"row": _desk_row(), "kickoff_utc": NOW.isoformat(), "game_id": "g"},
        )
    )
    db.commit()
    assert record_frozen_desk_picks(db)["recorded"] == 0
    assert db.query(PublishedPick).count() == 1
    assert db.query(PublishedPick).one().market_id == "rushing_yards"


def test_odds_lane_saves_the_board_before_downloading_prices():
    from types import SimpleNamespace
    from spe.publishing.pipeline import ODDS_LANE, _board_game_ids

    # ATS and the live card are saved before the price download. A second ATS
    # pass runs after new spreads land. Games that already kicked off must not
    # stand in front of the ones the homepage is still showing.
    assert ODDS_LANE[0] == "production_ats"
    assert ODDS_LANE[1] == "production_publish"
    assert ODDS_LANE.index("production_odds") > ODDS_LANE.index("production_publish")
    assert ODDS_LANE.index("production_spreads") > ODDS_LANE.index("production_odds")
    assert ODDS_LANE[-1] == "production_ats"
    assert ODDS_LANE.count("production_publish") == 2
    assert ODDS_LANE.count("production_ats") == 2
    now = NOW
    games = [
        SimpleNamespace(game_id="started", kickoff_utc=now - timedelta(hours=2), status="scheduled"),
        SimpleNamespace(game_id="later", kickoff_utc=now + timedelta(hours=4), status="scheduled"),
        SimpleNamespace(game_id="next", kickoff_utc=now + timedelta(hours=1), status="scheduled"),
        SimpleNamespace(game_id="live", kickoff_utc=now + timedelta(minutes=30), status="in_progress"),
    ]
    assert _board_game_ids(games, now) == ["next", "later", "started"]
    assert _board_game_ids(games, now, upcoming_only=True) == ["next", "later"]


def test_a_pass_before_kickoff_stays_off_the_public_record(db):
    game = seed(db)
    game.kickoff_utc = NOW
    db.add(
        DeskDecision(
            game_id="g",
            decided_at=NOW - timedelta(hours=3),
            payload={"row": _desk_row(), "kickoff_utc": NOW.isoformat(), "game_id": "g"},
        )
    )
    db.add(
        DeskDecision(
            game_id="g",
            decided_at=NOW - timedelta(hours=1),
            payload={"row": None, "kickoff_utc": NOW.isoformat(), "game_id": "g"},
        )
    )
    db.commit()
    assert record_frozen_desk_picks(db)["recorded"] == 0
    assert history(db)["total"] == 0
