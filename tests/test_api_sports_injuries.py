"""API-Sports FBS injury ingest. Missing reports stay unknown."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from spe.db.models import (
    Game,
    InjuryEvent,
    League,
    Player,
    Roster,
    Season,
    Sport,
    Team,
    Venue,
)
from spe.ingest.api_sports_injuries import ingest_api_sports_injuries
from spe.ops.availability import availability_for_player


def test_fbs_fixture_injuries_match_and_mark_coverage(db):
    s = db
    now = datetime.now(UTC)
    s.add(Sport(sport_id="football", name="Football"))
    s.flush()
    s.add(League(league_id="fbs", sport_id="football", name="FBS", classification="fbs"))
    s.flush()
    s.add(
        Season(
            season_id="fbs-2026",
            league_id="fbs",
            year=2026,
            start_at=now - timedelta(days=30),
            end_at=now + timedelta(days=60),
        )
    )
    s.add(Venue(venue_id="v", name="Stadium", lat=0.0, lon=0.0, roof_type="open", surface="grass"))
    s.add(Team(team_id="fbs:tex", league_id="fbs", name="Texas Longhorns", abbreviation="TEX"))
    s.add(Team(team_id="fbs:okla", league_id="fbs", name="Oklahoma Sooners", abbreviation="OU"))
    s.add(Player(player_id="fbs:p1", name="Arch Manning", position_group="QB", position="QB"))
    s.flush()
    s.add(Roster(roster_id="r1", team_id="fbs:tex", player_id="fbs:p1", valid_from=now - timedelta(days=30), status="active"))
    s.add(
        Game(
            game_id="espn:fbs:fixture1",
            season_id="fbs-2026",
            league_id="fbs",
            week=10,
            kickoff_utc=now + timedelta(hours=20),
            home_team_id="fbs:tex",
            away_team_id="fbs:okla",
            venue_id="v",
            status="scheduled",
            data_tier="upcoming",
        )
    )
    s.commit()

    result = ingest_api_sports_injuries(
        s,
        league_id="fbs",
        season=2026,
        now=now,
        fixture_team_map={"101": "fbs:tex"},
        fixture_injuries={
            "101": [
                {
                    "player": {"name": "Arch Manning", "status": "Questionable"},
                    "date": (now - timedelta(hours=2)).isoformat(),
                    "status": "Questionable",
                }
            ]
        },
    )
    s.commit()
    assert result["injuries_upserted"] == 1
    assert result["players_matched"] == 1
    assert result["plan_blocked"] is False
    row = s.query(InjuryEvent).filter(InjuryEvent.provider == "api_sports").one()
    assert row.player_id == "fbs:p1"
    assert row.coverage_complete is True
    assert row.game_id == "espn:fbs:fixture1"

    other = availability_for_player(
        league_id="fbs",
        kickoff=now + timedelta(hours=20),
        player_injury=None,
        game_has_complete_report=True,
        now=now,
    )
    assert other["coverage"] == "report_received_complete"
    assert other["availability_status"] == "probable"


def test_fbs_without_report_never_healthy():
    now = datetime.now(UTC)
    info = availability_for_player(
        league_id="fbs",
        kickoff=now + timedelta(hours=12),
        player_injury=None,
        game_has_complete_report=False,
        now=now,
    )
    assert info["coverage"] == "source_unavailable"
    assert "NEVER_ASSUME_HEALTHY" in info["abstain_reasons"]
