"""Phase 1 data-integrity repair orchestration for Anytime TD."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from spe.analytics.upcoming_features import generate_upcoming_features
from spe.db.models import FeatureRow, Game
from spe.identity.games import repair_canonical_games
from spe.identity.players import remint_espn_style_outcomes, sync_espn_player_maps
from spe.identity.roster import roster_summary, sync_current_rosters
from spe.integrity.forecast_audit import audit_and_invalidate_forecasts


def run_phase1_repair(
    session: Session,
    *,
    verify_game_ids: list[str] | None = None,
    now: datetime | None = None,
    horizon_hours: int = 24 * 14,
) -> dict[str, Any]:
    """Fix game aliases, roster contamination, player maps, and upcoming features.

    Does not enable Validated Play. Does not rewrite odds ticks or historical
    probability values. Receptions / yards models are left untouched.
    """
    now = now or datetime.now(UTC)
    report: dict[str, Any] = {
        "phase": 1,
        "started_at": now.isoformat(),
        "play_enabled": False,
        "validated_play": False,
        "priority": "anytime_td",
    }

    report["canonical_games"] = repair_canonical_games(session, upcoming_only=True)
    session.flush()

    report["player_maps"] = sync_espn_player_maps(session)
    session.flush()
    report["outcome_player_remap"] = remint_espn_style_outcomes(session)
    session.flush()

    report["rosters"] = sync_current_rosters(session, now=now)
    session.flush()

    report["upcoming_features"] = generate_upcoming_features(
        session,
        horizon_hours=horizon_hours,
        now=now,
    )
    session.flush()

    report["forecast_audit"] = audit_and_invalidate_forecasts(session)
    session.flush()

    verify_ids = verify_game_ids or _default_verify_ids(session, now=now)
    report["verification"] = [_verify_game(session, gid) for gid in verify_ids]
    report["ok"] = all(row.get("ok") for row in report["verification"]) if report["verification"] else False
    report["finished_at"] = datetime.now(UTC).isoformat()
    return report


def _default_verify_ids(session: Session, *, now: datetime) -> list[str]:
    out: list[str] = []
    for league_id in ("nfl", "fbs"):
        game = (
            session.query(Game)
            .filter(
                Game.league_id == league_id,
                Game.data_tier == "upcoming",
                Game.status.in_(("scheduled", "preview", "pregame")),
                Game.kickoff_utc >= now,
            )
            .order_by(Game.kickoff_utc)
            .first()
        )
        if game:
            out.append(game.game_id)
    return out


def _verify_game(session: Session, game_id: str) -> dict[str, Any]:
    game = session.get(Game, game_id)
    if game is None:
        return {"game_id": game_id, "ok": False, "error": "missing"}
    from sqlalchemy import func

    from spe.db.models import InjuryEvent, Outcome, WeatherSnapshot

    features = (
        session.query(func.count())
        .select_from(FeatureRow)
        .filter(FeatureRow.game_id == game_id)
        .scalar()
    )
    outcomes = session.query(func.count()).select_from(Outcome).filter(Outcome.game_id == game_id).scalar()
    injuries = session.query(func.count()).select_from(InjuryEvent).filter(InjuryEvent.game_id == game_id).scalar()
    weather = (
        session.query(func.count()).select_from(WeatherSnapshot).filter(WeatherSnapshot.game_id == game_id).scalar()
    )
    roster = roster_summary(session, game)
    with_history = 0
    if features:
        from spe.db.models import FeatureRow as FR

        for row in session.query(FR).filter(FR.game_id == game_id, FR.player_id.is_not(None)).limit(200):
            if int((row.feature_blob or {}).get("sample_size") or 0) > 0:
                with_history += 1
    return {
        "game_id": game_id,
        "league_id": game.league_id,
        "data_tier": game.data_tier,
        "kickoff_utc": str(game.kickoff_utc),
        "home_team_id": game.home_team_id,
        "away_team_id": game.away_team_id,
        "feature_rows": features,
        "player_features_with_history": with_history,
        "outcomes": outcomes,
        "injuries": injuries,
        "weather": weather,
        "roster": roster,
        "ok": game.data_tier != "alias" and (features or 0) > 0 and (roster.get("eligible_count") or 0) > 0,
    }
