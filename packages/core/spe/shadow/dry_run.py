"""Isolated pregame→settlement dry run. Never writes production settlements."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from spe.db.base import Base, new_id
from spe.db.models import (
    Bookmaker,
    Game,
    League,
    Market,
    OddsTick,
    Outcome,
    Player,
    PlayerGameStat,
    Season,
    ShadowPrediction,
    ShadowSettlement,
    Sport,
    Team,
    Venue,
)
from spe.shadow.settle import settle_shadow_predictions


def run_settlement_dry_run() -> dict[str, Any]:
    """Exercise missing-stat blocking and real TD settlement on an isolated SQLite DB."""
    path = Path("data") / "atd_sim_dev" / "live_weekend" / "dry_run_fixture.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    url = f"sqlite:///{path.resolve().as_posix()}"
    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.close()

    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = SessionLocal()
    now = datetime.now(UTC)
    kick = now - timedelta(hours=5)
    freeze_at = kick - timedelta(hours=3)
    try:
        _seed_parents(session, kick=kick)
        missing_id = _add_shadow(session, player_id="dry:p_missing", freeze_at=freeze_at)
        scored_id = _add_shadow(session, player_id="dry:p_scored", freeze_at=freeze_at)
        session.add(
            PlayerGameStat(
                id=new_id(),
                game_id="dry:game1",
                player_id="dry:p_scored",
                team_id="dry:home",
                rush_td=1,
                rec_td=0,
                provider="dry_run",
                provider_record_id="dry-scored",
                observed_at=kick + timedelta(hours=3),
                effective_at=kick + timedelta(hours=3),
            )
        )
        zero_id = _add_shadow(session, player_id="dry:p_zero", freeze_at=freeze_at)
        session.add(
            PlayerGameStat(
                id=new_id(),
                game_id="dry:game1",
                player_id="dry:p_zero",
                team_id="dry:home",
                rush_td=0,
                rec_td=0,
                provider="dry_run",
                provider_record_id="dry-zero",
                observed_at=kick + timedelta(hours=3),
                effective_at=kick + timedelta(hours=3),
            )
        )
        session.commit()

        summary = settle_shadow_predictions(session, now=now)
        session.commit()
        settles = {s.shadow_id: s for s in session.query(ShadowSettlement).all()}

        results = {
            "missing_stats": {
                "shadow_id": missing_id,
                "result": _res(settles, missing_id),
                "void_reason": settles[missing_id].void_reason if missing_id in settles else None,
                "expected": "blocked",
                "pass": missing_id in settles
                and settles[missing_id].result == "blocked"
                and "missing_box_score_not_assumed_zero" in (settles[missing_id].void_reason or ""),
            },
            "scored_td": {
                "shadow_id": scored_id,
                "result": _res(settles, scored_id),
                "actual_value": settles[scored_id].actual_value if scored_id in settles else None,
                "expected": "win",
                "pass": scored_id in settles
                and settles[scored_id].result == "win"
                and float(settles[scored_id].actual_value or 0) >= 1,
            },
            "explicit_zero": {
                "shadow_id": zero_id,
                "result": _res(settles, zero_id),
                "actual_value": settles[zero_id].actual_value if zero_id in settles else None,
                "expected": "loss",
                "pass": zero_id in settles
                and settles[zero_id].result == "loss"
                and settles[zero_id].actual_value is not None
                and float(settles[zero_id].actual_value) == 0.0,
            },
        }
        return {
            "isolated_db": str(path),
            "settle_summary": summary,
            "cases": results,
            "all_pass": all(bool(v["pass"]) for v in results.values()),
            "confirms": [
                "Missing player_game_stats → blocked (not zero TD)",
                "Explicit 0 rush+rec TD → loss",
                "rush_td/rec_td sum ≥ 1 → anytime yes win",
                "Dry-run DB is isolated from production shadow_settlements",
            ],
            "play_enabled": False,
            "manufactured_production_settlements": False,
        }
    finally:
        session.close()
        engine.dispose()


def _res(settles: dict[str, ShadowSettlement], sid: str) -> str | None:
    return settles[sid].result if sid in settles else None


def _seed_parents(session: Session, *, kick: datetime) -> None:
    session.add(Sport(sport_id="football", name="Football"))
    session.add(League(league_id="nfl", sport_id="football", name="NFL", classification="nfl"))
    session.add(
        Season(
            season_id="dry:2026",
            league_id="nfl",
            year=2026,
            start_at=kick - timedelta(days=100),
            end_at=kick + timedelta(days=100),
        )
    )
    session.add_all(
        [
            Team(team_id="dry:home", league_id="nfl", name="Dry Home", abbreviation="DH"),
            Team(team_id="dry:away", league_id="nfl", name="Dry Away", abbreviation="DA"),
        ]
    )
    session.add(
        Venue(
            venue_id="dry:venue",
            name="Dry Venue",
            lat=0.0,
            lon=0.0,
            roof_type="dome",
            surface="turf",
        )
    )
    session.add(
        Market(
            market_id="anytime_td",
            sport="football",
            market_type="player_prop",
            period="game",
            stat="anytime_td",
            grading_rule="rush_td + rec_td > 0",
        )
    )
    session.add(Bookmaker(bookmaker_id="dry_book", name="Dry Book"))
    session.add_all(
        [
            Player(player_id="dry:p_missing", name="Missing Stats", position_group="RB", position="RB"),
            Player(player_id="dry:p_scored", name="Scored TD", position_group="RB", position="RB"),
            Player(player_id="dry:p_zero", name="Zero TD", position_group="WR", position="WR"),
        ]
    )
    session.add(
        Game(
            game_id="dry:game1",
            league_id="nfl",
            season_id="dry:2026",
            week=10,
            kickoff_utc=kick,
            home_team_id="dry:home",
            away_team_id="dry:away",
            venue_id="dry:venue",
            status="final",
            data_tier="historical",
        )
    )
    session.flush()


def _add_shadow(session: Session, *, player_id: str, freeze_at: datetime) -> str:
    outcome_id = new_id()
    tick_id = new_id()
    shadow_id = new_id()
    session.add(
        Outcome(
            outcome_id=outcome_id,
            game_id="dry:game1",
            player_id=player_id,
            market_id="anytime_td",
            side="yes",
            line=None,
            description="dry anytime yes",
        )
    )
    session.add(
        OddsTick(
            tick_id=tick_id,
            outcome_id=outcome_id,
            bookmaker_id="dry_book",
            price_american=150,
            captured_at=freeze_at,
            source_at=freeze_at,
            provider="dry_run",
            provider_record_id=tick_id,
            observed_at=freeze_at,
            effective_at=freeze_at,
        )
    )
    session.add(
        ShadowPrediction(
            shadow_id=shadow_id,
            player_id=player_id,
            game_id="dry:game1",
            league_id="nfl",
            market_id="anytime_td",
            outcome_id=outcome_id,
            bookmaker_id="dry_book",
            line=None,
            side="yes",
            price_american=150,
            odds_tick_id=tick_id,
            forecast_probability=0.35,
            market_implied_raw=0.4,
            market_implied_novig=None,
            expected_value=-0.1,
            model_version_id="atd_sim_v1-nfl-anytime_td",
            forecast_as_of=freeze_at,
            data_quality_status="research",
            abstain_reasons=["RESEARCH_ONLY", "PLAY_DISABLED", "DRY_RUN"],
            recommendation_status="no_bet",
            components={"family": "atd_sim_v1", "dry_run": True, "injury_coverage": {"status": "unknown"}},
            research_only=True,
            play_enabled=False,
            created_at=freeze_at,
        )
    )
    session.flush()
    return shadow_id
