"""A forecast is rejected when the player is not on either team."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from spe.config import settings
from spe.db.models import (
    Forecast,
    Game,
    League,
    Market,
    ModelVersion,
    OddsTick,
    Outcome,
    Player,
    Roster,
    RosterWeek,
    Season,
    Sport,
    Team,
    Venue,
)
from spe.db.base import as_utc
from spe.ingest.model_forecasts import publish_model_forecasts
from spe.integrity.assignments import audit_published_forecasts, quarantine_conflicts


def _artifact() -> None:
    path = settings.parquet_dir / "models" / "nfl_m3.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    market = {
        "league_id": "nfl",
        "market_id": "anytime_td",
        "family": "anytime_td",
        "train_end": "2024-08-01T00:00:00+00:00",
        "epa_scale": "epa",
        "league_rate": 0.2,
        "team_rates": {},
        "pos_rates": {"RB": 0.25},
        "player_rates": {},
        "player_n": {},
        "k_player": 8.0,
        "opp_beta": 0.0,
        "z_mean": 0.0,
        "z_std": 1.0,
        "platt_a": 0.0,
        "platt_b": 1.0,
        "sigma": 1.0,
        "default_line": 0.5,
        "notes": ["test"],
    }
    path.write_text(
        json.dumps(
            {
                "league_id": "nfl",
                "train_end": "2024-08-01T00:00:00+00:00",
                "play_enabled": False,
                "markets": {"anytime_td": market},
            }
        ),
        encoding="utf-8",
    )


def _slate(db, week: int = 9) -> None:
    now = datetime(2026, 11, 1, tzinfo=UTC)
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
    db.add(Venue(venue_id="v", name="Levi's", lat=37.4, lon=-121.9, roof_type="open", surface="grass"))
    db.add_all(
        [
            Team(team_id="nfl:SF", league_id="nfl", name="San Francisco 49ers", abbreviation="SF"),
            Team(team_id="nfl:LV", league_id="nfl", name="Las Vegas Raiders", abbreviation="LV"),
            Team(team_id="nfl:SEA", league_id="nfl", name="Seattle Seahawks", abbreviation="SEA"),
        ]
    )
    db.add(
        Game(
            game_id="g-lv-sf",
            season_id="nfl-2026",
            league_id="nfl",
            home_team_id="nfl:SF",
            away_team_id="nfl:LV",
            kickoff_utc=datetime(2026, 11, 8, 21, tzinfo=UTC),
            venue_id="v",
            status="scheduled",
            week=week,
            data_tier="upcoming",
        )
    )
    db.add(Market(market_id="anytime_td", sport="football", market_type="player_yesno", period="game", stat="td", grading_rule="yes"))
    db.flush()


def test_publish_rejects_another_teams_player_and_a_reordered_name(db):
    _artifact()
    _slate(db)
    now = datetime(2026, 11, 6, tzinfo=UTC)
    db.add_all(
        [
            Player(player_id="nfl:james", name="Jordan James", position_group="RB", position="RB"),
            Player(player_id="nfl:sea", name="Jaxon Smith-Njigba", position_group="WR", position="WR"),
        ]
    )
    db.flush()
    db.add_all(
        [
            Roster(team_id="nfl:SF", player_id="nfl:james", valid_from=datetime(2025, 9, 1, tzinfo=UTC), status="active"),
            Roster(team_id="nfl:SEA", player_id="nfl:sea", valid_from=datetime(2023, 9, 1, tzinfo=UTC), status="active"),
            RosterWeek(
                season_year=2026,
                week=9,
                team_id="nfl:SF",
                player_id="nfl:james",
                provider="nflverse",
                provider_record_id="james-9",
                observed_at=now,
                effective_at=now,
            ),
            RosterWeek(
                season_year=2026,
                week=9,
                team_id="nfl:SEA",
                player_id="nfl:sea",
                provider="nflverse",
                provider_record_id="jsn-9",
                observed_at=now,
                effective_at=now,
            ),
        ]
    )
    db.add_all(
        [
            Outcome(
                outcome_id="reordered",
                market_id="anytime_td",
                game_id="g-lv-sf",
                player_id=None,
                side="yes",
                description="James Jordan",
            ),
            Outcome(
                outcome_id="other-team",
                market_id="anytime_td",
                game_id="g-lv-sf",
                player_id="nfl:sea",
                side="yes",
                description="Jaxon Smith-Njigba",
            ),
        ]
    )
    db.commit()

    result = publish_model_forecasts(db, now)
    db.commit()

    assert result["written"] == 0
    assert result["play_enabled"] is False
    assert result["identity_skipped"] == 1
    assert db.query(Forecast).count() == 0
    reordered = db.get(Outcome, "reordered")
    assert reordered.player_id is None


def test_quarantine_keeps_the_probability_and_the_odds_tick(db):
    _slate(db)
    now = datetime(2026, 11, 6, tzinfo=UTC)
    db.add(Player(player_id="nfl:james", name="Jordan James", position_group="RB", position="RB"))
    db.flush()
    db.add(Roster(team_id="nfl:SF", player_id="nfl:james", valid_from=datetime(2025, 9, 1, tzinfo=UTC), status="active"))
    db.add(
        RosterWeek(
            season_year=2026,
            week=9,
            team_id="nfl:SF",
            player_id="nfl:james",
            provider="nflverse",
            provider_record_id="james-9",
            observed_at=now,
            effective_at=now,
        )
    )
    from spe.db.models import Bookmaker

    db.add(Bookmaker(bookmaker_id="draftkings", name="DraftKings"))
    db.add(
        ModelVersion(
            model_version_id="m3-nfl-anytime_td",
            code_sha="m3",
            training_cutoff=now,
            data_snapshot="test",
            hyperparameters={},
            calibration_version="none",
            league_id="nfl",
            family="anytime_td",
        )
    )
    db.flush()
    db.add(
        Outcome(
            outcome_id="bad",
            market_id="anytime_td",
            game_id="g-lv-sf",
            player_id="nfl:james",
            side="yes",
            description="James Jordan",
        )
    )
    db.flush()
    db.add(
        Forecast(
            forecast_id="bad-fc",
            outcome_id="bad",
            as_of=now,
            model_version_id="m3-nfl-anytime_td",
            p=0.236,
            p_low=0.16,
            p_high=0.32,
            sim_count=0,
            data_cutoff=now,
            availability_status="unknown",
            reason_codes=["RESEARCH_ONLY"],
            components={"mean": 0.27},
            is_official=True,
            data_tier="research",
        )
    )
    db.add(
        OddsTick(
            tick_id="tick-bad",
            outcome_id="bad",
            bookmaker_id="draftkings",
            price_american=175,
            captured_at=now,
            source_at=now,
            provider="odds_api",
            provider_record_id="evt",
            observed_at=now,
            effective_at=now,
        )
    )
    db.commit()

    report = audit_published_forecasts(db)
    assert report["audited"] == 1
    assert report["conflicts"] == 1
    assert report["rows"][0]["problems"] == ["NAME_MISMATCH"]
    flagged = quarantine_conflicts(db, report)
    db.commit()

    assert flagged == 1
    forecast = db.get(Forecast, "bad-fc")
    tick = db.get(OddsTick, "tick-bad")
    assert forecast.p == 0.236
    assert as_utc(forecast.as_of) == now
    assert forecast.is_official is False
    assert "IDENTITY_CONFLICT" in forecast.reason_codes
    assert tick.price_american == 175
    assert db.query(OddsTick).count() == 1


def test_rams_and_washington_share_ids_and_odds_names():
    from types import SimpleNamespace

    from spe.integrity.assignments import event_matches_game, linked_team_ids

    was = SimpleNamespace(team_id="nfl:WAS", league_id="nfl", name="Washington", abbreviation="WAS", aliases=[])
    rams = SimpleNamespace(team_id="nfl:LA", league_id="nfl", name="Los Angeles Rams", abbreviation="LA", aliases=[])
    assert linked_team_ids(was) == {"nfl:WAS", "nfl:WSH"}
    assert linked_team_ids(rams) == {"nfl:LA", "nfl:LAR"}
    game = SimpleNamespace(home_team_id="nfl:WAS", away_team_id="nfl:LA")
    assert event_matches_game(game, {"nfl:WAS": was, "nfl:LA": rams}, "Washington Commanders", "Los Angeles Rams")


def test_odds_event_accepts_state_nickname_variants():
    from types import SimpleNamespace

    from spe.integrity.assignments import event_matches_game

    home = SimpleNamespace(
        team_id="fbs:55",
        league_id="fbs",
        name="Jacksonville State",
        abbreviation="JXST",
        aliases=[{"espn_id": "55", "location": "Jacksonville State", "nick": "Gamecocks"}],
    )
    away = SimpleNamespace(
        team_id="fbs:2534",
        league_id="fbs",
        name="Sam Houston",
        abbreviation="SHSU",
        aliases=[{"espn_id": "2534", "location": "Sam Houston", "nick": "Bearkats"}],
    )
    game = SimpleNamespace(home_team_id="fbs:55", away_team_id="fbs:2534")
    assert event_matches_game(
        game,
        {"fbs:55": home, "fbs:2534": away},
        "Jacksonville State Gamecocks",
        "Sam Houston State Bearkats",
    )
