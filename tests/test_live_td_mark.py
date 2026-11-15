"""A live touchdown turns green. A teammate who has not scored stays unmarked."""

from datetime import UTC, datetime, timedelta

from spe.db.models import Game, League, ProviderIdMap, Season, Sport, Team, Venue
from spe.desk.day_results import apply_recorded_results

NOW = datetime(2026, 11, 15, 16, tzinfo=UTC)


def _seed(db) -> None:
    db.add(Sport(sport_id="s", name="Football"))
    db.flush()
    db.add(League(league_id="nfl", sport_id="s", name="NFL", classification="pro"))
    db.flush()
    db.add(Season(
        season_id="season",
        league_id="nfl",
        year=2026,
        start_at=NOW - timedelta(days=50),
        end_at=NOW + timedelta(days=50),
    ))
    db.add(Venue(venue_id="v", name="Field", lat=0, lon=0, roof_type="open", surface="grass"))
    db.add_all([
        Team(team_id="det", league_id="nfl", name="Lions", abbreviation="DET"),
        Team(team_id="ne", league_id="nfl", name="Patriots", abbreviation="NE"),
    ])
    db.flush()
    db.add(Game(
        game_id="nfl:2026_10_NE_DET",
        season_id="season",
        league_id="nfl",
        home_team_id="det",
        away_team_id="ne",
        venue_id="v",
        kickoff_utc=NOW,
        data_tier="upcoming",
        status="in_progress",
    ))
    db.add(ProviderIdMap(
        map_id="map",
        provider="espn",
        entity_type="game",
        external_id="401873053",
        canonical_id="nfl:2026_10_NE_DET",
        confidence=1,
    ))
    db.commit()


def _row(player_id: str, name: str) -> dict:
    return {
        "game_id": "nfl:2026_10_NE_DET",
        "player_id": player_id,
        "player_name": name,
        "market_id": "anytime_td",
        "side": "yes",
    }


def _box(scorers: dict[tuple[str, str], int]) -> dict:
    athletes = []
    for (player_id, name), tds in scorers.items():
        athletes.append({
            "athlete": {"id": player_id.split(":")[-1], "displayName": name},
            "stats": ["4", "40", str(tds)],
        })
    return {
        "header": {
            "competitions": [{
                "status": {"type": {"name": "STATUS_IN_PROGRESS"}},
                "competitors": [
                    {"homeAway": "home", "score": "14"},
                    {"homeAway": "away", "score": "3"},
                ],
            }],
        },
        "boxscore": {
            "players": [{
                "statistics": [{
                    "name": "receiving",
                    "labels": ["REC", "YDS", "TD"],
                    "athletes": athletes,
                }],
            }],
        },
    }


def test_a_live_touchdown_grades_green_before_the_final(db):
    from spe.db.models import PublishedStat
    from spe.models.player_td.desk_replay import grade

    _seed(db)
    game = db.get(Game, "nfl:2026_10_NE_DET")
    db.add(PublishedStat(
        key="nfl:2026_10_NE_DET|nfl:6963|anytime_td",
        game_id="nfl:2026_10_NE_DET",
        player_id="nfl:6963",
        market_id="anytime_td",
        actual=1,
        source="espn.live",
        observed_at=NOW,
    ))
    db.add(PublishedStat(
        key="nfl:2026_10_NE_DET|nfl:9139|anytime_td",
        game_id="nfl:2026_10_NE_DET",
        player_id="nfl:9139",
        market_id="anytime_td",
        actual=0,
        source="espn.live",
        observed_at=NOW,
    ))
    db.flush()
    assert grade(db, game, "nfl:6963") == "scored"
    assert grade(db, game, "nfl:9139") == "pending"
    game.status = "final"
    db.flush()
    assert grade(db, game, "nfl:6963") == "scored"
    assert grade(db, game, "nfl:9139") == "awaiting_box"


def test_two_lines_for_one_player_save_once(db):
    _seed(db)
    box = _box({("nfl:9139", "Receiver"): 0})
    rows = [
        {
            "game_id": "nfl:2026_10_NE_DET",
            "player_id": "nfl:9139",
            "player_name": "Receiver",
            "market_id": "receptions",
            "side": "over",
            "line": 4.5,
        },
        {
            "game_id": "nfl:2026_10_NE_DET",
            "player_id": "nfl:9139",
            "player_name": "Receiver",
            "market_id": "receptions",
            "side": "under",
            "line": 4.5,
        },
    ]
    apply_recorded_results(db, [], rows, fetch=lambda league, event: box)
    db.flush()
    assert rows[0]["actual"] == 4
    assert rows[1]["actual"] == 4


def test_a_game_that_has_not_kicked_off_is_not_fetched(db):
    _seed(db)
    game = db.get(Game, "nfl:2026_10_NE_DET")
    game.status = "scheduled"
    game.kickoff_utc = datetime.now(UTC) + timedelta(hours=4)
    db.flush()
    rows = [
        {
            "game_id": "nfl:2026_10_NE_DET",
            "player_id": "nfl:9139",
            "player_name": "Receiver",
            "market_id": "receptions",
            "side": "over",
            "line": 4.5,
        }
    ]

    def fail(league, event):
        raise AssertionError("future game was fetched")

    apply_recorded_results(db, [], rows, fetch=fail)
    assert rows[0]["result"] == "pending"


def test_a_live_touchdown_is_green_and_a_blank_teammate_stays_open(db):
    _seed(db)
    rows = [
        _row("nfl:6963", "Amon-Ra St. Brown"),
        _row("nfl:40669", "Isaac TeSlaa"),
        _row("nfl:9139", "Jahmyr Gibbs"),
    ]
    box = _box({
        ("nfl:6963", "Amon-Ra St. Brown"): 1,
        ("nfl:40669", "Isaac TeSlaa"): 1,
        ("nfl:9139", "Jahmyr Gibbs"): 0,
    })
    apply_recorded_results(db, [], rows, fetch=lambda league, event: box)
    by_name = {row["player_name"]: row["result"] for row in rows}
    assert by_name["Amon-Ra St. Brown"] == "win"
    assert by_name["Isaac TeSlaa"] == "win"
    assert by_name["Jahmyr Gibbs"] == "pending"


def test_a_final_game_without_a_box_is_graded(db):
    from spe.models.player_td.desk_replay import grade

    _seed(db)
    game = db.get(Game, "nfl:2026_10_NE_DET")
    game.status = "final"
    game.away_score = 10
    game.home_score = 35
    db.flush()
    rows = [_row("nfl:6963", "Amon-Ra St. Brown"), _row("nfl:9139", "Jahmyr Gibbs")]
    box = _box({
        ("nfl:6963", "Amon-Ra St. Brown"): 0,
        ("nfl:9139", "Jahmyr Gibbs"): 1,
    })
    box["header"]["competitions"][0]["status"]["type"]["name"] = "STATUS_FINAL"
    apply_recorded_results(db, [], rows, fetch=lambda league, event: box)
    assert grade(db, game, "nfl:6963") == "blank"
    assert grade(db, game, "nfl:9139") == "scored"
