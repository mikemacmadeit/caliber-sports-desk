"""Today's slate stays on the ATS card when the paid line feed is out of credits."""

from datetime import UTC, datetime, timedelta

from spe.db.models import Game, League, Season, Sport, Team, Venue
from spe.ingest.espn_spreads import quotes_from_scoreboard, store_public_spreads
from spe.models.ats.predict import predict_upcoming_ats

NOW = datetime(2026, 11, 21, 21, tzinfo=UTC)


def test_scoreboard_spread_keeps_the_home_favorite_sign():
    payload = {
        "events": [
            {
                "id": "401860920",
                "competitions": [
                    {
                        "odds": [
                            {
                                "provider": {"name": "DraftKings"},
                                "spread": -14,
                                "pointSpread": {
                                    "home": {"close": {"line": "-14", "odds": "-105"}},
                                    "away": {"close": {"line": "+14", "odds": "-115"}},
                                },
                            }
                        ]
                    }
                ],
            }
        ]
    }
    quotes = quotes_from_scoreboard(payload, "fbs")
    assert quotes[0]["game_id"] == "espn:fbs:401860920"
    assert quotes[0]["home_line"] == -14
    assert quotes[0]["away_line"] == 14
    assert quotes[0]["bookmaker_id"] == "draftkings"


def test_a_scheduled_game_is_on_the_card_without_play_by_play(db):
    db.add(Sport(sport_id="football", name="Football"))
    db.flush()
    db.add(League(league_id="fbs", sport_id="football", name="FBS", classification="college"))
    db.add(
        Season(
            season_id="fbs-2026",
            league_id="fbs",
            year=2026,
            start_at=NOW - timedelta(days=80),
            end_at=NOW + timedelta(days=40),
        )
    )
    db.add(Venue(venue_id="v", name="Field", lat=0, lon=0, roof_type="open", surface="grass"))
    db.add_all(
        [
            Team(team_id="txst", league_id="fbs", name="Texas State", abbreviation="TXST"),
            Team(team_id="wsu", league_id="fbs", name="Washington State", abbreviation="WSU"),
        ]
    )
    db.flush()
    db.add(
        Game(
            game_id="espn:fbs:401860920",
            season_id="fbs-2026",
            league_id="fbs",
            home_team_id="txst",
            away_team_id="wsu",
            kickoff_utc=NOW + timedelta(hours=2),
            venue_id="v",
            status="scheduled",
            data_tier="upcoming",
        )
    )
    db.flush()
    quotes = quotes_from_scoreboard(
        {
            "events": [
                {
                    "id": "401860920",
                    "competitions": [
                        {
                            "odds": [
                                {
                                    "provider": {"name": "DraftKings"},
                                    "pointSpread": {
                                        "home": {"close": {"line": "-14", "odds": "-105"}},
                                        "away": {"close": {"line": "+14", "odds": "-115"}},
                                    },
                                }
                            ]
                        }
                    ],
                }
            ]
        },
        "fbs",
    )
    assert store_public_spreads(db, now=NOW, quotes=quotes) == 2

    payload = predict_upcoming_ats(db, league_id="fbs", now=NOW)
    rows = [row for row in payload["rows"] if row["game_id"] == "espn:fbs:401860920"]
    assert len(rows) == 1
    assert rows[0]["home_handicap"] == -14
    assert rows[0]["ats_pick_label"] == "Texas State -14"
    assert rows[0]["pick_label"] == "Texas State to win"
