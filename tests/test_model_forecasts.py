"""Trained-model forecasts append. They do not rewrite history or enable Play."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from spe.config import settings
from spe.db.models import (
    Bookmaker,
    Forecast,
    Game,
    League,
    Market,
    ModelHealthSnapshot,
    ModelVersion,
    OddsTick,
    Outcome,
    Player,
    Recommendation,
    Roster,
    Season,
    Sport,
    Team,
    Venue,
)
from spe.ingest.model_forecasts import publish_model_forecasts
from spe.ingest.seed_guard import seed_block_reason
from spe.models.train import FittedMarket, is_reportable_health, predict_row


def _market(family: str, line: float) -> dict:
    return {
        "league_id": "nfl",
        "market_id": family,
        "family": family,
        "train_end": "2024-08-01T00:00:00+00:00",
        "epa_scale": "epa",
        "league_rate": 20.0,
        "team_rates": {"nfl:KC": 40.0},
        "pos_rates": {"WR": 35.0, "RB": 18.0},
        "player_rates": {"nfl:rice": 70.0 if family != "anytime_td" else 0.45},
        "player_n": {"nfl:rice": 16},
        "k_player": 8.0,
        "opp_beta": 0.0,
        "z_mean": 0.0,
        "z_std": 1.0,
        "platt_a": 0.0,
        "platt_b": 1.0,
        "sigma": 25.0,
        "default_line": line,
        "notes": ["test"],
    }


def _write_artifacts() -> None:
    path = settings.parquet_dir / "models" / "nfl_m3.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "league_id": "nfl",
                "train_end": "2024-08-01T00:00:00+00:00",
                "play_enabled": False,
                "markets": {
                    "anytime_td": _market("anytime_td", 0.5),
                    "receptions": _market("receptions", 4.5),
                    "receiving_yards": _market("receiving_yards", 49.5),
                    "rushing_yards": _market("rushing_yards", 39.5),
                },
            }
        ),
        encoding="utf-8",
    )


def _fitted() -> FittedMarket:
    return FittedMarket(**_market("receiving_yards", 49.5))


def test_posted_line_changes_the_over_probability():
    fitted = _fitted()
    short = predict_row(fitted, "nfl:rice", "nfl:KC", "WR", 0.0, 16, line=49.5)
    long = predict_row(fitted, "nfl:rice", "nfl:KC", "WR", 0.0, 16, line=80.5)
    assert short["line"] == 49.5
    assert long["line"] == 80.5
    assert short["p"] > long["p"]
    default = predict_row(fitted, "nfl:rice", "nfl:KC", "WR", 0.0, 16)
    assert default["line"] == 49.5


def test_health_report_hides_seed_and_prototype_rows():
    assert is_reportable_health("m3 nfl anytime_td; play_enabled=false; epa")
    assert not is_reportable_health("Prototype seed metrics — not a live backtest.")
    assert not is_reportable_health("baseline_empirical_rate; not validated.")
    assert not is_reportable_health("Yes-only and sparse FBS props.")


def test_seed_refuses_once_football_exists(db):
    assert seed_block_reason(db) is None
    db.add(Sport(sport_id="football", name="American football"))
    db.commit()
    assert seed_block_reason(db) == "football sport already exists"
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "infra" / "seed" / "seed.py"
    spec = importlib.util.spec_from_file_location("caliber_seed", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    before = db.query(Forecast).count()
    module.run()
    assert db.query(Forecast).count() == before
    assert db.query(Game).count() == 0


def test_publish_appends_research_forecast_and_leaves_history(db):
    _write_artifacts()
    now = datetime(2026, 11, 6, 18, tzinfo=UTC)
    db.add(Sport(sport_id="football", name="American football"))
    db.flush()
    db.add(League(league_id="nfl", sport_id="football", name="NFL", classification="nfl"))
    db.flush()
    db.add(
        Season(
            season_id="nfl-2026",
            league_id="nfl",
            year=2026,
            start_at=datetime(2026, 9, 1, tzinfo=UTC),
            end_at=datetime(2027, 2, 1, tzinfo=UTC),
        )
    )
    db.add(Venue(venue_id="v", name="Arrowhead", lat=39.0, lon=-94.0, roof_type="open", surface="grass"))
    db.add_all(
        [
            Team(team_id="nfl:KC", league_id="nfl", name="Kansas City Chiefs", abbreviation="KC"),
            Team(team_id="nfl:BUF", league_id="nfl", name="Buffalo Bills", abbreviation="BUF"),
        ]
    )
    db.add(
        Game(
            game_id="g1",
            season_id="nfl-2026",
            league_id="nfl",
            home_team_id="nfl:KC",
            away_team_id="nfl:BUF",
            kickoff_utc=datetime(2026, 11, 8, 18, tzinfo=UTC),
            venue_id="v",
            status="scheduled",
            data_tier="upcoming",
        )
    )
    db.add(Player(player_id="nfl:rice", name="Rashee Rice", position_group="WR", position="WR"))
    db.flush()
    db.add(Roster(team_id="nfl:KC", player_id="nfl:rice", valid_from=now, status="active"))
    db.add_all(
        [
            Market(market_id="receiving_yards", sport="football", market_type="player_over_under", period="game", stat="rec_yards", grading_rule="over"),
            Market(market_id="anytime_td", sport="football", market_type="player_yesno", period="game", stat="td", grading_rule="yes"),
            Bookmaker(bookmaker_id="draftkings", name="DraftKings"),
            ModelVersion(
                model_version_id="slate-td-rate-nfl",
                code_sha="scoring-rate",
                training_cutoff=now,
                data_snapshot="rate",
                hyperparameters={},
                calibration_version="none",
                league_id="nfl",
                family="anytime_td",
            ),
        ]
    )
    db.flush()
    over = Outcome(
        outcome_id="o-over",
        market_id="receiving_yards",
        game_id="g1",
        player_id="nfl:rice",
        side="over",
        line=64.5,
        description="Rashee Rice",
    )
    under = Outcome(
        outcome_id="o-under",
        market_id="receiving_yards",
        game_id="g1",
        player_id="nfl:rice",
        side="under",
        line=64.5,
        description="Rashee Rice",
    )
    atd = Outcome(
        outcome_id="o-atd",
        market_id="anytime_td",
        game_id="g1",
        player_id="nfl:rice",
        side="yes",
        line=None,
        description="Rashee Rice anytime td",
    )
    db.add_all([over, under, atd])
    db.flush()
    old = Forecast(
        forecast_id="old-rate",
        outcome_id="o-atd",
        as_of=datetime(2026, 11, 6, 12, tzinfo=UTC),
        model_version_id="slate-td-rate-nfl",
        p=0.22,
        p_low=0.12,
        p_high=0.32,
        sim_count=0,
        data_cutoff=now,
        availability_status="unknown",
        reason_codes=["SCORING_RATE"],
        components={"games": 4},
        is_official=True,
        data_tier="upcoming",
    )
    db.add(old)
    db.flush()
    db.add(
        Recommendation(
            recommendation_id="old-rec",
            forecast_id="old-rate",
            threshold=0.04,
            status="no_bet",
            reason_codes=["SCORING_RATE"],
            immutable=True,
            data_tier="upcoming",
        )
    )
    tick = OddsTick(
        tick_id="tick-1",
        outcome_id="o-over",
        bookmaker_id="draftkings",
        price_american=-110,
        captured_at=now,
        source_at=now,
        provider="odds_api",
        provider_record_id="evt",
        observed_at=now,
        effective_at=now,
    )
    db.add(tick)
    db.add(
        ModelHealthSnapshot(
            league_id="nfl",
            market_id="anytime_td",
            as_of=now,
            brier=0.19,
            log_loss=0.5,
            ece=0.03,
            n=10,
            coverage=1,
            freshness_minutes=0,
            notes="Prototype seed metrics.",
        )
    )
    db.commit()

    result = publish_model_forecasts(db, now)
    db.commit()

    assert result["written"] == 3
    assert result["play_enabled"] is False
    db.refresh(old)
    db.refresh(tick)
    assert old.p == 0.22
    assert old.model_version_id == "slate-td-rate-nfl"
    assert old.reason_codes == ["SCORING_RATE"]
    assert tick.price_american == -110
    assert db.query(OddsTick).count() == 1
    kept = db.get(Recommendation, "old-rec")
    assert kept is not None
    assert kept.reason_codes == ["SCORING_RATE"]
    assert kept.status == "no_bet"

    fresh = (
        db.query(Forecast)
        .filter(Forecast.outcome_id == "o-over", Forecast.model_version_id == "m3-nfl-receiving_yards")
        .one()
    )
    assert fresh.data_tier == "research"
    assert fresh.is_official is True
    assert fresh.components["line_source"] == "sportsbook"
    assert fresh.components["line"] == 64.5
    assert fresh.components["play_enabled"] is False
    assert "RESEARCH_ONLY" in fresh.reason_codes
    assert "PLAY_DISABLED" in fresh.reason_codes
    rec = db.query(Recommendation).filter(Recommendation.forecast_id == fresh.forecast_id).one()
    assert rec.status == "no_bet"
    assert rec.immutable is True
    assert rec.odds_tick_id == "tick-1"

    under_fc = db.query(Forecast).filter(Forecast.outcome_id == "o-under").one()
    assert abs((under_fc.p + fresh.p) - 1.0) < 1e-6

    again = publish_model_forecasts(db, now)
    db.commit()
    assert again["written"] == 0
    assert again["unchanged"] == 3
    assert db.query(Forecast).filter(Forecast.model_version_id.like("m3-%")).count() == 3
    assert db.query(OddsTick).count() == 1


def test_college_prop_name_links_only_to_that_games_roster(db):
    path = settings.parquet_dir / "models" / "fbs_m3.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    market = _market("receiving_yards", 49.5)
    market["league_id"] = "fbs"
    market["team_rates"] = {"fbs:333": 80.0}
    market["player_rates"] = {"fbs:5141394": 70.0}
    market["player_n"] = {"fbs:5141394": 16}
    path.write_text(
        json.dumps(
            {
                "league_id": "fbs",
                "train_end": "2024-08-01T00:00:00+00:00",
                "play_enabled": False,
                "markets": {"receiving_yards": market},
            }
        ),
        encoding="utf-8",
    )
    now = datetime(2026, 11, 6, 18, tzinfo=UTC)
    db.add(Sport(sport_id="football", name="American football"))
    db.flush()
    db.add(League(league_id="fbs", sport_id="football", name="FBS", classification="ncaa"))
    db.flush()
    db.add(
        Season(
            season_id="fbs-2026",
            league_id="fbs",
            year=2026,
            start_at=datetime(2026, 8, 1, tzinfo=UTC),
            end_at=datetime(2027, 1, 20, tzinfo=UTC),
        )
    )
    db.add(Venue(venue_id="v", name="Tiger Stadium", lat=30.4, lon=-91.2, roof_type="open", surface="grass"))
    db.add_all(
        [
            Team(
                team_id="ala",
                league_id="fbs",
                name="Alabama Crimson Tide",
                abbreviation="ALA",
                aliases=["ALA", {"espn_id": "333"}],
            ),
            Team(team_id="lsu", league_id="fbs", name="LSU Tigers", abbreviation="LSU", aliases=[{"espn_id": "99"}]),
            Team(team_id="fbs:333", league_id="fbs", name="Alabama", abbreviation="ALA"),
            Team(team_id="fbs:99", league_id="fbs", name="LSU", abbreviation="LSU"),
            Player(player_id="fbs:5141394", name="Lotzeir Brooks", position_group="WR", position="WR"),
            Player(player_id="fbs:1", name="Kaleb Edwards", position_group="TE", position="TE"),
            Player(player_id="fbs:2", name="Kaleb Edwards", position_group="DB", position="DB"),
        ]
    )
    db.flush()
    db.add_all(
        [
            Roster(team_id="fbs:333", player_id="fbs:5141394", valid_from=now, status="active"),
            Roster(team_id="fbs:333", player_id="fbs:1", valid_from=now, status="active"),
            Roster(team_id="fbs:99", player_id="fbs:2", valid_from=now, status="active"),
            Market(
                market_id="receiving_yards",
                sport="football",
                market_type="player_over_under",
                period="game",
                stat="rec_yards",
                grading_rule="over",
            ),
        ]
    )
    db.add(
        Game(
            game_id="espn:fbs:1",
            season_id="fbs-2026",
            league_id="fbs",
            home_team_id="lsu",
            away_team_id="ala",
            kickoff_utc=datetime(2026, 11, 7, 20, tzinfo=UTC),
            venue_id="v",
            status="scheduled",
            data_tier="upcoming",
        )
    )
    db.flush()
    linked = Outcome(
        outcome_id="yards-linked",
        market_id="receiving_yards",
        game_id="espn:fbs:1",
        player_id=None,
        side="over",
        line=44.5,
        description="Lotzeir Brooks",
    )
    ambiguous = Outcome(
        outcome_id="yards-ambiguous",
        market_id="receiving_yards",
        game_id="espn:fbs:1",
        player_id=None,
        side="over",
        line=13.5,
        description="Kaleb Edwards",
    )
    db.add_all([linked, ambiguous])
    db.commit()

    result = publish_model_forecasts(db, now)
    db.commit()

    db.refresh(linked)
    db.refresh(ambiguous)
    assert linked.player_id == "fbs:5141394"
    assert ambiguous.player_id is None
    assert result["written"] == 1
    assert result["skipped"] == 1
    assert result["play_enabled"] is False
    forecast = db.query(Forecast).filter(Forecast.outcome_id == "yards-linked").one()
    assert forecast.model_version_id == "m3-fbs-receiving_yards"
    assert forecast.data_tier == "research"
    assert forecast.components["line"] == 44.5
    assert forecast.components["line_source"] == "sportsbook"
    rec = db.query(Recommendation).one()
    assert rec.status == "no_bet"
