"""Phase 1: canonical games, eligible rosters, upcoming player history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from spe.db.models import (
    FeatureRow,
    Game,
    League,
    Player,
    ProviderIdMap,
    Roster,
    RosterWeek,
    Season,
    Sport,
    Team,
    Venue,
)
from spe.identity.games import choose_canonical, find_duplicate_groups, repair_canonical_games
from spe.identity.roster import eligible_players_for_game, sync_current_rosters
from spe.integrity.phase1 import run_phase1_repair


def _base(db) -> None:
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
    db.add(Venue(venue_id="v1", name="Field", lat=1, lon=1, roof_type="open", surface="grass"))
    db.add_all(
        [
            Team(team_id="nfl:CIN", league_id="nfl", name="Cincinnati Bengals", abbreviation="CIN"),
            Team(team_id="nfl:ATL", league_id="nfl", name="Atlanta Falcons", abbreviation="ATL"),
        ]
    )
    db.flush()


def test_choose_canonical_prefers_nflverse_over_espn():
    kick = datetime(2026, 11, 8, 18, tzinfo=UTC)
    nfl = Game(
        game_id="nfl:2026_09_CIN_ATL",
        season_id="nfl-2026",
        league_id="nfl",
        home_team_id="nfl:ATL",
        away_team_id="nfl:CIN",
        kickoff_utc=kick,
        venue_id="v1",
        data_tier="upcoming",
        status="scheduled",
        week=9,
    )
    espn = Game(
        game_id="espn:nfl:401873000",
        season_id="nfl-2026",
        league_id="nfl",
        home_team_id="nfl:ATL",
        away_team_id="nfl:CIN",
        kickoff_utc=kick,
        venue_id="v1",
        data_tier="upcoming",
        status="scheduled",
        week=9,
    )
    assert choose_canonical([espn, nfl]).game_id == "nfl:2026_09_CIN_ATL"


def test_repair_marks_espn_alias_and_maps(db):
    _base(db)
    kick = datetime(2026, 11, 8, 18, tzinfo=UTC)
    db.add_all(
        [
            Game(
                game_id="nfl:2026_09_CIN_ATL",
                season_id="nfl-2026",
                league_id="nfl",
                home_team_id="nfl:ATL",
                away_team_id="nfl:CIN",
                kickoff_utc=kick,
                venue_id="v1",
                data_tier="upcoming",
                status="scheduled",
                week=9,
            ),
            Game(
                game_id="espn:nfl:401873000",
                season_id="nfl-2026",
                league_id="nfl",
                home_team_id="nfl:ATL",
                away_team_id="nfl:CIN",
                kickoff_utc=kick,
                venue_id="v1",
                data_tier="upcoming",
                status="scheduled",
                week=9,
            ),
        ]
    )
    db.commit()
    groups = find_duplicate_groups(db, upcoming_only=True)
    assert len(groups) == 1
    summary = repair_canonical_games(db, upcoming_only=True)
    db.commit()
    assert summary["aliases_marked"] == 1
    alias = db.get(Game, "espn:nfl:401873000")
    assert alias is not None and alias.data_tier == "alias"
    mapped = (
        db.query(ProviderIdMap)
        .filter_by(provider="caliber", entity_type="game_alias", external_id="espn:nfl:401873000")
        .one()
    )
    assert mapped.canonical_id == "nfl:2026_09_CIN_ATL"


def test_eligible_roster_uses_week_not_historical_open_rows(db):
    _base(db)
    kick = datetime(2026, 11, 8, 18, tzinfo=UTC)
    db.add(
        Game(
            game_id="nfl:2026_09_CIN_ATL",
            season_id="nfl-2026",
            league_id="nfl",
            home_team_id="nfl:ATL",
            away_team_id="nfl:CIN",
            kickoff_utc=kick,
            venue_id="v1",
            data_tier="upcoming",
            status="scheduled",
            week=9,
        )
    )
    db.add_all(
        [
            Player(player_id="nfl:00-0036900", name="Ja'Marr Chase", position_group="WR", position="WR", aliases=[]),
            Player(player_id="nfl:00-0020000", name="Retired WR", position_group="WR", position="WR", aliases=[]),
        ]
    )
    # Contaminating open historical memberships.
    db.add_all(
        [
            Roster(
                team_id="nfl:ATL",
                player_id="nfl:00-0020000",
                valid_from=datetime(2018, 9, 1, tzinfo=UTC),
                valid_to=None,
                status="active",
            ),
            Roster(
                team_id="nfl:CIN",
                player_id="nfl:00-0036900",
                valid_from=datetime(2021, 9, 1, tzinfo=UTC),
                valid_to=None,
                status="active",
            ),
        ]
    )
    db.add(
        RosterWeek(
            season_year=2026,
            week=9,
            team_id="nfl:CIN",
            player_id="nfl:00-0036900",
            status="ACT",
            provider="nflverse",
            provider_record_id="rw1",
            observed_at=kick,
            effective_at=kick,
        )
    )
    db.commit()
    eligible = eligible_players_for_game(db, db.get(Game, "nfl:2026_09_CIN_ATL"))
    ids = {row.player_id for row in eligible}
    assert "nfl:00-0036900" in ids
    assert "nfl:00-0020000" not in ids


def test_sync_current_rosters_closes_contamination(db):
    _base(db)
    now = datetime(2026, 11, 6, tzinfo=UTC)
    db.add(
        Player(player_id="nfl:00-0036900", name="Ja'Marr Chase", position_group="WR", position="WR", aliases=[])
    )
    db.add(
        Roster(
            team_id="nfl:CIN",
            player_id="nfl:00-0036900",
            valid_from=datetime(2021, 9, 1, tzinfo=UTC),
            valid_to=None,
            status="active",
        )
    )
    db.add(
        RosterWeek(
            season_year=2026,
            week=9,
            team_id="nfl:CIN",
            player_id="nfl:00-0036900",
            status="ACT",
            provider="nflverse",
            provider_record_id="rw1",
            observed_at=now,
            effective_at=now,
        )
    )
    db.commit()
    summary = sync_current_rosters(db, now=now)
    db.commit()
    assert summary["nfl"]["opened"] == 1
    open_rows = db.query(Roster).filter(Roster.valid_to.is_(None)).all()
    assert len(open_rows) == 1
    assert open_rows[0].player_id == "nfl:00-0036900"
    assert open_rows[0].team_id == "nfl:CIN"
    closed = db.query(Roster).filter(Roster.valid_to.is_not(None)).count()
    assert closed >= 1


def test_phase1_writes_upcoming_features_with_history(db):
    _base(db)
    now = datetime(2026, 11, 6, 12, tzinfo=UTC)
    kick = now + timedelta(days=2)
    prior_kick = now - timedelta(days=7)
    db.add_all(
        [
            Game(
                game_id="nfl:2026_08_PRIOR",
                season_id="nfl-2026",
                league_id="nfl",
                home_team_id="nfl:ATL",
                away_team_id="nfl:CIN",
                kickoff_utc=prior_kick,
                venue_id="v1",
                data_tier="historical",
                status="final",
                week=8,
            ),
            Game(
                game_id="nfl:2026_09_CIN_ATL",
                season_id="nfl-2026",
                league_id="nfl",
                home_team_id="nfl:ATL",
                away_team_id="nfl:CIN",
                kickoff_utc=kick,
                venue_id="v1",
                data_tier="upcoming",
                status="scheduled",
                week=9,
            ),
            Player(player_id="nfl:00-0036900", name="Ja'Marr Chase", position_group="WR", position="WR", aliases=[]),
        ]
    )
    from spe.db.models import PlayerGameStat

    db.add(
        PlayerGameStat(
            game_id="nfl:2026_08_PRIOR",
            player_id="nfl:00-0036900",
            team_id="nfl:CIN",
            targets=8,
            receptions=5,
            rec_yards=90,
            carries=0,
            rush_yards=0,
            rush_td=0,
            rec_td=1,
            rz_targets=2,
            gl_carries=0,
            provider="nflverse",
            provider_record_id="pgs1",
            observed_at=prior_kick,
            effective_at=prior_kick,
        )
    )
    db.add(
        RosterWeek(
            season_year=2026,
            week=9,
            team_id="nfl:CIN",
            player_id="nfl:00-0036900",
            status="ACT",
            provider="nflverse",
            provider_record_id="rw1",
            observed_at=now,
            effective_at=now,
        )
    )
    db.commit()
    report = run_phase1_repair(db, verify_game_ids=["nfl:2026_09_CIN_ATL"], now=now)
    db.commit()
    assert report["play_enabled"] is False
    assert report["validated_play"] is False
    features = db.query(FeatureRow).filter(FeatureRow.game_id == "nfl:2026_09_CIN_ATL").all()
    assert features
    player_feat = next(r for r in features if r.player_id == "nfl:00-0036900")
    assert int(player_feat.sample_size) >= 1
    assert (player_feat.feature_blob or {}).get("usage", {}).get("targets_per_game") == 8.0
    verify = report["verification"][0]
    assert verify["ok"] is True
