"""Fresh prices, gameday inactives, portal moves, and usage roles."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from spe.adapters.cfbd import CfbdAdapter
from spe.db.models import Game, League, Player, PlayerGameStat, Roster, Season, Sport, Team, Venue
from spe.identity.roster import EligiblePlayer, eligible_players_for_game
from spe.ingest.cfbd_portal import ingest_cfbd_portal
from spe.ingest.espn_availability import parse_summary_availability
from spe.market.price_age import max_fresh_minutes
from spe.models.player_td.score import _player_priors_from_hist
from spe.ops.availability import availability_for_player


def test_near_kickoff_price_window_is_a_few_minutes():
    now = datetime(2026, 11, 7, 18, 0, tzinfo=UTC)
    near = max_fresh_minutes(now + timedelta(hours=1), now)
    far = max_fresh_minutes(now + timedelta(hours=30), now)
    assert near == 12
    assert far == 45
    assert near < far


def test_inactive_window_ignores_a_midweek_report_until_the_list_arrives():
    now = datetime(2026, 11, 7, 18, 0, tzinfo=UTC)
    kick = now + timedelta(hours=1)
    missing = availability_for_player(
        league_id="nfl",
        kickoff=kick,
        player_injury=None,
        game_has_complete_report=True,
        now=now,
        inactive_list_received=False,
    )
    assert missing["coverage"] == "report_expected_missing"
    assert "NEVER_ASSUME_HEALTHY" in missing["abstain_reasons"]

    cleared = availability_for_player(
        league_id="nfl",
        kickoff=kick,
        player_injury=type("Row", (), {"designation": "Out", "practice_status": None, "confidence": 0.7, "source": "nflverse.injuries"})(),
        game_has_complete_report=True,
        now=now,
        inactive_list_received=True,
    )
    assert cleared["availability_status"] == "probable"
    assert cleared["abstain_reasons"] == []


def test_inactive_designation_is_unavailable():
    now = datetime(2026, 11, 7, 18, 0, tzinfo=UTC)
    row = type(
        "Row",
        (),
        {"designation": "Inactive", "practice_status": None, "confidence": 0.8, "source": "espn.gameday_inactives"},
    )()
    info = availability_for_player(
        league_id="nfl",
        kickoff=now + timedelta(minutes=40),
        player_injury=row,
        game_has_complete_report=False,
        now=now,
        inactive_list_received=True,
    )
    assert info["coverage"] == "confirmed_player_unavailable"


def test_espn_summary_separates_injury_notes_from_an_inactive_list():
    parsed = parse_summary_availability(
        {
            "injuries": [{"injuries": [{"athlete": {"displayName": "A Player"}, "status": "Questionable"}]}],
            "inactives": [{"athlete": {"displayName": "B Player"}, "status": "Inactive"}],
        }
    )
    assert parsed["inactive_list"] is True
    names = {row["name"]: row["inactive_list"] for row in parsed["rows"]}
    assert names["A Player"] == "0"
    assert names["B Player"] == "1"


def test_new_transfer_shrinks_prior_school_workload():
    hist = pd.DataFrame(
        [
            {
                "player_id": "fbs:1",
                "team_id": "fbs:old",
                "kickoff_utc": "2025-10-01T00:00:00Z",
                "season_year": 2025,
                "carries": 20,
                "receptions": 0,
                "anytime_tds": 2,
                "rush_td": 2,
                "rec_td": 0,
            }
        ]
    )
    out = _player_priors_from_hist(
        hist,
        "fbs:1",
        datetime(2026, 11, 8, tzinfo=UTC),
        "fbs",
        current_team_id="fbs:new",
        role="new_transfer",
    )
    assert "new_transfer_prior_org_usage_shrunk" in out["scope_flags"]
    assert abs(float(out["prior_carries"]) - 7.0) < 1e-9
    assert abs(float(out["recent8_td_rate_current_team"]) - 0.7) < 1e-9


def test_portal_closes_origin_and_opens_destination(db):
    db.add(Sport(sport_id="football", name="American football"))
    db.flush()
    db.add(League(league_id="fbs", sport_id="football", name="FBS", classification="fbs"))
    db.flush()
    db.add_all(
        [
            Team(team_id="fbs:old", league_id="fbs", name="Old School", abbreviation="OLD", aliases=[]),
            Team(team_id="fbs:new", league_id="fbs", name="New School", abbreviation="NEW", aliases=[]),
        ]
    )
    db.add(Player(player_id="fbs:9", name="Portal Back", position_group="RB", position="RB", aliases=[]))
    db.flush()
    db.add(
        Roster(
            team_id="fbs:old",
            player_id="fbs:9",
            valid_from=datetime(2025, 8, 1, tzinfo=UTC),
            status="active",
        )
    )
    db.commit()
    result = ingest_cfbd_portal(
        db,
        year=2026,
        adapter=CfbdAdapter(api_key="test"),
        fixture=[
            {
                "firstName": "Portal",
                "lastName": "Back",
                "position": "RB",
                "origin": "Old School",
                "destination": "New School",
                "transferDate": "2026-01-15T00:00:00Z",
            }
        ],
    )
    db.commit()
    assert result["moved"] == 1
    rows = db.query(Roster).filter(Roster.player_id == "fbs:9").all()
    by_team = {row.team_id: row for row in rows}
    assert by_team["fbs:old"].valid_to is not None
    assert by_team["fbs:new"].valid_to is None


def test_usage_role_is_starter_committee_or_new_transfer(db):
    kick = datetime(2026, 11, 8, 20, 0, tzinfo=UTC)
    db.add(Sport(sport_id="football", name="American football"))
    db.flush()
    db.add(League(league_id="fbs", sport_id="football", name="FBS", classification="fbs"))
    db.flush()
    db.add(Season(season_id="fbs-2026", league_id="fbs", year=2026, start_at=kick - timedelta(days=60), end_at=kick + timedelta(days=30)))
    db.add(Venue(venue_id="v1", name="Field", lat=1, lon=1, roof_type="open", surface="grass"))
    db.add_all(
        [
            Team(team_id="fbs:home", league_id="fbs", name="Home", abbreviation="HOM", aliases=[]),
            Team(team_id="fbs:away", league_id="fbs", name="Away", abbreviation="AWY", aliases=[]),
            Team(team_id="fbs:old", league_id="fbs", name="Old", abbreviation="OLD", aliases=[]),
        ]
    )
    db.add_all(
        [
            Player(player_id="fbs:starter", name="Lead Back", position_group="RB", position="RB", aliases=[]),
            Player(player_id="fbs:committee", name="Split Back", position_group="RB", position="RB", aliases=[]),
            Player(player_id="fbs:transfer", name="New Back", position_group="RB", position="RB", aliases=[]),
        ]
    )
    db.flush()
    prior = datetime(2026, 10, 1, tzinfo=UTC)
    for offset, week in ((0, prior), (7, prior + timedelta(days=7)), (14, prior + timedelta(days=14))):
        del offset
        gid = f"fbs:g{week.day}"
        db.add(
            Game(
                game_id=gid,
                season_id="fbs-2026",
                league_id="fbs",
                home_team_id="fbs:home",
                away_team_id="fbs:away",
                kickoff_utc=week,
                venue_id="v1",
                status="final",
                week=1,
                data_tier="historical",
            )
        )
    db.add(
        Game(
            game_id="fbs:next",
            season_id="fbs-2026",
            league_id="fbs",
            home_team_id="fbs:home",
            away_team_id="fbs:away",
            kickoff_utc=kick,
            venue_id="v1",
            status="scheduled",
            week=10,
            data_tier="upcoming",
        )
    )
    db.flush()
    for gid, starter_carries, committee_carries in (
        ("fbs:g1", 18, 4),
        ("fbs:g8", 16, 5),
        ("fbs:g15", 20, 6),
    ):
        for pid, carries in (("fbs:starter", starter_carries), ("fbs:committee", committee_carries)):
            db.add(
                PlayerGameStat(
                    game_id=gid,
                    player_id=pid,
                    team_id="fbs:home",
                    carries=carries,
                    targets=0,
                    receptions=0,
                    provider="cfbd",
                    provider_record_id=f"{gid}:{pid}",
                    observed_at=prior,
                    effective_at=prior,
                )
            )
    db.add_all(
        [
            Roster(team_id="fbs:home", player_id="fbs:starter", valid_from=datetime(2026, 8, 1, tzinfo=UTC), status="active"),
            Roster(team_id="fbs:home", player_id="fbs:committee", valid_from=datetime(2026, 8, 1, tzinfo=UTC), status="active"),
            Roster(team_id="fbs:old", player_id="fbs:transfer", valid_from=datetime(2025, 8, 1, tzinfo=UTC), valid_to=datetime(2026, 1, 1, tzinfo=UTC), status="active"),
            Roster(team_id="fbs:home", player_id="fbs:transfer", valid_from=datetime(2026, 8, 1, tzinfo=UTC), status="active"),
        ]
    )
    db.commit()
    game = db.get(Game, "fbs:next")
    roles = {row.player_id: row.role for row in eligible_players_for_game(db, game)}
    assert roles["fbs:starter"] == "starter"
    assert roles["fbs:committee"] == "committee"
    assert roles["fbs:transfer"] == "new_transfer"


def test_inactive_list_lookup_is_empty_without_a_list(db):
    from spe.ops.availability import games_with_inactive_list

    assert games_with_inactive_list(db, []) == set()
    assert games_with_inactive_list(db, ["espn:nfl:missing"]) == set()


def test_eligible_player_role_defaults():
    player = EligiblePlayer("p", "t", "Name", "RB", "active", "test")
    assert player.role == "unspecified"
