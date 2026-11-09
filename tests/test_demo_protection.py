from __future__ import annotations

from datetime import UTC, datetime

from spe.db.models import (
    Bookmaker,
    Forecast,
    Game,
    League,
    Market,
    ModelVersion,
    OddsTick,
    Outcome,
    Player,
    Recommendation,
    Season,
    Sport,
    Team,
    Venue,
)
from spe.recommend.engine import RecommendInput, recommend

from app.queries import forecast_payload, games_for_slate


def _seed_demo_and_historical(db) -> None:
    db.add(Sport(sport_id="football", name="American football"))
    db.flush()
    db.add(League(league_id="nfl", sport_id="football", name="NFL", classification="nfl"))
    db.flush()
    db.add(
        Season(
            season_id="nfl-2023",
            league_id="nfl",
            year=2023,
            start_at=datetime(2023, 9, 1, tzinfo=UTC),
            end_at=datetime(2024, 2, 15, tzinfo=UTC),
        )
    )
    db.add(
        Venue(
            venue_id="v1",
            name="Test Bowl",
            lat=0,
            lon=0,
            roof_type="open",
            surface="grass",
        )
    )
    db.add_all(
        [
            Team(team_id="nfl:AAA", league_id="nfl", name="A", abbreviation="AAA"),
            Team(team_id="nfl:BBB", league_id="nfl", name="B", abbreviation="BBB"),
        ]
    )
    db.flush()
    db.add_all(
        [
            Game(
                game_id="demo-game",
                season_id="nfl-2023",
                league_id="nfl",
                home_team_id="nfl:AAA",
                away_team_id="nfl:BBB",
                kickoff_utc=datetime(2026, 11, 8, tzinfo=UTC),
                venue_id="v1",
                data_tier="demo",
            ),
            Game(
                game_id="nfl:2023_01_AAA_BBB",
                season_id="nfl-2023",
                league_id="nfl",
                home_team_id="nfl:AAA",
                away_team_id="nfl:BBB",
                kickoff_utc=datetime(2023, 9, 10, tzinfo=UTC),
                venue_id="v1",
                data_tier="historical",
                status="final",
            ),
        ]
    )
    db.add(Player(player_id="p1", name="Demo WR", position_group="WR", position="WR"))
    db.add(Market(market_id="anytime_td", sport="football", market_type="player", period="game", stat="td", grading_rule="anytime"))
    db.add(
        ModelVersion(
            model_version_id="mv-demo",
            code_sha="test",
            training_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
            data_snapshot="seed",
            hyperparameters={},
            calibration_version="0",
            league_id="nfl",
            family="demo",
        )
    )
    db.flush()
    db.add(
        Outcome(
            outcome_id="o1",
            market_id="anytime_td",
            game_id="demo-game",
            player_id="p1",
            side="yes",
        )
    )
    db.flush()
    fc = Forecast(
        forecast_id="f1",
        outcome_id="o1",
        as_of=datetime(2026, 11, 6, tzinfo=UTC),
        model_version_id="mv-demo",
        p=0.44,
        p_low=0.36,
        p_high=0.52,
        sim_count=1000,
        data_cutoff=datetime(2026, 11, 6, tzinfo=UTC),
        availability_status="probable",
        reason_codes=[],
        data_tier="demo",
    )
    db.add(fc)
    db.add(Bookmaker(bookmaker_id="dk", name="DraftKings"))
    db.flush()
    db.add(
        OddsTick(
            tick_id="t1",
            outcome_id="o1",
            bookmaker_id="dk",
            price_american=180,
            captured_at=datetime(2026, 11, 6, tzinfo=UTC),
            source_at=datetime(2026, 11, 6, tzinfo=UTC),
            provider="seed",
            provider_record_id="t1",
            observed_at=datetime(2026, 11, 6, tzinfo=UTC),
            effective_at=datetime(2026, 11, 6, tzinfo=UTC),
        )
    )
    db.add(
        Recommendation(
            forecast_id="f1",
            odds_tick_id="t1",
            ev=0.2,
            ev_after_haircut=0.15,
            threshold=0.04,
            status="play",
            reason_codes=["DEMO"],
            data_tier="demo",
        )
    )
    db.commit()


def test_slate_hides_historical_games(db):
    _seed_demo_and_historical(db)
    slate = games_for_slate(db, None, "nfl")
    assert [g.game_id for g in slate] == []
    all_games = games_for_slate(db, None, "nfl", include_historical=True)
    assert {g.game_id for g in all_games} == {"nfl:2023_01_AAA_BBB"}


def test_slate_hides_espn_copy_of_the_same_game(db):
    _seed_demo_and_historical(db)
    kick = datetime(2026, 11, 8, 18, tzinfo=UTC)
    db.add(
        Venue(
            venue_id="v2",
            name="Other Field",
            lat=1,
            lon=1,
            roof_type="open",
            surface="grass",
        )
    )
    db.add_all(
        [
            Game(
                game_id="nfl:2026_09_LA_WAS",
                season_id="nfl-2023",
                league_id="nfl",
                home_team_id="nfl:AAA",
                away_team_id="nfl:BBB",
                kickoff_utc=kick,
                venue_id="v1",
                data_tier="upcoming",
                status="scheduled",
            ),
            Game(
                game_id="espn:nfl:401873043",
                season_id="nfl-2023",
                league_id="nfl",
                home_team_id="nfl:AAA",
                away_team_id="nfl:BBB",
                kickoff_utc=kick,
                venue_id="v1",
                data_tier="upcoming",
                status="scheduled",
            ),
            Game(
                game_id="espn:nfl:999",
                season_id="nfl-2023",
                league_id="nfl",
                home_team_id="nfl:AAA",
                away_team_id="nfl:BBB",
                kickoff_utc=kick,
                venue_id="v2",
                data_tier="upcoming",
                status="scheduled",
            ),
        ]
    )
    db.commit()
    slate = games_for_slate(db, "2026-11-08", "nfl")
    assert {g.game_id for g in slate} == {"nfl:2026_09_LA_WAS", "espn:nfl:999"}


def test_demo_play_is_not_a_validated_play(db):
    _seed_demo_and_historical(db)
    fc = db.get(Forecast, "f1")
    payload = forecast_payload(db, fc, None)
    assert payload["data_tier"] == "demo"
    assert payload["recommendation_status"] == "demo_play"
    assert "DEMO_NOT_VALIDATED" in payload["reason_codes"]


def test_recommend_engine_still_abstains_without_price():
    result = recommend(
        RecommendInput(
            p=0.5,
            p_low=0.4,
            p_high=0.6,
            american=None,
            price_captured_at=None,
            game_status="scheduled",
            availability_status="probable",
            availability_prob=0.9,
            injury_confidence=0.9,
            source_fresh=True,
            grading_supported=True,
            calibration_n=200,
        )
    )
    assert result.status == "no_bet"
    assert "NO_CURRENT_PRICE" in result.reason_codes
