"""Prospective shadow capture and settlement rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from spe.db.models import (
    Bookmaker,
    Forecast,
    Game,
    League,
    Market,
    ModelVersion,
    OddsTick,
    Outcome,
    Player,
    Season,
    ShadowPrediction,
    ShadowSettlement,
    Sport,
    Team,
    Venue,
)
from spe.market.odds import american_to_implied
from spe.models.validation import market_comparison_status
from spe.shadow.capture import capture_shadow_predictions
from spe.shadow.report import shadow_track_record
from spe.shadow.settle import _grade, settle_shadow_predictions


def test_grade_push_and_anytime_td():
    assert _grade("over", 49.5, 49.5, "receiving_yards") == "push"
    assert _grade("over", 49.5, 60.0, "receiving_yards") == "win"
    assert _grade("under", 49.5, 60.0, "receiving_yards") == "loss"
    assert _grade("yes", None, 1.0, "anytime_td") == "win"
    assert _grade("yes", None, 0.0, "anytime_td") == "loss"


def test_research_lines_remain_non_market():
    status = market_comparison_status(0, 0)
    assert status["research_lines_are_market_comparisons"] is False


def test_shadow_capture_is_append_only_and_tracks_abstention(db):
    s = db
    now = datetime.now(UTC)
    s.add(Sport(sport_id="football", name="Football"))
    s.flush()
    s.add(League(league_id="nfl", sport_id="football", name="NFL", classification="pro"))
    s.flush()
    s.add(
        Season(
            season_id="nfl-2026",
            league_id="nfl",
            year=2026,
            start_at=now - timedelta(days=30),
            end_at=now + timedelta(days=60),
        )
    )
    s.flush()
    s.add(Venue(venue_id="v", name="Arrowhead", lat=39.0, lon=-94.0, roof_type="open", surface="grass"))
    s.add(Team(team_id="nfl:KC", league_id="nfl", name="Chiefs", abbreviation="KC"))
    s.add(Team(team_id="nfl:BAL", league_id="nfl", name="Ravens", abbreviation="BAL"))
    s.add(Player(player_id="nfl:p1", name="Test Back", position_group="RB", position="RB"))
    s.add(
        Market(
            market_id="rushing_yards",
            sport="football",
            market_type="player_over_under",
            period="game",
            stat="rush_yards",
            grading_rule="over",
        )
    )
    s.add(
        Game(
            game_id="g1",
            season_id="nfl-2026",
            league_id="nfl",
            week=10,
            kickoff_utc=now + timedelta(hours=6),
            home_team_id="nfl:KC",
            away_team_id="nfl:BAL",
            venue_id="v",
            status="scheduled",
            data_tier="upcoming",
        )
    )
    s.add(
        ModelVersion(
            model_version_id="m3-nfl-rushing_yards",
            code_sha="test",
            training_cutoff=now - timedelta(days=1),
            data_snapshot="test",
            hyperparameters={"play_enabled": False},
            calibration_version="none",
            league_id="nfl",
            family="rushing_yards",
        )
    )
    s.flush()
    s.add(
        Outcome(
            outcome_id="o1",
            game_id="g1",
            market_id="rushing_yards",
            description="Test Back",
            player_id="nfl:p1",
            side="over",
            line=39.5,
        )
    )
    s.add(Bookmaker(bookmaker_id="draftkings", name="DraftKings"))
    s.flush()
    s.add(
        OddsTick(
            tick_id="t1",
            outcome_id="o1",
            bookmaker_id="draftkings",
            price_american=-110,
            captured_at=now,
            source_at=now,
            is_live=False,
            provider="odds_api",
            provider_record_id="t1",
            observed_at=now,
            effective_at=now,
        )
    )
    s.flush()
    s.add(
        Forecast(
            forecast_id="f1",
            outcome_id="o1",
            as_of=now,
            model_version_id="m3-nfl-rushing_yards",
            p=0.41,
            p_low=0.33,
            p_high=0.49,
            sim_count=0,
            data_cutoff=now,
            availability_status="unknown",
            reason_codes=["RESEARCH_ONLY", "PLAY_DISABLED", "INJURY_COVERAGE_INCOMPLETE"],
            components={"mean": 35.0, "line": 39.5, "side": "over", "play_enabled": False},
            is_official=True,
            data_tier="research",
        )
    )
    s.commit()

    first = capture_shadow_predictions(s, now=now)
    s.commit()
    second = capture_shadow_predictions(s, now=now)
    s.commit()
    assert first["written"] >= 1
    assert second["unchanged"] >= 1
    rows = s.query(ShadowPrediction).all()
    assert len(rows) == first["written"]
    assert all(row.play_enabled is False for row in rows)
    assert all(row.recommendation_status == "no_bet" for row in rows)
    assert any("INJURY_COVERAGE_INCOMPLETE" in (row.abstain_reasons or []) for row in rows)
    assert abs(rows[0].market_implied_raw - american_to_implied(-110)) < 1e-9
    assert rows[0].market_implied_novig is None

    s.query(Game).filter(Game.game_id == "g1").update({"status": "final", "data_tier": "historical"})
    s.commit()
    settled = settle_shadow_predictions(s, now=now + timedelta(hours=8))
    s.commit()
    assert settled["blocked"] >= 1
    block = s.query(ShadowSettlement).one()
    assert block.result == "blocked"
    assert block.void_reason == "missing_box_score_not_assumed_zero"
    again = settle_shadow_predictions(s, now=now + timedelta(hours=9))
    s.commit()
    assert s.query(ShadowSettlement).count() == 1
    assert again["already_settled"] >= 1 or again.get("status") == "nothing_open"

    report = shadow_track_record(s)
    assert report["play_enabled"] is False
    assert report["totals"]["blocked"] >= 1
    assert "INJURY_COVERAGE_INCOMPLETE" in report["abstain_reason_counts"]
    assert "injury_coverage" in report
    assert "odds_freshness" in report
    assert "job_execution" in report


def _seed_final_prop(db, *, shadow_id: str, side: str, line: float | None, market: str, price: int = -110):
    from spe.db.models import PlayerGameStat, InjuryEvent

    s = db
    now = datetime.now(UTC) - timedelta(days=1)
    if s.get(Sport, "football") is None:
        s.add(Sport(sport_id="football", name="Football"))
        s.flush()
    if s.get(League, "nfl") is None:
        s.add(League(league_id="nfl", sport_id="football", name="NFL", classification="pro"))
        s.flush()
    if s.get(Season, "nfl-2026") is None:
        s.add(
            Season(
                season_id="nfl-2026",
                league_id="nfl",
                year=2026,
                start_at=now - timedelta(days=30),
                end_at=now + timedelta(days=60),
            )
        )
        s.flush()
    if s.get(Venue, "v") is None:
        s.add(Venue(venue_id="v", name="Arrowhead", lat=39.0, lon=-94.0, roof_type="open", surface="grass"))
    if s.get(Team, "nfl:KC") is None:
        s.add(Team(team_id="nfl:KC", league_id="nfl", name="Chiefs", abbreviation="KC"))
    if s.get(Team, "nfl:BAL") is None:
        s.add(Team(team_id="nfl:BAL", league_id="nfl", name="Ravens", abbreviation="BAL"))
    s.flush()
    player_id = f"nfl:settlement-{shadow_id}"
    s.add(Player(player_id=player_id, name=f"Player {shadow_id}", position_group="WR", position="WR"))
    if s.get(Market, market) is None:
        s.add(
            Market(
                market_id=market,
                sport="football",
                market_type="player_prop",
                period="game",
                stat=market,
                grading_rule="over",
            )
        )
    s.flush()
    game_id = f"final-{shadow_id}"
    s.add(
        Game(
            game_id=game_id,
            season_id="nfl-2026",
            league_id="nfl",
            week=9,
            kickoff_utc=now,
            home_team_id="nfl:KC",
            away_team_id="nfl:BAL",
            venue_id="v",
            status="final",
            data_tier="historical",
        )
    )
    s.flush()
    outcome_id = f"out-{shadow_id}"
    s.add(
        Outcome(
            outcome_id=outcome_id,
            game_id=game_id,
            market_id=market,
            description=f"Player {shadow_id}",
            player_id=player_id,
            side=side,
            line=line,
        )
    )
    s.flush()
    if s.get(Bookmaker, "draftkings") is None:
        s.add(Bookmaker(bookmaker_id="draftkings", name="DraftKings"))
        s.flush()
    s.add(
        ShadowPrediction(
            shadow_id=shadow_id,
            player_id=player_id,
            game_id=game_id,
            league_id="nfl",
            market_id=market,
            outcome_id=outcome_id,
            bookmaker_id="draftkings",
            line=line,
            side=side,
            price_american=price,
            odds_tick_id=None,
            forecast_probability=0.55,
            market_implied_raw=american_to_implied(price),
            market_implied_novig=None,
            expected_value=0.02,
            model_version_id="m3-nfl-" + market,
            forecast_as_of=now - timedelta(hours=2),
            data_quality_status="ok",
            abstain_reasons=["RESEARCH_ONLY", "PLAY_DISABLED", "SHADOW_TRACK"],
            recommendation_status="no_bet",
            components={"play_enabled": False},
            research_only=True,
            play_enabled=False,
            created_at=now - timedelta(hours=2),
        )
    )
    s.flush()
    return player_id, game_id, now


def test_settlement_win_loss_push_void_missing_and_duplicate(db):
    from spe.db.models import InjuryEvent, PlayerGameStat

    s = db
    # Win
    pid, gid, now = _seed_final_prop(s, shadow_id="win1", side="over", line=49.5, market="receiving_yards")
    s.add(
        PlayerGameStat(
            game_id=gid,
            player_id=pid,
            team_id="nfl:KC",
            targets=8,
            receptions=6,
            rec_yards=80,
            rush_yards=0,
            carries=0,
            rush_td=0,
            rec_td=1,
            provider="test",
            provider_record_id="win1",
            observed_at=now,
            effective_at=now,
        )
    )
    # Loss
    pid2, gid2, _ = _seed_final_prop(s, shadow_id="loss1", side="over", line=49.5, market="receiving_yards")
    s.add(
        PlayerGameStat(
            game_id=gid2,
            player_id=pid2,
            team_id="nfl:KC",
            targets=3,
            receptions=2,
            rec_yards=20,
            rush_yards=0,
            carries=0,
            rush_td=0,
            rec_td=0,
            provider="test",
            provider_record_id="loss1",
            observed_at=now,
            effective_at=now,
        )
    )
    # Push
    pid3, gid3, _ = _seed_final_prop(s, shadow_id="push1", side="over", line=49.5, market="receiving_yards")
    s.add(
        PlayerGameStat(
            game_id=gid3,
            player_id=pid3,
            team_id="nfl:KC",
            targets=5,
            receptions=4,
            rec_yards=49.5,
            rush_yards=0,
            carries=0,
            rush_td=0,
            rec_td=0,
            provider="test",
            provider_record_id="push1",
            observed_at=now,
            effective_at=now,
        )
    )
    # Anytime TD loss (official zero is a real row)
    pid4, gid4, _ = _seed_final_prop(s, shadow_id="tdloss", side="yes", line=None, market="anytime_td")
    if s.get(Market, "anytime_td") is None:
        s.add(
            Market(
                market_id="anytime_td",
                sport="football",
                market_type="player_prop",
                period="game",
                stat="anytime_td",
                grading_rule="yes",
            )
        )
        s.flush()
    s.add(
        PlayerGameStat(
            game_id=gid4,
            player_id=pid4,
            team_id="nfl:KC",
            targets=4,
            receptions=2,
            rec_yards=18,
            rush_yards=0,
            carries=0,
            rush_td=0,
            rec_td=0,
            provider="test",
            provider_record_id="tdloss",
            observed_at=now,
            effective_at=now,
        )
    )
    # Confirmed inactive void
    pid5, gid5, kick = _seed_final_prop(s, shadow_id="void1", side="over", line=39.5, market="receiving_yards")
    s.add(
        InjuryEvent(
            injury_event_id="inj-void1",
            game_id=gid5,
            player_id=pid5,
            practice_status=None,
            designation="Out",
            reported_at=kick - timedelta(hours=3),
            source="test",
            coverage_complete=True,
            confidence=0.95,
            provider="test",
            provider_record_id="inj-void1",
            observed_at=kick - timedelta(hours=3),
            effective_at=kick - timedelta(hours=3),
        )
    )
    # Missing box score blocked
    _seed_final_prop(s, shadow_id="miss1", side="over", line=39.5, market="receiving_yards")
    s.commit()

    first = settle_shadow_predictions(s, now=now + timedelta(hours=5))
    s.commit()
    by = first["by_result"]
    assert by.get("win", 0) >= 1
    assert by.get("loss", 0) >= 2
    assert by.get("push", 0) >= 1
    assert by.get("void", 0) >= 1
    assert first["blocked"] >= 1

    second = settle_shadow_predictions(s, now=now + timedelta(hours=6))
    s.commit()
    assert second["already_settled"] >= first["settled"] or second.get("status") == "nothing_open"
    assert s.query(ShadowSettlement).count() == first["settled"]
