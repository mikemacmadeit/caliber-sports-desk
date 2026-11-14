"""Phase 2 reliability regressions: budget, freshness, identity, injuries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.queries import price_freshness
from spe.config import settings
from spe.db.base import as_utc, new_id
from spe.db.models import (
    Bookmaker,
    DataCoverage,
    Forecast,
    Game,
    League,
    Market,
    ModelVersion,
    OddsTick,
    Outcome,
    Player,
    RosterWeek,
    Season,
    Sport,
    Team,
    Venue,
)
from spe.db.session import init_db, session_factory
from spe.ingest import odds_budget
from spe.ingest.api_sports_injuries import ingest_api_sports_injuries
from spe.ingest.odds_live import _store_tick
from spe.integrity.assignments import IDENTITY_CONFLICT, quarantine_conflicts
from spe.ops.availability import availability_for_player
from spe.ops.freshness import injury_coverage_health, odds_freshness_health


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    url = f"sqlite:///{tmp_path.as_posix()}/phase2.db"
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "odds_credit_budget_per_day", 100)
    monkeypatch.setattr(settings, "odds_credit_budget_temporary", 0)
    monkeypatch.setattr(settings, "odds_credit_budget_temporary_until", "")
    monkeypatch.setattr(settings, "odds_near_reserve_credits", 40)
    monkeypatch.setattr(settings, "max_price_age_minutes", 45)
    import spe.db.session as sess

    sess._engine = None
    sess._SessionLocal = None
    init_db()
    s = session_factory()
    try:
        yield s
    finally:
        s.close()
        sess._engine = None
        sess._SessionLocal = None


def test_concurrent_budget_consumers_cannot_double_spend(db: Session, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "odds_credit_budget_per_day", 50)
    monkeypatch.setattr(settings, "odds_credit_budget_temporary", 0)
    odds_budget.ensure_credit_row(db)
    db.commit()

    def worker(_: int) -> bool:
        s = session_factory()
        try:
            ok = odds_budget.reserve_credits(s, 30)
            s.commit()
            return ok
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(worker, range(4)))
    assert sum(1 for r in results if r) == 1
    db.expire_all()
    assert odds_budget.used_credits(db) == 30
    assert odds_budget.remaining_soft_budget(db) == 20


def test_temporary_ceiling_is_total_not_additive_grant(db: Session, monkeypatch: pytest.MonkeyPatch):
    """Option B raises the total ceiling; remaining = ceiling - already_used."""
    monkeypatch.setattr(settings, "odds_credit_budget_per_day", 2000)
    monkeypatch.setattr(settings, "odds_credit_budget_temporary", 2312)
    monkeypatch.setattr(settings, "odds_credit_budget_temporary_until", "2099-01-01T00:00:00+00:00")
    assert odds_budget.effective_soft_budget() == 2312
    odds_budget.ensure_credit_row(db)
    # Simulate prior usage under the old cap.
    assert odds_budget.reserve_credits(db, 2051)
    db.flush()
    assert odds_budget.remaining_soft_budget(db) == 261
    assert odds_budget.reserve_credits(db, 261)
    db.flush()
    assert odds_budget.remaining_soft_budget(db) == 0
    assert not odds_budget.reserve_credits(db, 1)


def test_temporary_ceiling_expires_to_standard(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "odds_credit_budget_per_day", 2000)
    monkeypatch.setattr(settings, "odds_credit_budget_temporary", 2312)
    monkeypatch.setattr(settings, "odds_credit_budget_temporary_until", "2020-01-01T00:00:00+00:00")
    assert odds_budget.effective_soft_budget() == 2000
    assert odds_budget.temporary_budget_active() is False


def test_temporary_ceiling_expires_exactly_at_reset_instant(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "odds_credit_budget_per_day", 2000)
    monkeypatch.setattr(settings, "odds_credit_budget_temporary", 2312)
    monkeypatch.setattr(settings, "odds_credit_budget_temporary_until", "2026-11-08T00:00:00+00:00")
    before = datetime(2026, 11, 7, 23, 59, 59, tzinfo=UTC)
    at = datetime(2026, 11, 8, 0, 0, 0, tzinfo=UTC)
    after = datetime(2026, 11, 8, 0, 0, 1, tzinfo=UTC)
    assert odds_budget.effective_soft_budget(before) == 2312
    assert odds_budget.effective_soft_budget(at) == 2000
    assert odds_budget.effective_soft_budget(after) == 2000
    assert odds_budget.spendable_for_bucket.__doc__  # smoke: helper still present


def test_near_reserve_protected_from_mid_far(db: Session, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "odds_credit_budget_per_day", 100)
    monkeypatch.setattr(settings, "odds_credit_budget_temporary", 0)
    monkeypatch.setattr(settings, "odds_near_reserve_credits", 40)
    assert odds_budget.reserve_credits(db, 60)
    db.flush()
    assert odds_budget.spendable_for_bucket(db, "mid") == 0
    assert odds_budget.spendable_for_bucket(db, "far") == 0
    assert odds_budget.spendable_for_bucket(db, "near") == 40
    assert odds_budget.reserve_credits(db, 40)
    db.flush()
    assert odds_budget.remaining_soft_budget(db) == 0
    # Soft cap is hard — near emergency must not authorize overspend.
    assert odds_budget.spendable_for_bucket(db, "near", allow_near_emergency=True) == 0
    assert not odds_budget.force_spend_near_emergency(db, 1)
    assert odds_budget.soft_overspend(db) == 0


def test_soft_cap_blocks_new_spend_when_exhausted(db: Session, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "odds_credit_budget_per_day", 50)
    monkeypatch.setattr(settings, "odds_credit_budget_temporary", 0)
    monkeypatch.setattr(settings, "odds_near_reserve_credits", 40)
    assert odds_budget.reserve_credits(db, 50)
    db.flush()
    assert odds_budget.remaining_soft_budget(db) == 0
    assert not odds_budget.reserve_credits(db, 1)
    assert not odds_budget.force_spend_near_emergency(db, 4)
    # Ledger may record post-call actuals above the cap without authorizing more.
    odds_budget.record_provider_actual(db, 3)
    db.flush()
    assert odds_budget.used_credits(db) == 53
    assert not odds_budget.reserve_credits(db, 1)


def test_duplicate_refresh_shares_event_dedupe_key(db: Session):
    from spe.db.models import RawSnapshot
    from spe.ingest.odds_live import _recently_fetched

    now = datetime(2026, 11, 7, 18, 0, tzinfo=UTC)
    db.add(
        RawSnapshot(
            snapshot_id=new_id(),
            provider="odds_api",
            endpoint="odds_event:evt1",
            provider_record_id="evt1",
            observed_at=now - timedelta(minutes=5),
            payload={"dataset": "spreads", "observation_ok": True},
            license_name="odds_api",
            credit_cost=0,
        )
    )
    db.flush()
    assert _recently_fetched(db, "evt1", now, timedelta(minutes=20))
    assert not _recently_fetched(db, "evt1", now, timedelta(minutes=2))


def test_unchanged_quote_reobserve_keeps_fresh(db: Session):
    game, outcome, book = _seed_priced_outcome(db, kickoff_hours=2)
    now = datetime.now(UTC)
    old = now - timedelta(minutes=90)
    tick = OddsTick(
        tick_id=new_id(),
        outcome_id=outcome.outcome_id,
        bookmaker_id=book.bookmaker_id,
        price_american=-110,
        captured_at=old,
        source_at=old,
        is_live=False,
        credit_cost=1,
        provider="odds_api",
        provider_record_id="t1",
        observed_at=old,
        effective_at=old,
        quality_status="ok",
    )
    db.add(tick)
    db.flush()
    catalog = [("nfl:p1", "Test Player")]
    added, unmatched, unlinked, changed = _store_tick(
        db,
        game,
        {
            "market_key": "player_anytime_td",
            "bookmaker": book.bookmaker_id,
            "description": "Test Player",
            "name": "Yes",
            "point": None,
            "price_american": -110,
            "source_at": (now - timedelta(minutes=1)).isoformat(),
            "provider_record_id": "t1",
        },
        catalog,
        now,
        0,
    )
    assert added == 1
    assert changed is False
    assert unmatched == 0
    assert unlinked == 0
    db.flush()
    db.refresh(tick)
    observed = as_utc(tick.observed_at)
    assert observed is not None
    assert abs((observed - now).total_seconds()) < 5
    fresh = price_freshness(tick, now=now)
    assert fresh["status"] == "fresh"
    assert fresh["is_current_opportunity"] is True
    assert fresh["verification"] == "observed"


def test_budget_blocked_quality_not_opportunity(db: Session):
    _, outcome, book = _seed_priced_outcome(db, kickoff_hours=2)
    now = datetime.now(UTC)
    tick = OddsTick(
        tick_id=new_id(),
        outcome_id=outcome.outcome_id,
        bookmaker_id=book.bookmaker_id,
        price_american=-105,
        captured_at=now - timedelta(hours=3),
        source_at=now - timedelta(hours=3),
        is_live=False,
        credit_cost=0,
        provider="odds_api",
        provider_record_id="blocked",
        observed_at=now - timedelta(hours=3),
        effective_at=now - timedelta(hours=3),
        quality_status="budget_blocked",
    )
    db.add(tick)
    db.flush()
    fresh = price_freshness(tick, now=now)
    assert fresh["status"] == "stale"
    assert fresh["is_current_opportunity"] is False
    assert fresh["verification"] == "budget_blocked"


def test_wrong_team_player_stays_unlinked(db: Session):
    game, _, book = _seed_priced_outcome(db, kickoff_hours=3)
    db.add(Player(player_id="nfl:stranger", name="Stranger Danger", position_group="WR", position="WR", aliases=[]))
    db.flush()
    added, unmatched, unlinked, _ = _store_tick(
        db,
        game,
        {
            "market_key": "player_anytime_td",
            "bookmaker": book.bookmaker_id,
            "description": "Stranger Danger",
            "name": "Yes",
            "price_american": 200,
            "provider_record_id": "x",
        },
        [("nfl:stranger", "Stranger Danger")],
        datetime.now(UTC),
        0,
    )
    assert unmatched == 1
    assert unlinked == 1
    assert db.query(Outcome).filter(Outcome.player_id == "nfl:stranger").count() == 0


@pytest.mark.parametrize("allowed", [True, False])
def test_batch_quote_reuses_validated_roster_without_losing_exclusions(db, monkeypatch, allowed):
    game, _, book = _seed_priced_outcome(db, kickoff_hours=3)
    def unexpected_audit(*args, **kwargs):
        raise AssertionError("Do not repeat the full roster audit for each quote")
    monkeypatch.setattr("spe.ingest.odds_live._player_on_game_roster", unexpected_audit)
    for price in (200, 210, 220):
        _, unmatched, unlinked, _ = _store_tick(
            db, game,
            {"market_key": "player_anytime_td", "bookmaker": book.bookmaker_id,
             "description": "Test Player", "name": "Yes", "price_american": price},
            [("nfl:p1", "Test Player")], datetime.now(UTC), 0,
            eligible_ids={"nfl:p1"} if allowed else set(),
        )
        assert (unmatched, unlinked) == ((0, 0) if allowed else (1, 1))


def test_ambiguous_name_stays_unlinked(db: Session):
    game, _, book = _seed_priced_outcome(db, kickoff_hours=3)
    catalog = [("nfl:p1", "Test Player")]
    added, unmatched, unlinked, _ = _store_tick(
        db,
        game,
        {
            "market_key": "player_anytime_td",
            "bookmaker": book.bookmaker_id,
            "description": "T Player",
            "name": "Yes",
            "price_american": 250,
            "provider_record_id": "y",
        },
        catalog,
        datetime.now(UTC),
        0,
    )
    assert unmatched == 1
    assert unlinked == 1


def test_plan_blocked_injuries_never_healthy(db: Session):
    now = datetime.now(UTC)
    _seed_base(db)
    db.add(League(league_id="fbs", sport_id="football", name="FBS", classification="fbs"))
    db.flush()
    db.add(
        Season(
            season_id="fbs-2026",
            league_id="fbs",
            year=2026,
            start_at=now - timedelta(days=30),
            end_at=now + timedelta(days=60),
        )
    )
    db.add(
        DataCoverage(
            coverage_id=new_id(),
            provider="api_sports",
            dataset="injuries",
            league_id="fbs",
            season_year=2026,
            row_count=0,
            status="plan_blocked",
            notes="Free plan blocked",
            last_ingested_at=now,
        )
    )
    db.flush()
    summary = ingest_api_sports_injuries(db, league_id="fbs", season=2026, now=now)
    assert summary["status"] == "plan_blocked"
    assert summary.get("skipped_known_blocked") is True
    info = availability_for_player(
        league_id="fbs",
        kickoff=now + timedelta(hours=20),
        player_injury=None,
        game_has_complete_report=False,
        now=now,
    )
    assert info["availability_status"] == "unknown"
    assert "NEVER_ASSUME_HEALTHY" in info["abstain_reasons"]
    assert info["coverage"] == "source_unavailable"
    health = injury_coverage_health(db, now)
    assert health["honesty"]["never_assume_healthy"] is True


def test_freshness_windows_exclude_started_from_pregame(db: Session):
    now = datetime.now(UTC)
    _seed_base(db)
    book = db.get(Bookmaker, "draftkings")
    assert book is not None
    for gid, hours, status in [
        ("g-near", 2, "scheduled"),
        ("g-later", 30, "scheduled"),
        ("g-live", -1, "in_progress"),
    ]:
        db.add(
            Game(
                game_id=gid,
                season_id="nfl-2026",
                league_id="nfl",
                week=10,
                home_team_id="nfl:HOME",
                away_team_id="nfl:AWAY",
                kickoff_utc=now + timedelta(hours=hours),
                venue_id="v1",
                status=status,
                data_tier="upcoming",
            )
        )
        outcome = Outcome(
            outcome_id=new_id(),
            market_id="anytime_td",
            game_id=gid,
            player_id=None,
            side="yes",
            line=None,
            description="x",
        )
        db.add(outcome)
        db.flush()
        age = 10 if hours >= 0 else 5
        db.add(
            OddsTick(
                tick_id=new_id(),
                outcome_id=outcome.outcome_id,
                bookmaker_id="draftkings",
                price_american=-110,
                captured_at=now - timedelta(minutes=age),
                source_at=now - timedelta(minutes=age),
                is_live=False,
                credit_cost=0,
                provider="odds_api",
                provider_record_id=gid,
                observed_at=now - timedelta(minutes=age),
                effective_at=now - timedelta(minutes=age),
                quality_status="ok",
            )
        )
    db.flush()
    health = odds_freshness_health(db, now)
    assert health["windows"]["in_progress"]["price_rows"] >= 1
    assert health["pregame_price_rows"] == (
        health["windows"]["near_pregame"]["price_rows"] + health["windows"]["later_pregame"]["price_rows"]
    )


def test_quarantined_identity_conflict_not_official(db: Session):
    game, outcome, _ = _seed_priced_outcome(db, kickoff_hours=4)
    now = datetime.now(UTC)
    db.add(
        ModelVersion(
            model_version_id="m1",
            code_sha="test",
            training_cutoff=now,
            data_snapshot="seed",
            hyperparameters={},
            calibration_version="0",
            league_id="nfl",
            family="test",
        )
    )
    db.flush()
    fc = Forecast(
        forecast_id=new_id(),
        outcome_id=outcome.outcome_id,
        model_version_id="m1",
        as_of=now,
        data_cutoff=now,
        p=0.2,
        p_low=0.1,
        p_high=0.3,
        sim_count=100,
        availability_status="probable",
        reason_codes=[],
        is_official=True,
        data_tier="official",
    )
    db.add(fc)
    db.flush()
    n = quarantine_conflicts(
        db,
        {
            "rows": [
                {
                    "forecast_id": fc.forecast_id,
                    "outcome_id": outcome.outcome_id,
                    "problems": ["NO_TEAM_MEMBERSHIP"],
                }
            ]
        },
    )
    assert n == 1
    db.refresh(fc)
    assert fc.is_official is False
    assert IDENTITY_CONFLICT in (fc.reason_codes or [])


def _seed_base(db: Session) -> None:
    if db.get(Sport, "football") is None:
        db.add(Sport(sport_id="football", name="American football"))
        db.flush()
    if db.get(League, "nfl") is None:
        db.add(League(league_id="nfl", sport_id="football", name="NFL", classification="nfl"))
        db.flush()
    if db.get(Season, "nfl-2026") is None:
        db.add(
            Season(
                season_id="nfl-2026",
                league_id="nfl",
                year=2026,
                start_at=datetime(2026, 9, 1, tzinfo=UTC),
                end_at=datetime(2027, 2, 1, tzinfo=UTC),
            )
        )
    if db.get(Venue, "v1") is None:
        db.add(Venue(venue_id="v1", name="Field", lat=1, lon=1, roof_type="open", surface="grass"))
    for tid, name, abbr in [("nfl:HOME", "Home Team", "HOM"), ("nfl:AWAY", "Away Team", "AWY")]:
        if db.get(Team, tid) is None:
            db.add(Team(team_id=tid, league_id="nfl", name=name, abbreviation=abbr, aliases=[]))
    if db.get(Market, "anytime_td") is None:
        db.add(
            Market(
                market_id="anytime_td",
                sport="football",
                market_type="player",
                period="game",
                stat="td",
                grading_rule="anytime",
                odds_api_key="player_anytime_td",
            )
        )
    if db.get(Bookmaker, "draftkings") is None:
        db.add(Bookmaker(bookmaker_id="draftkings", name="DraftKings"))
    if db.get(Player, "nfl:p1") is None:
        db.add(Player(player_id="nfl:p1", name="Test Player", position_group="WR", position="WR", aliases=[]))
    db.flush()


def _seed_priced_outcome(db: Session, *, kickoff_hours: float):
    _seed_base(db)
    now = datetime.now(UTC)
    gid = f"nfl:g-{int(kickoff_hours * 10)}"
    existing = db.get(Game, gid)
    if existing is None:
        game = Game(
            game_id=gid,
            season_id="nfl-2026",
            league_id="nfl",
            week=10,
            home_team_id="nfl:HOME",
            away_team_id="nfl:AWAY",
            kickoff_utc=now + timedelta(hours=kickoff_hours),
            venue_id="v1",
            status="scheduled",
            data_tier="upcoming",
        )
        db.add(game)
    else:
        game = existing
    if (
        db.query(RosterWeek)
        .filter_by(player_id="nfl:p1", team_id="nfl:HOME", season_year=2026, week=10)
        .one_or_none()
        is None
    ):
        db.add(
            RosterWeek(
                id=new_id(),
                player_id="nfl:p1",
                team_id="nfl:HOME",
                season_year=2026,
                week=10,
                position="WR",
                status="ACT",
                provider="test",
                provider_record_id="rw1",
                observed_at=now,
                effective_at=now,
            )
        )
    outcome = Outcome(
        outcome_id=new_id(),
        market_id="anytime_td",
        game_id=game.game_id,
        player_id="nfl:p1",
        side="yes",
        line=None,
        description="Test Player",
    )
    db.add(outcome)
    db.flush()
    return game, outcome, db.get(Bookmaker, "draftkings")
