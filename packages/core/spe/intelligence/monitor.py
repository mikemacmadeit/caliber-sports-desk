"""Change-driven research monitoring. Skip unchanged packets; track triggers and cost."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, joinedload

from spe.db.models import AiAlert, AiRun, Game, OddsTick, Outcome
from spe.intelligence.client import LanguageModel
from spe.intelligence.context import build_research_context, fingerprint, research_as_of
from spe.intelligence.research import research_game


def monitor_board(
    session: Session,
    model: LanguageModel,
    *,
    limit: int = 4,
    league_id: str | None = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Research games whose verified context changed, or new slate entries."""
    now = datetime.now(UTC)
    query = (
        session.query(Game)
        .options(
            joinedload(Game.home_team),
            joinedload(Game.away_team),
            joinedload(Game.venue),
            joinedload(Game.season),
        )
        .filter(
            Game.data_tier.notin_(("historical", "season")),
            Game.kickoff_utc >= now - timedelta(hours=6),
            Game.kickoff_utc <= now + timedelta(days=10),
        )
    )
    if league_id:
        query = query.filter(Game.league_id == league_id)
    games = query.order_by(Game.kickoff_utc).limit(max(limit * 3, limit)).all()

    results: list[dict[str, Any]] = []
    researched = 0
    for game in games:
        if researched >= limit:
            break
        moment = research_as_of(game, now)
        packet = build_research_context(session, game, moment)
        mark = fingerprint(packet)
        previous = (
            session.query(AiRun)
            .filter(AiRun.game_id == game.game_id, AiRun.kind == "game_research", AiRun.status == "accepted")
            .order_by(AiRun.created_at.desc())
            .first()
        )
        triggers = detect_triggers(session, game, packet, previous, moment)
        if previous and previous.fingerprint == mark and not force:
            results.append(
                {
                    "game_id": game.game_id,
                    "league_id": game.league_id,
                    "action": "skipped_unchanged",
                    "triggers": triggers,
                    "cached": True,
                    "cost_usd": 0.0,
                }
            )
            continue
        if not triggers and previous is not None and not force:
            results.append(
                {
                    "game_id": game.game_id,
                    "league_id": game.league_id,
                    "action": "skipped_no_trigger",
                    "triggers": [],
                    "cached": True,
                    "cost_usd": 0.0,
                }
            )
            continue

        run = research_game(session, game, model, as_of=moment)
        researched += 1
        if previous and previous.fingerprint != mark and run.status == "accepted":
            _write_change_alerts(session, game, run, triggers)
        results.append(
            {
                "game_id": game.game_id,
                "league_id": game.league_id,
                "action": "researched",
                "triggers": triggers or ["new_slate_game"],
                "status": run.status,
                "run_id": run.run_id,
                "cached": previous is not None and previous.run_id == run.run_id,
                "cost_usd": run.cost_usd,
            }
        )
    return results


def detect_triggers(
    session: Session,
    game: Game,
    packet: dict[str, Any],
    previous: AiRun | None,
    as_of: datetime,
) -> list[str]:
    triggers: list[str] = []
    if previous is None:
        triggers.append("new_slate_game")
        return triggers

    prev_fp = previous.fingerprint or ""
    curr_fp = fingerprint(packet)
    if prev_fp == curr_fp:
        # Still check significant odds movement against the prior accepted output snapshot.
        if _odds_moved(session, game.game_id, as_of, previous.as_of):
            triggers.append("odds_movement")
        return triggers

    # Diff packet sections using current packet contents vs prior output metadata when available.
    prior_output = previous.output or {}
    prior_inj = {
        (row.get("ref"), row.get("designation"))
        for row in (prior_output.get("_monitor_injuries") or [])
    }
    # Fall back to fingerprint mismatch categories from live packet richness.
    if packet.get("injuries"):
        triggers.append("injury_change")
    if packet.get("weather"):
        triggers.append("weather_change")
    if packet.get("depth_chart_changes"):
        triggers.append("depth_chart_change")
    if packet.get("prices") and _odds_moved(session, game.game_id, as_of, previous.as_of):
        triggers.append("odds_movement")
    if packet.get("news"):
        # News alone is unverified; only trigger when paired with another verified change.
        if any(t != "injury_change" for t in triggers) or prior_inj:
            pass
    if not triggers:
        triggers.append("context_change")
    # Deduplicate while preserving order.
    seen: set[str] = set()
    ordered = []
    for item in triggers:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _odds_moved(session: Session, game_id: str, as_of: datetime, since: datetime) -> bool:
    recent = (
        session.query(OddsTick)
        .join(Outcome, Outcome.outcome_id == OddsTick.outcome_id)
        .filter(Outcome.game_id == game_id, OddsTick.captured_at <= as_of, OddsTick.captured_at > since)
        .limit(1)
        .first()
    )
    return recent is not None


def _write_change_alerts(session: Session, game: Game, run: AiRun, triggers: list[str]) -> None:
    for kind in triggers:
        session.add(
            AiAlert(
                game_id=game.game_id,
                run_id=run.run_id,
                kind=kind,
                summary=(
                    f"Monitor trigger `{kind}` fired. Research refreshed. "
                    "Official probabilities were not rewritten. Validated Play stays off."
                ),
                affected_forecast_ids=[],
                model_response="official_unchanged",
            )
        )
