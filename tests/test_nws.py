from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from spe.adapters.base import AdapterError
from spe.adapters.nws import assemble_conditions
from spe.db.models import Game, Venue, WeatherSnapshot
from spe.ingest.nflverse_historical import ingest_nfl_history
from spe.ingest.nws_live import backfill_venue_coordinates, ingest_nws_forecasts
from spe.ingest.stadiums import lookup_stadium


def test_renamed_college_stadiums_resolve_to_one_site():
    from spe.ingest.open_meteo_history import _choose_venue_records, _index_venues

    records = [
        {"name": "Memorial Stadium", "city": "Columbia", "state": "MO", "latitude": 38.93, "longitude": -92.33, "grass": False, "dome": False},
        {"name": "Memorial Stadium", "city": "Lawrence", "state": "KS", "latitude": 38.96, "longitude": -95.24, "grass": False, "dome": False},
        {"name": "Memorial Stadium (Champaign, IL)", "city": "Champaign", "state": "IL", "latitude": 40.09, "longitude": -88.23, "grass": False, "dome": False},
        {"name": "Arizona Stadium", "city": "Tucson", "state": "AZ", "latitude": 32.22, "longitude": -110.94, "grass": False, "dome": False},
        {"name": "Husky Stadium", "city": "Seattle", "state": "WA", "latitude": 47.65, "longitude": -122.30, "grass": False, "dome": False},
        {"name": "Husky Stadium", "city": "Houston", "state": "TX", "latitude": 29.69, "longitude": -95.51, "grass": False, "dome": False},
    ]
    indexed = _index_venues(records)
    illinois = _choose_venue_records(indexed, "Gies Memorial Stadium", {"illinois"})
    arizona = _choose_venue_records(indexed, "Casino Del Sol Stadium", {"arizona"})
    missouri = _choose_venue_records(indexed, "Memorial Stadium", {"missouri"})
    washington = _choose_venue_records(indexed, "Husky Stadium", {"washington"})
    assert [round(row["latitude"], 2) for row in illinois] == [40.09]
    assert [round(row["latitude"], 2) for row in arizona] == [32.22]
    assert [round(row["latitude"], 2) for row in missouri] == [38.93]
    assert [round(row["latitude"], 2) for row in washington] == [47.65]


def test_stadium_lookup_uses_name_over_wrong_id():
    arrowhead = lookup_stadium("KAN00", "GEHA Field at Arrowhead Stadium")
    assert arrowhead is not None
    assert abs(arrowhead.lat - 39.0489) < 0.01
    neutral = lookup_stadium("JAX00", "Tottenham Hotspur Stadium")
    home = lookup_stadium("JAX00", "EverBank Stadium")
    assert neutral is not None and home is not None
    assert abs(neutral.lat - home.lat) > 1


def test_nws_assemble_kickoff_hour_and_drops_expired_alerts():
    periods = [
        {
            "startTime": "2026-11-08T12:00:00-06:00",
            "endTime": "2026-11-08T13:00:00-06:00",
            "temperature": 48,
            "temperatureUnit": "F",
            "windSpeed": "12 mph",
            "windGust": "22 mph",
            "windDirection": "NW",
            "probabilityOfPrecipitation": {"value": 40},
            "relativeHumidity": {"value": 80},
            "shortForecast": "Chance Rain",
        }
    ]
    alerts = [
        {
            "event": "Wind Advisory",
            "headline": "Wind through Sunday",
            "severity": "Moderate",
            "urgency": "Expected",
            "onset": "2026-11-08T12:00:00Z",
            "ends": "2026-11-08T23:00:00Z",
        },
        {
            "event": "Frost Advisory",
            "headline": "Expired Friday",
            "severity": "Minor",
            "onset": "2026-11-06T20:00:00Z",
            "ends": "2026-11-07T14:00:00Z",
        },
    ]
    kick = datetime(2026, 11, 8, 18, 30, tzinfo=UTC)
    parsed = assemble_conditions(
        periods,
        alerts,
        kick,
        observed_at=datetime(2026, 11, 6, 22, tzinfo=UTC),
    )
    assert parsed is not None
    assert abs(parsed["temperature_c"] - (48 - 32) * 5 / 9) < 0.01
    assert abs(parsed["wind_mps"] - 12 * 0.44704) < 0.01
    assert parsed["gust_mps"] is not None
    assert parsed["wind_dir_deg"] == 315
    assert parsed["precip_mm"] is None
    assert parsed["precip_probability"] == 0.4
    assert [a["event"] for a in parsed["alerts"]] == ["Wind Advisory"]
    assert assemble_conditions(periods, [], datetime(2026, 12, 1, tzinfo=UTC)) is None


class _Bundle:
    def __init__(self) -> None:
        self.calls: list[tuple[float, float]] = []

    def fetch_bundle(self, lat: float, lon: float):
        self.calls.append((lat, lon))
        if abs(lat - 40.45) < 0.1:
            raise AdapterError("NWS coverage unavailable")
        return (
            [
                {
                    "startTime": "2026-11-08T12:00:00-06:00",
                    "endTime": "2026-11-08T13:00:00-06:00",
                    "temperature": 50,
                    "temperatureUnit": "F",
                    "windSpeed": "10 mph",
                    "windDirection": "S",
                    "probabilityOfPrecipitation": {"value": 20},
                    "shortForecast": "Mostly Cloudy",
                }
            ],
            [
                {
                    "event": "Wind Advisory",
                    "headline": "Sunday wind",
                    "severity": "Moderate",
                    "onset": "2026-11-08T12:00:00Z",
                    "ends": "2026-11-09T00:00:00Z",
                }
            ],
        )


def test_nws_ingest_stores_forecast_and_skips_gaps(db):
    now = datetime(2026, 11, 6, 22, tzinfo=UTC)
    db.add_all(
        [
            Venue(
                venue_id="arrowhead",
                name="GEHA Field at Arrowhead Stadium",
                lat=39.0489,
                lon=-94.4839,
                roof_type="open",
                surface="grass",
            ),
            Venue(
                venue_id="bernabeu",
                name="Bernabeu",
                lat=40.4531,
                lon=-3.6883,
                roof_type="open",
                surface="grass",
            ),
            Venue(
                venue_id="unknown",
                name="Mystery Bowl",
                lat=0,
                lon=0,
                roof_type="open",
                surface="grass",
            ),
        ]
    )
    from spe.db.models import League, Season, Sport, Team

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
            end_at=datetime(2027, 2, 15, tzinfo=UTC),
        )
    )
    db.add_all(
        [
            Team(team_id="nfl:KC", league_id="nfl", name="Kansas City Chiefs", abbreviation="KC"),
            Team(team_id="nfl:NYJ", league_id="nfl", name="New York Jets", abbreviation="NYJ"),
            Team(team_id="nfl:ATL", league_id="nfl", name="Atlanta Falcons", abbreviation="ATL"),
            Team(team_id="nfl:CIN", league_id="nfl", name="Cincinnati Bengals", abbreviation="CIN"),
        ]
    )
    db.flush()
    kick = datetime(2026, 11, 8, 18, 30, tzinfo=UTC)
    db.add_all(
        [
            Game(
                game_id="real-kc",
                season_id="nfl-2026",
                league_id="nfl",
                home_team_id="nfl:KC",
                away_team_id="nfl:NYJ",
                kickoff_utc=kick,
                venue_id="arrowhead",
                status="scheduled",
                data_tier="upcoming",
            ),
            Game(
                game_id="real-mad",
                season_id="nfl-2026",
                league_id="nfl",
                home_team_id="nfl:ATL",
                away_team_id="nfl:CIN",
                kickoff_utc=kick,
                venue_id="bernabeu",
                status="scheduled",
                data_tier="upcoming",
            ),
            Game(
                game_id="real-mystery",
                season_id="nfl-2026",
                league_id="nfl",
                home_team_id="nfl:KC",
                away_team_id="nfl:ATL",
                kickoff_utc=kick,
                venue_id="unknown",
                status="scheduled",
                data_tier="upcoming",
            ),
        ]
    )
    db.commit()
    bundle = _Bundle()
    result = ingest_nws_forecasts(db, now=now, adapter=bundle, min_interval=timedelta(0))  # type: ignore[arg-type]
    db.commit()
    assert result["forecasts_upserted"] == 1
    assert result["skipped_outside_coverage"] == 1
    assert result["skipped_no_coordinates"] == 1
    assert result["alerts"] == 1
    row = db.query(WeatherSnapshot).filter_by(game_id="real-kc").one()
    assert row.quality_status == "nws_forecast"
    assert row.provider == "nws"
    assert row.precip_mm is None
    assert row.precip_probability == 0.2
    assert row.summary == "Mostly Cloudy"
    assert row.alerts[0]["event"] == "Wind Advisory"
    again = ingest_nws_forecasts(db, now=now, adapter=bundle, min_interval=timedelta(hours=2))  # type: ignore[arg-type]
    assert again["forecasts_upserted"] == 0
    assert again["skipped_fresh"] == 1


def test_nflverse_upcoming_game_gets_coordinates_and_board_tier(db):
    observed = datetime(2026, 11, 6, 18, tzinfo=UTC)
    schedules = pd.DataFrame(
        [
            {
                "game_id": "2026_09_NYJ_KC",
                "season": 2026,
                "week": 9,
                "gameday": "2026-11-08",
                "gametime": "13:00",
                "home_team": "KC",
                "away_team": "NYJ",
                "stadium_id": "KAN00",
                "stadium": "GEHA Field at Arrowhead Stadium",
                "roof": "outdoors",
                "surface": "grass",
                "home_score": None,
                "away_score": None,
            }
        ]
    )
    players = pd.DataFrame([{"gsis_id": "00-1", "display_name": "P", "position": "QB"}])
    result = ingest_nfl_history(
        db,
        seasons=[2026],
        schedules=schedules,
        players=players,
        weekly={2026: pd.DataFrame([])},
        injuries={2026: pd.DataFrame([])},
        observed_at=observed,
    )
    db.commit()
    assert result["games_upserted"] == 1
    game = db.get(Game, "nfl:2026_09_NYJ_KC")
    assert game is not None
    assert game.status == "scheduled"
    assert game.data_tier == "upcoming"
    venue = db.get(Venue, game.venue_id)
    assert venue is not None
    assert abs(venue.lat - 39.0489) < 0.02
    blank = Venue(
        venue_id="blank",
        name="Lambeau Field",
        lat=0,
        lon=0,
        roof_type="open",
        surface="grass",
    )
    db.add(blank)
    db.commit()
    assert backfill_venue_coordinates(db) == 1
    assert abs(blank.lat - 44.5013) < 0.02
