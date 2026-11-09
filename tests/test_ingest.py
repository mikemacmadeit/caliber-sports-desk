from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from spe.db.models import Game, Player, PlayerGameStat, Play, WeatherSnapshot
from spe.ingest.catalog import catalog_payload
from spe.ingest.cfbd_historical import ingest_fbs_history
from spe.ingest.coverage import coverage_report
from spe.ingest.nflverse_historical import ingest_nfl_history
from spe.ingest.odds_historical import ingest_odds_history
from spe.ingest.weather_historical import record_weather_coverage


def test_catalog_distinguishes_available_from_paid():
    assets = {a["asset"]: a["availability"] for a in catalog_payload()}
    assert assets["nfl_schedules_1999_present"] == "available"
    assert assets["nfl_player_week_stats"] == "available"
    assert assets["fbs_games_teams_rosters_plays_ppa"] == "needs_key"
    assert assets["live_stadium_forecast_alerts"] == "available"
    assert assets["historical_player_prop_odds"] == "paid_plan"
    assert assets["route_participation_charting"] == "requires_contract"
    assert assets["nfl_tracking_ngs"] == "requires_contract"


def test_nflverse_fixture_round_trip(db):
    observed = datetime(2024, 2, 1, tzinfo=UTC)
    schedules = pd.DataFrame(
        [
            {
                "game_id": "2023_01_BUF_NYJ",
                "season": 2023,
                "week": 1,
                "gameday": "2023-09-11",
                "gametime": "20:15",
                "home_team": "NYJ",
                "away_team": "BUF",
                "stadium": "MetLife Stadium",
                "roof": "open",
                "surface": "turf",
                "home_score": 22,
                "away_score": 16,
                "temp": 70,
                "wind": 8,
            }
        ]
    )
    players = pd.DataFrame(
        [
            {
                "gsis_id": "00-0034796",
                "display_name": "Josh Allen",
                "position": "QB",
                "position_group": "QB",
            }
        ]
    )
    weekly = pd.DataFrame(
        [
            {
                "player_id": "00-0034796",
                "player_display_name": "Josh Allen",
                "position": "QB",
                "season": 2023,
                "week": 1,
                "recent_team": "BUF",
                "attempts": 41,
                "targets": 0,
                "receptions": 0,
                "rushing_attempts": 6,
                "rushing_yards": 14,
                "receiving_yards": 0,
                "rushing_tds": 1,
                "receiving_tds": 0,
                "passing_tds": 1,
            }
        ]
    )
    pbp = pd.DataFrame(
        [
            {
                "game_id": "2023_01_BUF_NYJ",
                "play_id": "101",
                "qtr": 1,
                "time": "12:34",
                "down": 1,
                "ydstogo": 10,
                "yardline_100": 65,
                "posteam": "BUF",
                "defteam": "NYJ",
                "play_type": "pass",
                "yards_gained": 12,
                "epa": 0.4,
                "posteam_score": 0,
                "defteam_score": 0,
                "desc": "Allen pass complete",
                "ftn_charting_ignored": 1,
            }
        ]
    )
    result = ingest_nfl_history(
        db,
        seasons=[2023],
        include_pbp=True,
        schedules=schedules,
        players=players,
        weekly={2023: weekly},
        pbp={2023: pbp},
        injuries={2023: pd.DataFrame([])},
        observed_at=observed,
    )
    db.commit()
    assert result["games_upserted"] == 1
    game = db.get(Game, "nfl:2023_01_BUF_NYJ")
    assert game is not None
    assert game.data_tier == "historical"
    assert game.league_id == "nfl"
    assert game.status == "final"
    player = db.get(Player, "nfl:00-0034796")
    assert player is not None
    stat = (
        db.query(PlayerGameStat)
        .filter_by(game_id=game.game_id, player_id=player.player_id)
        .one()
    )
    assert stat.rush_td == 1
    assert stat.pass_td == 1
    assert stat.quality_status == "partial_no_charting"
    play = db.query(Play).one()
    assert play.epa == 0.4
    weather = db.query(WeatherSnapshot).one()
    assert weather.quality_status == "observed_not_forecast"

    again = ingest_nfl_history(
        db,
        seasons=[2023],
        include_pbp=True,
        schedules=schedules,
        players=players,
        weekly={2023: weekly},
        pbp={2023: pbp},
        injuries={2023: pd.DataFrame([])},
        observed_at=observed,
    )
    assert again["games_upserted"] == 0
    assert db.query(Game).filter_by(data_tier="historical").count() == 1


def test_cfbd_without_key_records_gap(db):
    result = ingest_fbs_history(db, seasons=[2019])
    db.commit()
    assert result["status"] == "needs_key"
    report = coverage_report(db)
    games = [r for r in report["coverage"] if r["dataset"] == "games" and r["league_id"] == "fbs"]
    assert games
    assert games[0]["status"] == "needs_key"


def test_cfbd_fixture_ingests_fbs_game(db):
    fixtures = {
        "teams": [{"id": 194, "school": "Ohio State", "abbreviation": "OSU", "conference": "Big Ten"}],
        "games:2019": [
            {
                "id": 401112148,
                "season": 2019,
                "week": 12,
                "homeTeam": "Ohio State",
                "awayTeam": "Penn State",
                "homeId": 194,
                "awayId": 213,
                "startDate": "2019-11-23T17:00:00.000Z",
                "venue": "Ohio Stadium",
                "neutralSite": False,
                "homeConference": "Big Ten",
                "awayConference": "Big Ten",
                "homePoints": 28,
                "awayPoints": 17,
            }
        ],
        "roster:2019": [
            {
                "id": 8,
                "firstName": "Justin",
                "lastName": "Fields",
                "position": "QB",
                "teamId": 194,
            }
        ],
        "ppa_players:2019": [],
    }
    result = ingest_fbs_history(db, seasons=[2019], fixtures=fixtures)
    db.commit()
    assert result["games_upserted"] == 1
    game = db.get(Game, "fbs:401112148")
    assert game is not None
    assert game.data_tier == "historical"
    assert game.league_id == "fbs"
    player = db.get(Player, "fbs:8")
    assert player.name == "Justin Fields"


def test_odds_and_weather_do_not_fabricate(db):
    odds = ingest_odds_history(db)
    weather = record_weather_coverage(db)
    db.commit()
    assert odds["ticks_upserted"] == 0
    assert odds["status"] == "needs_key"
    report = coverage_report(db)
    prop_rows = [r for r in report["coverage"] if r["dataset"] == "historical_player_props"]
    assert prop_rows
    assert all(r["row_count"] == 0 for r in prop_rows)
    assert weather["historical_forecast_status"] in {"paid_plan", "experimental", "disabled", "not_ingested"}
    assert db.query(WeatherSnapshot).filter_by(quality_status="forecast_lead_24h").count() == 0
