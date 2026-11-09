from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from spe.analytics.engine import prior_only
from spe.db.models import Bookmaker, Game, Market, OddsTick, Outcome, Player, PlayerGameStat, Roster, Team
from spe.ingest.cfbd_box import flatten_game_players, parse_comp_att
from spe.ingest.cfbd_historical import ingest_fbs_history
from spe.ingest.cfbd_players import ingest_fbs_players
from spe.ingest.odds_live import ingest_odds_live
from spe.ingest.store import ensure_football_leagues, ensure_season, ensure_venue
from spe.models.metrics import apply_platt, platt_calibrate


def test_flatten_box_does_not_invent_play_involvement():
    raw = {
        "id": 401677183,
        "teams": [
            {
                "team": "Ohio State",
                "categories": [
                    {
                        "name": "receiving",
                        "types": [
                            {"name": "REC", "athletes": [{"id": "1", "name": "Emeka Egbuka", "stat": "6"}]},
                            {"name": "YDS", "athletes": [{"id": "1", "name": "Emeka Egbuka", "stat": "82"}]},
                            {"name": "TD", "athletes": [{"id": "1", "name": "Emeka Egbuka", "stat": "1"}]},
                        ],
                    },
                    {
                        "name": "passing",
                        "types": [
                            {"name": "C/ATT", "athletes": [{"id": "2", "name": "Will Howard", "stat": "21/30"}]},
                            {"name": "TD", "athletes": [{"id": "2", "name": "Will Howard", "stat": "2"}]},
                        ],
                    },
                    {"name": "defensive", "types": [{"name": "TD", "athletes": [{"id": "9", "name": "DB", "stat": "1"}]}]},
                ],
            }
        ],
    }
    rows = flatten_game_players(raw)
    ids = {r["athlete_id"] for r in rows}
    assert "9" not in ids
    rec = next(r for r in rows if r["athlete_id"] == "1")
    assert rec["receptions"] == 6 and rec["rec_yards"] == 82 and rec["rec_td"] == 1
    qb = next(r for r in rows if r["athlete_id"] == "2")
    assert qb["attempts"] == 30 and qb["pass_td"] == 2
    assert parse_comp_att("12/20") == (12, 20)


def test_cfbd_box_ingest_resolves_team_name_and_keeps_zero_td_rows(db):
    fixtures = {
        "teams": [
            {"id": 194, "school": "Ohio State", "abbreviation": "OSU", "conference": "Big Ten", "classification": "fbs"},
            {"id": 213, "school": "Penn State", "abbreviation": "PSU", "conference": "Big Ten", "classification": "fbs"},
        ],
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
                "homePoints": 28,
                "awayPoints": 17,
            }
        ],
        "roster:2019": [
            {"id": 8, "firstName": "Justin", "lastName": "Fields", "position": "QB", "team": "Ohio State"},
            {"id": 8, "firstName": "Justin", "lastName": "Fields", "position": "QB", "team": "Ohio State"},
            {"id": 9, "firstName": "Chris", "lastName": "Olave", "position": "WR", "team": "Ohio State"},
        ],
        "ppa_players:2019": [],
        "games_players:2019": [
            {
                "id": 401112148,
                "teams": [
                    {
                        "team": "Ohio State",
                        "categories": [
                            {
                                "name": "passing",
                                "types": [
                                    {"name": "C/ATT", "athletes": [{"id": "8", "name": "Justin Fields", "stat": "20/30"}]},
                                    {"name": "TD", "athletes": [{"id": "8", "name": "Justin Fields", "stat": "3"}]},
                                ],
                            },
                            {
                                "name": "receiving",
                                "types": [
                                    {"name": "REC", "athletes": [{"id": "9", "name": "Chris Olave", "stat": "5"}]},
                                    {"name": "YDS", "athletes": [{"id": "9", "name": "Chris Olave", "stat": "70"}]},
                                    {"name": "TD", "athletes": [{"id": "9", "name": "Chris Olave", "stat": "0"}]},
                                ],
                            },
                        ],
                    }
                ],
            }
        ],
        "player_usage:2019": [],
        "ppa_players_season:2019": [],
    }
    ingest_fbs_history(db, seasons=[2019], fixtures=fixtures)
    result = ingest_fbs_players(db, seasons=[2019], fixtures=fixtures)
    db.commit()
    assert result["stats_upserted"] == 2
    olave = db.query(PlayerGameStat).filter_by(player_id="fbs:9").one()
    assert olave.rec_td == 0
    assert olave.receptions == 5
    assert olave.team_id == "fbs:194"
    fields = db.query(PlayerGameStat).filter_by(player_id="fbs:8").one()
    assert fields.pass_td == 3 and fields.rec_td == 0


def test_prior_only_requires_game_completion_not_just_kickoff_timestamp():
    games = pd.DataFrame(
        {
            "game_id": ["early", "late"],
            "kickoff_utc": [
                datetime(2023, 9, 10, 17, tzinfo=UTC),
                datetime(2023, 9, 10, 20, tzinfo=UTC),
            ],
        }
    )
    prior = prior_only(games, pd.Timestamp(datetime(2023, 9, 10, 20, tzinfo=UTC)))
    assert list(prior["game_id"]) == []
    later = prior_only(games, pd.Timestamp(datetime(2023, 9, 10, 22, tzinfo=UTC)))
    assert list(later["game_id"]) == ["early"]


def test_live_odds_appends_price_changes_and_skips_demo(db):
    ensure_football_leagues(db)
    ensure_season(db, "nfl", 2026)
    db.add_all(
        [
            Team(team_id="nfl:KC", league_id="nfl", name="Kansas City Chiefs", abbreviation="KC", aliases=[]),
            Team(team_id="nfl:BUF", league_id="nfl", name="Buffalo Bills", abbreviation="BUF", aliases=[]),
            Bookmaker(bookmaker_id="draftkings", name="DraftKings"),
            Market(
                market_id="anytime_td",
                sport="football",
                market_type="player_yesno",
                period="game",
                stat="touchdowns",
                grading_rule="td",
                odds_api_key="player_anytime_td",
            ),
        ]
    )
    venue = ensure_venue(db, "nfl-venue:arrowhead", "Arrowhead")
    demo = Game(
        game_id="kc",
        season_id="nfl-2026",
        league_id="nfl",
        home_team_id="nfl:KC",
        away_team_id="nfl:BUF",
        kickoff_utc=datetime(2026, 11, 8, 18, 25, tzinfo=UTC),
        venue_id=venue.venue_id,
        status="scheduled",
        data_tier="demo",
    )
    hist = Game(
        game_id="nfl:2026_10_BUF_KC",
        season_id="nfl-2026",
        league_id="nfl",
        home_team_id="nfl:KC",
        away_team_id="nfl:BUF",
        kickoff_utc=datetime(2026, 11, 8, 18, 25, tzinfo=UTC),
        venue_id=venue.venue_id,
        status="scheduled",
        data_tier="historical",
    )
    db.add_all([demo, hist])
    db.add(Player(player_id="nfl:rice", name="Rashee Rice", position_group="WR", position="WR", aliases=[]))
    db.add(Roster(team_id="nfl:KC", player_id="nfl:rice", valid_from=datetime(2026, 8, 1, tzinfo=UTC), status="active"))
    db.commit()
    event = {
        "id": "evt-nfl-1",
        "home_team": "Kansas City Chiefs",
        "away_team": "Buffalo Bills",
        "commence_time": "2026-11-08T18:25:00Z",
    }
    odds = {
        "id": "evt-nfl-1",
        "home_team": "Kansas City Chiefs",
        "away_team": "Buffalo Bills",
        "commence_time": "2026-11-08T18:25:00Z",
        "bookmakers": [
            {
                "key": "draftkings",
                "markets": [
                    {
                        "key": "player_anytime_td",
                        "last_update": "2026-11-06T18:00:00Z",
                        "outcomes": [{"name": "Yes", "description": "Rashee Rice", "price": 180}],
                    }
                ],
            }
        ],
    }
    now = datetime(2026, 11, 6, 18, tzinfo=UTC)
    first = ingest_odds_live(
        db,
        now=now,
        leagues=("nfl",),
        fixture_events={"nfl": [event]},
        fixture_odds={"evt-nfl-1": odds},
    )
    db.commit()
    assert first["ticks_upserted"] == 1
    assert db.query(OddsTick).count() == 1
    assert db.query(Outcome).filter_by(game_id="kc").count() == 0
    second = ingest_odds_live(
        db,
        now=now + timedelta(minutes=1),
        leagues=("nfl",),
        fixture_events={"nfl": [event]},
        fixture_odds={"evt-nfl-1": odds},
    )
    db.commit()
    assert second["ticks_upserted"] == 0
    odds["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = 165
    third = ingest_odds_live(
        db,
        now=now + timedelta(minutes=181),
        leagues=("nfl",),
        fixture_events={"nfl": [event]},
        fixture_odds={"evt-nfl-1": odds},
    )
    db.commit()
    assert third["ticks_upserted"] == 1
    assert db.query(OddsTick).count() == 2


def test_live_odds_run_alternates_leagues_so_one_slate_cannot_take_every_slot():
    from types import SimpleNamespace

    from spe.ingest.odds_live import _interleave_leagues

    fbs = [("fbs", SimpleNamespace(game_id=f"f{i}"), {"id": f"f{i}"}) for i in range(5)]
    nfl = [("nfl", SimpleNamespace(game_id=f"n{i}"), {"id": f"n{i}"}) for i in range(3)]
    mixed = _interleave_leagues(fbs + nfl)
    assert [item[0] for item in mixed] == ["fbs", "nfl", "fbs", "nfl", "fbs", "nfl", "fbs", "fbs"]
    assert [item[2]["id"] for item in mixed if item[0] == "nfl"] == ["n0", "n1", "n2"]


def test_rams_at_washington_prefers_the_nflverse_game():
    from types import SimpleNamespace

    from spe.ingest.odds_live import _prefer_slate_game, _same_nfl_club

    assert _same_nfl_club("nfl:LA", "nfl:LAR")
    assert _same_nfl_club("nfl:WAS", "nfl:WSH")
    espn = SimpleNamespace(game_id="espn:nfl:401873043")
    slate = SimpleNamespace(game_id="nfl:2026_09_LA_WAS")
    assert _prefer_slate_game(slate, espn) is slate
    assert _prefer_slate_game(espn, slate) is slate


def test_college_names_match_mascots_without_crossing_schools():
    from spe.ingest.odds_live import _names_compatible

    assert _names_compatible("Utah", "Utah Utes")
    assert _names_compatible("Iowa", "Iowa Hawkeyes")
    assert _names_compatible("App State", "Appalachian State Mountaineers")
    assert _names_compatible("Sam Houston", "Sam Houston State Bearkats")
    assert _names_compatible("San José State", "San Jose State Spartans")
    assert not _names_compatible("Ohio", "Ohio State Buckeyes")
    assert not _names_compatible("Texas", "Texas A&M Aggies")
    assert not _names_compatible("Miami", "Miami (OH) RedHawks")
    assert not _names_compatible("Michigan", "Michigan State Spartans")


def test_platt_improves_calibration_on_biased_probs():
    import numpy as np

    y = np.array([0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0])
    p = np.array([0.2, 0.2, 0.2, 0.4, 0.4, 0.4, 0.4, 0.4])
    a, b = platt_calibrate(p, y)
    cal = apply_platt(p, a, b)
    assert abs(cal.mean() - y.mean()) < abs(p.mean() - y.mean())
