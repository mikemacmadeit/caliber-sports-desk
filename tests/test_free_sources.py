from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from spe.config import settings
from spe.db.models import (
    DepthChartWeek,
    Game,
    InjuryEvent,
    NewsArticle,
    NgsWeek,
    ParticipationPlay,
    ParticipationPlayerGame,
    WeatherSnapshot,
)
from spe.ingest.gdelt_news import ingest_news_records, parse_seen
from spe.ingest.nflverse_extra import (
    game_available_at,
    ingest_depth_frame,
    ingest_ngs_frame,
    ingest_participation_frame,
    week_available_at,
)
from spe.ingest.noaa_cdo import ingest_noaa_observations
from spe.ingest.open_meteo_history import forecast_row, ingest_historical_forecasts
from spe.analytics.usage import compute_usage
from spe.db.base import as_utc
from spe.ingest.nflverse_historical import ingest_nfl_history


def _game(db):
    observed = datetime(2024, 10, 1, tzinfo=UTC)
    ingest_nfl_history(
        db,
        seasons=[2024],
        schedules=pd.DataFrame(
            [
                {
                    "game_id": "2024_01_BUF_NYJ",
                    "season": 2024,
                    "week": 1,
                    "gameday": "2024-09-09",
                    "gametime": "20:15",
                    "home_team": "NYJ",
                    "away_team": "BUF",
                    "stadium": "MetLife Stadium",
                    "roof": "open",
                    "surface": "turf",
                    "home_score": 10,
                    "away_score": 20,
                },
                {
                    "game_id": "2024_02_BUF_MIA",
                    "season": 2024,
                    "week": 2,
                    "gameday": "2024-09-16",
                    "gametime": "20:15",
                    "home_team": "MIA",
                    "away_team": "BUF",
                    "stadium": "Hard Rock Stadium",
                    "roof": "open",
                    "surface": "grass",
                    "home_score": 10,
                    "away_score": 17,
                },
            ]
        ),
        players=pd.DataFrame(
            [{"gsis_id": "00-0034796", "display_name": "Josh Allen", "position": "QB", "position_group": "QB"}]
        ),
        weekly={2024: pd.DataFrame()},
        injuries={2024: pd.DataFrame()},
        observed_at=observed,
    )
    db.flush()


def test_participation_counts_snaps_and_does_not_assign_routes(db):
    _game(db)
    frame = pd.DataFrame(
        [
            {
                "nflverse_game_id": "2024_01_BUF_NYJ",
                "play_id": 1,
                "possession_team": "BUF",
                "offense_formation": "SHOTGUN",
                "offense_personnel": "1 RB, 1 TE, 3 WR",
                "defense_personnel": None,
                "defenders_in_box": 6,
                "number_of_pass_rushers": 4,
                "n_offense": 2,
                "n_defense": 1,
                "offense_players": "00-0034796;00-0030001",
                "defense_players": "00-0099999",
                "offense_names": "Josh Allen;Rookie Wideout",
                "defense_names": "Unknown Corner",
                "offense_positions": "QB;WR",
                "defense_positions": "CB",
                "route": "GO",
                "defense_man_zone_type": "ZONE_COVERAGE",
                "defense_coverage_type": "COVER_3",
                "was_pressure": False,
            },
            {
                "nflverse_game_id": "2024_01_BUF_NYJ",
                "play_id": 2,
                "possession_team": "BUF",
                "offense_formation": "SHOTGUN",
                "offense_personnel": None,
                "defense_personnel": None,
                "defenders_in_box": 7,
                "number_of_pass_rushers": 4,
                "n_offense": 1,
                "n_defense": 0,
                "offense_players": "00-0034796",
                "defense_players": "",
                "offense_names": "Josh Allen",
                "defense_names": "",
                "offense_positions": "QB",
                "defense_positions": "",
                "route": "",
                "defense_man_zone_type": "",
                "defense_coverage_type": None,
                "was_pressure": None,
            },
        ]
    )
    result = ingest_participation_frame(db, frame, year=2024)
    db.flush()
    assert result["plays"] == 2
    play = db.query(ParticipationPlay).filter_by(play_id="1").one()
    assert play.route == "GO"
    assert play.possession_team == "BUF"
    assert db.query(ParticipationPlay).filter_by(play_id="2").one().route is None
    snaps = db.query(ParticipationPlayerGame).filter_by(player_id="nfl:00-0034796").one()
    assert snaps.offense_snaps == 2
    assert snaps.quality_status == "derived_from_player_lists"
    assert not hasattr(snaps, "route") or getattr(snaps, "route", None) is None
    game = db.get(Game, "nfl:2024_01_BUF_NYJ")
    assert as_utc(snaps.effective_at) > as_utc(game.kickoff_utc)


def test_dated_depth_chart_uses_the_file_timestamp(db):
    _game(db)
    written = ingest_depth_frame(
        db,
        pd.DataFrame(
            [
                {
                    "dt": "2024-09-08T10:00:00Z",
                    "team": "BUF",
                    "player_name": "Josh Allen",
                    "gsis_id": "00-0034796",
                    "pos_grp": "Base 3WR",
                    "pos_abb": "QB",
                    "pos_slot": 1,
                    "pos_rank": 1,
                }
            ]
        ),
        year=2024,
        observed_at=datetime(2024, 10, 1, tzinfo=UTC),
    )
    assert written["rows"] == 1
    row = db.query(DepthChartWeek).one()
    assert row.quality_status == "snapshot_timestamp"
    assert row.depth_rank == 1
    assert as_utc(row.effective_at) == datetime(2024, 9, 8, 10, tzinfo=UTC)
    game = db.get(Game, "nfl:2024_01_BUF_NYJ")
    assert as_utc(row.effective_at) < as_utc(game.kickoff_utc)


def test_week_chart_is_not_available_at_kickoff(db):
    _game(db)
    game = db.get(Game, "nfl:2024_01_BUF_NYJ")
    later = db.get(Game, "nfl:2024_02_BUF_MIA")
    available = week_available_at(game.kickoff_utc)
    assert available > as_utc(game.kickoff_utc)
    assert available < as_utc(later.kickoff_utc)
    assert game_available_at(game.kickoff_utc) > as_utc(game.kickoff_utc)


def test_ngs_week_zero_is_not_a_pregame_feature(db):
    _game(db)
    frame = pd.DataFrame(
        [
            {
                "season": 2024,
                "season_type": "REG",
                "week": 0,
                "player_display_name": "Josh Allen",
                "player_position": "QB",
                "team_abbr": "BUF",
                "player_gsis_id": "00-0034796",
                "completion_percentage_above_expectation": 9.0,
            },
            {
                "season": 2024,
                "season_type": "REG",
                "week": 1,
                "player_display_name": "Josh Allen",
                "player_position": "QB",
                "team_abbr": "BUF",
                "player_gsis_id": "00-0034796",
                "completion_percentage_above_expectation": 4.0,
            },
        ]
    )
    ingest_ngs_frame(db, {"passing": frame}, seasons=[2024])
    week0 = db.query(NgsWeek).filter_by(week=0).one()
    assert week0.quality_status == "season_aggregate_not_pregame"
    for external in ("2024_01_BUF_NYJ", "2024_02_BUF_MIA"):
        ingest_participation_frame(
            db,
            pd.DataFrame(
                [
                    {
                        "nflverse_game_id": external,
                        "play_id": 1,
                        "possession_team": "BUF",
                        "offense_formation": "SHOTGUN",
                        "offense_players": "00-0034796",
                        "defense_players": "",
                        "offense_names": "Josh Allen",
                        "offense_positions": "QB",
                        "route": "",
                        "defense_man_zone_type": "MAN_COVERAGE",
                    }
                ]
            ),
            year=2024,
        )
    depth = pd.DataFrame(
        [
            {
                "season": 2024,
                "week": 1,
                "club_code": "BUF",
                "game_type": "REG",
                "depth_team": "1",
                "formation": "Offense",
                "gsis_id": "00-0034796",
                "position": "QB",
                "depth_position": "QB",
                "full_name": "Josh Allen",
            }
        ]
    )
    ingest_depth_frame(db, depth, year=2024)
    usage = compute_usage(db)
    assert usage["player_rows"] >= 1
    from spe.db.models import PlayerAnalytics

    assert db.query(PlayerAnalytics).filter_by(game_id="nfl:2024_01_BUF_NYJ", source="caliber_usage_v1").count() == 0
    week2 = db.query(PlayerAnalytics).filter_by(game_id="nfl:2024_02_BUF_MIA", source="caliber_usage_v1").one()
    assert week2.metrics["ngs_completion_percentage_above_expectation"] == 4.0
    assert week2.metrics["starter_prior_week"] is True


def test_news_is_not_an_injury_event(db):
    before = db.query(InjuryEvent).count()
    outcome = ingest_news_records(
        db,
        [
            {
                "url": "https://example.com/allen-questionable",
                "title": "Allen listed as questionable",
                "domain": "example.com",
                "language": "English",
                "sourcecountry": "US",
                "seendate": "20240908120000",
            },
            {"url": "https://example.com/no-time", "title": "missing timestamp", "seendate": ""},
        ],
        league_id="nfl",
        theme="injury_news",
        query='"NFL" injury',
    )
    db.flush()
    assert outcome["rows"] == 1
    assert db.query(InjuryEvent).count() == before
    article = db.query(NewsArticle).one()
    assert article.verified_injury is False
    assert article.quality_status == "unverified_news"
    assert as_utc(article.effective_at) == parse_seen("20240908120000")


def test_forecast_issue_time_is_twenty_four_hours_before_the_hour(db):
    _game(db)
    game = db.get(Game, "nfl:2024_01_BUF_NYJ")
    from spe.db.models import Venue

    venue = db.get(Venue, game.venue_id)
    kick = game.kickoff_utc.replace(tzinfo=UTC).replace(minute=0, second=0, microsecond=0)
    series = {kick.strftime("%Y-%m-%dT%H:%M"): {"temperature_c": 18.0, "wind_mps": 4.0, "gust_mps": None, "precip_mm": 0.0, "wind_dir_deg": 180.0, "humidity": 60.0}}
    row = forecast_row(game, venue, series, observed_at=datetime(2024, 9, 8, tzinfo=UTC))
    assert row is not None
    assert row["issued_at"] == row["forecast_for"] - timedelta(hours=24)
    assert row["effective_at"] == row["issued_at"]
    assert row["quality_status"] == "forecast_lead_24h"
    assert row["temperature_c"] == 18.0
    empty = forecast_row(
        game,
        venue,
        {kick.strftime("%Y-%m-%dT%H:%M"): {"temperature_c": None, "wind_mps": None, "gust_mps": None, "precip_mm": None, "wind_dir_deg": None, "humidity": None}},
        observed_at=datetime(2024, 9, 8, tzinfo=UTC),
    )
    assert empty is None


def test_open_meteo_disabled_writes_nothing(db, monkeypatch):
    monkeypatch.setattr(settings, "open_meteo_enabled", False)
    result = ingest_historical_forecasts(db, venue_records=[])
    assert result["rows"] == 0
    assert result["status"] == "disabled"
    assert db.query(WeatherSnapshot).count() == 0


def test_noaa_uses_nearest_station_and_waits_until_local_midnight(db, monkeypatch):
    _game(db)
    game = db.get(Game, "nfl:2024_01_BUF_NYJ")
    from spe.db.models import Venue

    venue = db.get(Venue, game.venue_id)
    venue.lat = 40.81
    venue.lon = -74.07
    venue.timezone = "America/New_York"
    venue.roof_type = "open"
    db.flush()
    monkeypatch.setattr(settings, "noaa_cdo_token", "test-token")

    class Stub:
        def get(self, path, params):
            if path == "/stations":
                return {"results": [{"id": "GHCND:USW00014732", "latitude": 40.81, "longitude": -74.07}]}
            return {
                "metadata": {"resultset": {"count": 2}},
                "results": [
                    {"date": "2024-09-09T00:00:00", "datatype": "TMAX", "value": 250},
                    {"date": "2024-09-09T00:00:00", "datatype": "TMIN", "value": 150},
                ],
            }

    result = ingest_noaa_observations(db, adapter=Stub())
    assert result["rows"] == 1
    row = db.query(WeatherSnapshot).filter_by(provider="noaa_cdo").one()
    assert row.quality_status == "daily_observation_not_forecast"
    assert row.temperature_c == 20.0
    assert row.wind_mps is None
    assert as_utc(row.effective_at) > as_utc(game.kickoff_utc)
    assert str(row.summary).startswith("GHCND:USW00014732")


def test_noaa_without_token_writes_nothing(db, monkeypatch):
    monkeypatch.setattr(settings, "noaa_cdo_token", "")

    class Stub:
        def probe_without_token(self):
            return "HTTP 400: Token parameter is required."

    result = ingest_noaa_observations(db, adapter=Stub())
    assert result["status"] == "needs_key"
    assert result["rows"] == 0
    assert db.query(WeatherSnapshot).filter_by(provider="noaa_cdo").count() == 0
