"""CFB Anytime TD board trust: current-team priors + research sportsbook attach."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from spe.identity.roster import _open_roster_at_kickoff
from spe.models.player_td.score import _player_priors_from_hist
from spe.research.atd_research_prices import attach_research_price
from spe.research.m5_fbs_lock_handoff import _score_c_recent


def _hist_frame() -> pd.DataFrame:
    # Deuce-like: Auburn blowup then Ole Miss backup touches.
    rows = [
        {"player_id": "fbs:5141506", "team_id": "fbs:2", "kickoff_utc": "2025-09-06T18:00:00Z", "season_year": 2025, "carries": 4, "rush_td": 0, "rec_td": 0, "anytime_tds": 0, "receptions": 0, "pass_td": 0},
        {"player_id": "fbs:5141506", "team_id": "fbs:2", "kickoff_utc": "2025-11-22T18:00:00Z", "season_year": 2025, "carries": 9, "rush_td": 4, "rec_td": 0, "anytime_tds": 4, "receptions": 0, "pass_td": 2},
        {"player_id": "fbs:5141506", "team_id": "fbs:145", "kickoff_utc": "2026-09-12T23:45:00Z", "season_year": 2026, "carries": 4, "rush_td": 1, "rec_td": 0, "anytime_tds": 1, "receptions": 0, "pass_td": 0},
        {"player_id": "fbs:5141506", "team_id": "fbs:145", "kickoff_utc": "2026-10-10T19:30:00Z", "season_year": 2026, "carries": 4, "rush_td": 0, "rec_td": 0, "anytime_tds": 0, "receptions": 0, "pass_td": 0},
        {"player_id": "fbs:5141506", "team_id": "fbs:145", "kickoff_utc": "2026-10-31T16:45:00Z", "season_year": 2026, "carries": 3, "rush_td": 1, "rec_td": 0, "anytime_tds": 1, "receptions": 0, "pass_td": 0},
    ]
    return pd.DataFrame(rows)


def test_current_team_recent8_excludes_prior_org_blowup() -> None:
    hist = _hist_frame()
    kick = datetime(2026, 11, 8, 0, 30, tzinfo=UTC)
    all_teams = _player_priors_from_hist(hist, "fbs:5141506", kick, "fbs")
    scoped = _player_priors_from_hist(
        hist, "fbs:5141506", kick, "fbs", current_team_id="fbs:145"
    )
    # All-team mean is (0+4+1+0+1)/5 = 1.2 — the Deuce board bug.
    assert abs(float(all_teams["recent8_td_rate"]) - 1.2) < 1e-9
    # Current-team Ole Miss only: (1+0+1)/3 ≈ 0.667
    assert scoped["recent8_td_rate_current_team"] is not None
    assert abs(float(scoped["recent8_td_rate_current_team"]) - (2 / 3)) < 1e-9
    assert "prior_org_games_excluded_from_current_team_windows" in (scoped.get("scope_flags") or [])
    # Workload also prefers current team (not Auburn 9-carry game dominating alone).
    assert scoped["prior_carries"] is not None
    assert float(scoped["prior_carries"]) < 5.0


def test_score_c_recent_prefers_current_team_recent8(monkeypatch) -> None:
    captured: dict = {}

    def fake_predict(artifact, *, player_id, team_id, position_group, row):
        captured["row"] = dict(row)
        return {"status": "ok", "p_atd": 0.25}

    monkeypatch.setattr(
        "spe.research.m5_fbs_lock_handoff.predict_m5_strict",
        fake_predict,
    )
    pl = {
        "player_id": "fbs:x",
        "team_id": "fbs:145",
        "position_group": "QB",
        "recent8_td_rate": 1.2,
        "recent8_td_rate_current_team": 0.6667,
        "prior_td_rate": 1.2,
        "roll3_td_rate": 0.5,
        "prior_carries": 3.5,
        "prior_carry_share": 0.1,
        "opp_def_z": 0.0,
        "is_home": 1.0,
    }
    out = _score_c_recent({"model": "stub"}, pl)
    assert out["status"] == "ok"
    assert abs(float(captured["row"]["prior_td_rate"]) - 0.6667) < 1e-6
    assert captured["row"]["prior_td_rate_source"] == "recent8_td_rate_current_team"


def test_open_roster_keeps_continuing_prior_year_starters(db) -> None:
    """2026 refresh must not erase still-open 2025 starters on the same club."""
    from spe.db.models import League, Player, Roster, Sport, Team

    kick = datetime(2026, 11, 8, 0, 30, tzinfo=UTC)
    db.add(Sport(sport_id="football", name="American football"))
    db.flush()
    db.add(League(league_id="fbs", sport_id="football", name="FBS", classification="fbs"))
    db.flush()
    db.add(Team(team_id="fbs:145", league_id="fbs", name="Ole Miss", abbreviation="MISS", aliases=[]))
    db.flush()
    players = [
        Player(player_id="fbs:starter", name="Starter QB", position_group="QB", position="QB", aliases=[]),
        Player(player_id="fbs:transfer", name="Transfer QB", position_group="QB", position="QB", aliases=[]),
    ]
    for i in range(20):
        players.append(
            Player(player_id=f"fbs:pad{i}", name=f"Pad {i}", position_group="WR", position="WR", aliases=[])
        )
    db.add_all(players)
    db.flush()
    db.add_all(
        [
            # Continuing starter stamped in prior season year, still open.
            Roster(
                team_id="fbs:145",
                player_id="fbs:starter",
                valid_from=datetime(2025, 8, 1, tzinfo=UTC),
                valid_to=None,
                status="active",
            ),
            Roster(
                team_id="fbs:145",
                player_id="fbs:transfer",
                valid_from=datetime(2026, 8, 1, tzinfo=UTC),
                valid_to=None,
                status="active",
            ),
        ]
        + [
            Roster(
                team_id="fbs:145",
                player_id=f"fbs:pad{i}",
                valid_from=datetime(2026, 8, 1, tzinfo=UTC),
                valid_to=None,
                status="active",
            )
            for i in range(20)
        ]
    )
    db.commit()
    elig = _open_roster_at_kickoff(db, {"fbs:145"}, kick, season_year=2026)
    ids = {e.player_id for e in elig}
    assert "fbs:starter" in ids
    assert "fbs:transfer" in ids


def test_fbs_eligibility_supplements_priced_player_after_false_closure(db) -> None:
    """Players with game markets must remain eligible even if roster was wrongly closed."""
    from spe.db.models import Game, League, Market, Outcome, Player, Roster, Season, Sport, Team, Venue
    from spe.identity.roster import eligible_players_for_game

    kick = datetime(2026, 11, 8, 0, 30, tzinfo=UTC)
    db.add(Sport(sport_id="football", name="American football"))
    db.flush()
    db.add(League(league_id="fbs", sport_id="football", name="FBS", classification="fbs"))
    db.flush()
    db.add(
        Market(
            market_id="anytime_td",
            sport="football",
            market_type="player_prop",
            period="game",
            stat="anytime_td",
            grading_rule="player_scores_td",
            odds_api_key="player_anytime_td",
        )
    )
    db.add(
        Season(
            season_id="fbs-2026",
            league_id="fbs",
            year=2026,
            start_at=datetime(2026, 8, 1, tzinfo=UTC),
            end_at=datetime(2027, 1, 15, tzinfo=UTC),
        )
    )
    db.add(Venue(venue_id="v1", name="Vaught", lat=1, lon=1, roof_type="open", surface="grass"))
    db.add_all(
        [
            Team(team_id="fbs:145", league_id="fbs", name="Ole Miss", abbreviation="MISS", aliases=[]),
            Team(team_id="fbs:61", league_id="fbs", name="Georgia", abbreviation="UGA", aliases=[]),
        ]
    )
    db.flush()
    db.add(Player(player_id="fbs:qb", name="Starter QB", position_group="QB", position="QB", aliases=[]))
    db.flush()
    db.add(
        Roster(
            team_id="fbs:61",
            player_id="fbs:qb",
            valid_from=datetime(2022, 8, 1, tzinfo=UTC),
            valid_to=datetime(2026, 11, 7, 1, 28, 25, tzinfo=UTC),
            status="active",
        )
    )
    db.add(
        Game(
            game_id="espn:fbs:test1",
            season_id="fbs-2026",
            league_id="fbs",
            home_team_id="fbs:145",
            away_team_id="fbs:61",
            kickoff_utc=kick,
            venue_id="v1",
            data_tier="upcoming",
            status="scheduled",
        )
    )
    db.add(
        Outcome(
            outcome_id="o1",
            game_id="espn:fbs:test1",
            market_id="anytime_td",
            player_id="fbs:qb",
            description="Starter QB",
            side="yes",
            line=None,
        )
    )
    db.commit()
    elig = eligible_players_for_game(db, db.get(Game, "espn:fbs:test1"))
    hit = next(e for e in elig if e.player_id == "fbs:qb")
    assert hit.source == "game_market_outcome"
    assert hit.team_id == "fbs:61"


def test_season_box_supplement_ignores_opponent_only_history(db) -> None:
    """A past game vs a participant must not put the opponent's star on this board."""
    from spe.db.models import Game, League, Player, PlayerGameStat, Roster, Season, Sport, Team, Venue
    from spe.identity.roster import eligible_players_for_game

    kick = datetime(2026, 11, 8, 18, tzinfo=UTC)
    db.add(Sport(sport_id="football", name="American football"))
    db.flush()
    db.add(League(league_id="fbs", sport_id="football", name="FBS", classification="fbs"))
    db.flush()
    db.add(
        Season(
            season_id="fbs-2026",
            league_id="fbs",
            year=2026,
            start_at=datetime(2026, 8, 1, tzinfo=UTC),
            end_at=datetime(2027, 1, 15, tzinfo=UTC),
        )
    )
    db.add(Venue(venue_id="v1", name="Field", lat=1, lon=1, roof_type="open", surface="grass"))
    db.add_all(
        [
            Team(team_id="fbs:2", league_id="fbs", name="Auburn", abbreviation="AUB", aliases=[]),
            Team(team_id="fbs:8", league_id="fbs", name="Arkansas", abbreviation="ARK", aliases=[]),
            Team(
                team_id="uga",
                league_id="fbs",
                name="Georgia",
                abbreviation="UGA",
                aliases=[{"espn_id": "61"}],
            ),
            Team(team_id="fbs:61", league_id="fbs", name="Georgia", abbreviation="UGA", aliases=[]),
        ]
    )
    db.flush()
    db.add(Player(player_id="fbs:qb", name="Gunner", position_group="QB", position="QB", aliases=[]))
    db.flush()
    # Closed Georgia roster (false prior-year sync) + null-team_id box vs Auburn.
    db.add(
        Roster(
            team_id="fbs:61",
            player_id="fbs:qb",
            valid_from=datetime(2022, 8, 1, tzinfo=UTC),
            valid_to=datetime(2026, 11, 7, 1, 28, 25, tzinfo=UTC),
            status="active",
        )
    )
    prior = Game(
        game_id="espn:fbs:prior",
        season_id="fbs-2026",
        league_id="fbs",
        home_team_id="uga",
        away_team_id="fbs:2",
        kickoff_utc=datetime(2026, 10, 17, 19, 30, tzinfo=UTC),
        venue_id="v1",
        data_tier="final",
        status="final",
    )
    upcoming = Game(
        game_id="espn:fbs:ark_aub",
        season_id="fbs-2026",
        league_id="fbs",
        home_team_id="fbs:2",
        away_team_id="fbs:8",
        kickoff_utc=kick,
        venue_id="v1",
        data_tier="upcoming",
        status="scheduled",
    )
    db.add_all([prior, upcoming])
    db.flush()
    db.add(
        PlayerGameStat(
            game_id="espn:fbs:prior",
            player_id="fbs:qb",
            team_id=None,
            attempts=20,
            carries=5,
            receptions=0,
            provider="test",
            provider_record_id="1",
            observed_at=kick,
            effective_at=kick,
        )
    )
    db.commit()
    elig = eligible_players_for_game(db, db.get(Game, "espn:fbs:ark_aub"))
    assert not any(e.player_id == "fbs:qb" for e in elig)


def test_attach_research_price_priced_and_unpriced() -> None:
    idx = {
        ("g1", "p1"): {
            "status": "priced",
            "book_price": 250,
            "bookmaker": "DraftKings",
            "captured_at": "2026-11-07T14:00:00",
            "price_freshness": {"status": "fresh", "age_minutes": 10, "is_current_opportunity": True},
        }
    }
    priced = attach_research_price({"player_id": "p1", "model_source": {}}, game_id="g1", price_index=idx)
    assert priced["sportsbook"]["status"] == "priced"
    assert priced["sportsbook"]["book_price"] == 250
    assert priced["model_source"]["price_captured_at"] == "2026-11-07T14:00:00"

    bare = attach_research_price({"player_id": "p2", "model_source": {}}, game_id="g1", price_index=idx)
    assert bare["sportsbook"]["status"] == "unpriced"
    assert bare["sportsbook"]["book_price"] is None
    assert bare["sportsbook"]["price_freshness"]["status"] == "missing"
