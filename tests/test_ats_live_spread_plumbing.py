"""ATS consumes Odds API spreads without conflating them with nflverse L."""

from __future__ import annotations

import pandas as pd

from spe.models.ats.spread_contract import attach_spread_fields, classify_spread_source


def test_cfbd_home_handicap_is_not_flipped():
    from spe.ingest.cfbd_lines import consensus_home_handicap

    lines = [
        {"provider": "DraftKings", "spread": -6.0},
        {"provider": "consensus", "spread": -7.5},
    ]
    assert consensus_home_handicap(lines) == -7.5
    frame = pd.DataFrame({"spread_line": [-7.5], "source": ["cfbd"]})
    out = attach_spread_fields(frame)
    assert float(out.loc[0, "home_handicap"]) == -7.5
    assert float(out.loc[0, "market_home_margin"]) == 7.5


def test_odds_api_source_is_sportsbook_handicap_not_nflverse_L():
    assert classify_spread_source("odds_api") == "sportsbook_handicap"
    assert classify_spread_source("draftkings") == "sportsbook_handicap"
    frame = pd.DataFrame({"spread_line": [-6.5], "source": ["odds_api"]})
    out = attach_spread_fields(frame)
    assert float(out.loc[0, "home_handicap"]) == -6.5
    assert float(out.loc[0, "market_home_margin"]) == 6.5
    # Contrast: same numeric under nflverse would flip.
    frame2 = pd.DataFrame({"spread_line": [-6.5], "source": ["nflverse_consensus"]})
    out2 = attach_spread_fields(frame2)
    assert float(out2.loc[0, "home_handicap"]) == 6.5


def test_shared_school_prefix_does_not_steal_the_spread():
    """Michigan must not take Michigan State's number, or the favorite shows up as +14."""
    from types import SimpleNamespace

    from spe.ingest.odds_spreads import _name_variants, spread_side, team_name_from_spread_record

    home = SimpleNamespace(
        name="Michigan Wolverines",
        abbreviation="MICH",
        aliases=["Michigan Wolverines", {"location": "Michigan", "nick": "Wolverines"}],
    )
    away = SimpleNamespace(
        name="Michigan State",
        abbreviation="MSU",
        aliases=[{"location": "Michigan State", "nick": "Spartans"}],
    )
    home_names = _name_variants(home)
    away_names = _name_variants(away)
    assert spread_side("Michigan Wolverines", home_names, away_names) == "home"
    assert spread_side("Michigan State Spartans", home_names, away_names) == "away"
    assert spread_side("Michigan", home_names, away_names) == "home"
    assert team_name_from_spread_record(
        "evt:draftkings:spreads:Michigan State Spartans:None:14.0"
    ) == "Michigan State Spartans"


def test_carolina_and_ohio_prefixes_stay_on_the_longer_name():
    from types import SimpleNamespace

    from spe.ingest.odds_spreads import _name_variants, spread_side

    north = _name_variants(SimpleNamespace(name="North Carolina Tar Heels", abbreviation="UNC", aliases=[]))
    south = _name_variants(SimpleNamespace(name="South Carolina Gamecocks", abbreviation="SC", aliases=[]))
    assert spread_side("North Carolina Tar Heels", north, south) == "home"
    assert spread_side("South Carolina Gamecocks", north, south) == "away"

    buckeyes = _name_variants(SimpleNamespace(name="Ohio State Buckeyes", abbreviation="OSU", aliases=[]))
    bobcats = _name_variants(SimpleNamespace(name="Ohio Bobcats", abbreviation="OHIO", aliases=[]))
    assert spread_side("Ohio State Buckeyes", buckeyes, bobcats) == "home"
    assert spread_side("Ohio Bobcats", buckeyes, bobcats) == "away"
    assert spread_side("State", buckeyes, bobcats) is None

    app = _name_variants(
        SimpleNamespace(
            name="App State",
            abbreviation="APP",
            aliases=[{"location": "App State", "nick": "Mountaineers"}],
        )
    )
    georgia_state = _name_variants(
        SimpleNamespace(
            name="Georgia State",
            abbreviation="GAST",
            aliases=[{"location": "Georgia State", "nick": "Panthers"}],
        )
    )
    assert spread_side("Appalachian State Mountaineers", app, georgia_state) == "home"
    assert spread_side("Georgia State Panthers", app, georgia_state) == "away"


def test_conflicting_home_lines_keep_the_pair_that_agrees():
    from datetime import UTC, datetime

    from spe.models.ats.predict import _coherent_spread_quotes

    now = datetime(2026, 11, 7, tzinfo=UTC)
    raw = pd.DataFrame(
        [
            {"game_id": "g", "side": "home", "line": 14.0, "price_american": -108, "bookmaker_id": "draftkings", "captured_at": now},
            {"game_id": "g", "side": "home", "line": -14.0, "price_american": -112, "bookmaker_id": "draftkings", "captured_at": now},
            {"game_id": "g", "side": "away", "line": 14.0, "price_american": -108, "bookmaker_id": "draftkings", "captured_at": now},
        ]
    )
    out = _coherent_spread_quotes(raw)
    assert float(out.loc[0, "spread_line"]) == -14.0


def test_in_game_spread_does_not_replace_the_pregame_number():
    """Duke @ NC State: +4.5 before kickoff, +35.5 once the game was a blowout."""
    from datetime import UTC, datetime

    from spe.models.ats.predict import _coherent_spread_quotes

    kick = datetime(2026, 11, 7, 17, 30, tzinfo=UTC)
    early = datetime(2026, 11, 7, 14, 30, tzinfo=UTC)
    later = datetime(2026, 11, 7, 20, 28, tzinfo=UTC)
    raw = pd.DataFrame(
        [
            {"game_id": "g", "side": "home", "line": 35.5, "price_american": -215, "bookmaker_id": "fanduel", "captured_at": later, "kickoff_utc": kick},
            {"game_id": "g", "side": "away", "line": -35.5, "price_american": 158, "bookmaker_id": "fanduel", "captured_at": later, "kickoff_utc": kick},
            {"game_id": "g", "side": "home", "line": 4.5, "price_american": -110, "bookmaker_id": "fanduel", "captured_at": early, "kickoff_utc": kick},
            {"game_id": "g", "side": "away", "line": -4.5, "price_american": -110, "bookmaker_id": "fanduel", "captured_at": early, "kickoff_utc": kick},
        ]
    )
    out = _coherent_spread_quotes(raw)
    assert float(out.loc[0, "spread_line"]) == 4.5


def test_line_posted_only_after_kickoff_is_not_a_bet():
    from datetime import UTC, datetime

    from spe.models.ats.predict import _coherent_spread_quotes

    kick = datetime(2026, 11, 7, 17, 30, tzinfo=UTC)
    later = datetime(2026, 11, 7, 20, 28, tzinfo=UTC)
    raw = pd.DataFrame(
        [
            {"game_id": "g", "side": "home", "line": 35.5, "price_american": -215, "bookmaker_id": "fanduel", "captured_at": later, "kickoff_utc": kick},
            {"game_id": "g", "side": "away", "line": -35.5, "price_american": 158, "bookmaker_id": "fanduel", "captured_at": later, "kickoff_utc": kick},
        ]
    )
    out = _coherent_spread_quotes(raw)
    assert out.empty


def test_final_scoreboard_keeps_the_score_and_a_scheduled_zero_does_not():
    from spe.ingest.espn_scoreboard import _parse_event

    def event(status: str, home: str, away: str) -> dict:
        return {
            "id": "401858286",
            "date": "2026-11-07T17:30Z",
            "season": {"year": 2026},
            "week": {"number": 11},
            "competitions": [
                {
                    "neutralSite": False,
                    "venue": {"id": "1", "fullName": "Carter-Finley Stadium"},
                    "status": {"type": {"name": status}},
                    "competitors": [
                        {"homeAway": "home", "score": home, "team": {"id": "152", "abbreviation": "NCSU", "displayName": "NC State", "name": "Wolfpack"}},
                        {"homeAway": "away", "score": away, "team": {"id": "150", "abbreviation": "DUKE", "displayName": "Duke", "name": "Blue Devils"}},
                    ],
                }
            ],
        }

    final = _parse_event("fbs", event("STATUS_FINAL", "3", "41"))
    assert final is not None
    assert final["status"] == "final"
    assert final["home_score"] == 3
    assert final["away_score"] == 41
    scheduled = _parse_event("fbs", event("STATUS_SCHEDULED", "0", "0"))
    assert scheduled is not None
    assert scheduled["home_score"] is None
    assert scheduled["away_score"] is None


def test_spread_dedupe_id_distinct_from_props_event_id():
    from spe.ingest.odds_spreads import _spread_dedupe_id

    assert _spread_dedupe_id("abc123") == "abc123:spreads"
    assert _spread_dedupe_id("abc123") != "abc123"


def test_live_book_eligibility_rule_for_clean_prospective():
    """Clean prospective requires live_book + enabled cover probs (locked eval rules)."""
    quote_class = "live_book"
    cover_enabled = True
    p_cover = 0.55
    eligibility = (
        "clean_prospective"
        if (cover_enabled and quote_class == "live_book" and p_cover is not None)
        else (
            "consensus_research"
            if (cover_enabled and quote_class == "consensus" and p_cover is not None)
            else "ineligible"
        )
    )
    assert eligibility == "clean_prospective"
    # Consensus never becomes clean_prospective.
    quote_class = "consensus"
    eligibility = (
        "clean_prospective"
        if (cover_enabled and quote_class == "live_book" and p_cover is not None)
        else (
            "consensus_research"
            if (cover_enabled and quote_class == "consensus" and p_cover is not None)
            else "ineligible"
        )
    )
    assert eligibility == "consensus_research"
