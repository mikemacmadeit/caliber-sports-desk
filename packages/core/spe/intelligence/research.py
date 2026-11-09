"""Game research, opportunity signals, and research-only scenario triggers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, joinedload

from spe.db.models import (
    AiAlert,
    AiExplanation,
    AiFinding,
    AiOpportunity,
    AiRun,
    AiSignal,
    Forecast,
    Game,
    Outcome,
)
from spe.intelligence import PROMPT_VERSION
from spe.intelligence.client import LanguageModel, Usage
from spe.intelligence.context import (
    build_explanation_context,
    build_research_context,
    fingerprint,
    packet_hash,
    research_as_of,
)
from spe.intelligence.scenarios import plan_from_signals, run_research_simulation
from spe.intelligence.schema import ExplanationOutput, ResearchOutput
from spe.intelligence.validate import (
    availability_override,
    misstates_direction,
    repair_research_evidence,
    stamp_evidence,
    validate_explanation,
    validate_research,
)

RESEARCH_INSTRUCTIONS = """
You are Caliber's NFL/FBS research desk — not a chatbot and not a probability engine.

Use only the JSON packet. Facts require evidence objects shaped exactly as {"source": "<bucket>", "ref": "<id>"}
where source is a key in packet.evidence (for example injury_events or team_analytics)
and ref is one of that key's ids. Never put the id in source.
Prefer packet.evidence_catalog entries when citing.
If a topic is listed in gaps, do not state it as a fact.
News titles are unverified. Do not treat them as injury designations.
Do not invent players, prices, weather, coaching, injuries, or statistics.
Do not confuse historical seasons with the packet season_year.
Label each claim fact, inference, or hypothetical.
Do not produce a probability, edge, EV, Play, or No bet.

Investigate every investigation_topics item that the packet supports:
injuries/availability, offensive/defensive matchups, workload/usage changes,
coaching tendencies (only if sourced), scoring expectations, recent trends,
home-field (schedule/venue facts only), weather, depth-chart/role changes.

For findings, note which prop markets could be materially affected:
anytime_td, receptions, receiving_yards, rushing_yards.
When a finding or opportunity is about a prop, name the pick in the exact form
"Player over/under LINE market" using the packet price or forecast field "pick".
Copy that pick. Do not say "his receiving prop" or "the rushing number" without
the player, the side, and the line. A price without "pick" is not a pick.

For opportunities, identify only evidence-backed situations such as:
increased opportunity, backups benefiting from injuries, favorable matchups,
strategy/usage shifts, or potentially mispriced props that deserve Python analysis.
Each opportunity must include explanation, evidence_confidence, missing_information,
and recommended_analysis. Never invent a numeric edge; Python owns prices and forecasts.

For injury facts, set subject_key to injury:<player_id> and stance to one of:
out, healthy, questionable, doubtful, limited, or unknown.
Do not reuse one subject_key for unrelated workload notes.

Keep NFL and FBS separate. Do not transfer conclusions across leagues.
""".strip()

EXPLAIN_INSTRUCTIONS = """
Explain the Python forecast using only the supplied numbers and research.
Each forecast has a "pick" field. Start from that sentence — player, side, line,
and market — and treat p as the chance that exact side hits. Do not discuss a
probability that is not tied to its pick.
Copy recommendation_status and reason_codes exactly.
Do not change the call. Do not invent a new probability.
Separate assumptions and uncertainty from facts.
Cover: why p is high or low, main factors, injury effects on opportunity,
matchup influence, what could change the prediction, and why the call is
Play or No bet using only the echoed recommendation_status/reason_codes.
Every factual claim needs a verified packet ref. Validated Play stays off unless
the echoed status already says play, and even then do not upgrade demo_play.
""".strip()


def research_game(session: Session, game: Game, model: LanguageModel, *, as_of: datetime | None = None) -> AiRun:
    moment = research_as_of(game, as_of)
    packet = build_research_context(session, game, moment)
    digest = packet_hash("game_research", PROMPT_VERSION, _model_name(model), packet)
    cached = _cached(session, "game_research", digest)
    if cached:
        return cached
    mark = fingerprint(packet)
    unchanged = _latest_accepted(session, game.game_id, "game_research")
    if unchanged and unchanged.fingerprint == mark and unchanged.prompt_version == PROMPT_VERSION:
        return unchanged
    _clear_rejected(session, "game_research", digest)
    parsed, usage = model.complete(
        instructions=RESEARCH_INSTRUCTIONS,
        user=_dump(packet),
        schema=ResearchOutput,
    )
    allowed = {row["player_id"] for row in packet["players"]}
    evidence = {key: set(value) for key, value in packet["evidence"].items()}
    parsed = repair_research_evidence(parsed, evidence)
    player_teams = {row["player_id"]: row.get("team_id") for row in packet["players"]}
    game_teams = {packet["game"]["home_team_id"], packet["game"]["away_team_id"]}
    errors = validate_research(
        parsed,
        allowed,
        evidence,
        player_teams=player_teams,
        game_teams=game_teams,
        packet_gaps=packet.get("gaps"),
    )
    run = _open_run(game, "game_research", moment, digest, mark, usage, parsed, errors)
    session.add(run)
    session.flush()
    if errors:
        return run
    _store_findings(session, run, parsed)
    overrides = _store_signals(session, run, game, parsed, packet)
    _store_opportunities(session, run, game, parsed, packet)
    plan = plan_from_signals(packet, parsed.signals)
    sim_id = run_research_simulation(
        session,
        game,
        plan,
        packet,
        as_of=moment,
        include_hypothetical=True,
    )
    if sim_id is None and overrides:
        # Fallback: validated OUT designations alone.
        from spe.intelligence.scenarios import ScenarioChange, ScenarioPlan

        fallback = ScenarioPlan(
            game_id=game.game_id,
            changes=[
                ScenarioChange(kind="availability", player_id=pid, value=val, claim_type="fact", evidence=[])
                for pid, val in overrides.items()
            ],
            research_only=True,
            validated_changes=[
                ScenarioChange(kind="availability", player_id=pid, value=val, claim_type="fact", evidence=[])
                for pid, val in overrides.items()
            ],
            hypothetical_changes=[],
        )
        sim_id = run_research_simulation(session, game, fallback, packet, as_of=moment, include_hypothetical=False)
    if sim_id:
        for signal in session.query(AiSignal).filter(AiSignal.run_id == run.run_id, AiSignal.validation_status == "validated"):
            signal.simulation_run_id = sim_id
    _alerts(session, game, run, packet, bool(sim_id))
    _explain(session, game, model, parsed)
    from spe.intelligence.review import review_game_forecasts

    review_game_forecasts(
        session,
        game,
        model,
        research={
            "findings": [row.model_dump() for row in parsed.findings],
            "opportunities": [row.model_dump() for row in parsed.opportunities],
            "signals": [row.model_dump() for row in parsed.signals],
        },
    )
    return run


def research_board(
    session: Session,
    model: LanguageModel,
    *,
    limit: int = 1,
    league_id: str | None = None,
) -> list[dict[str, Any]]:
    """Research upcoming games. NFL and FBS stay separate when league_id is set."""
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
        )
    )
    if league_id:
        query = query.filter(Game.league_id == league_id)
    games = query.order_by(Game.kickoff_utc).limit(limit).all()
    results = []
    for game in games:
        before = _latest_accepted(session, game.game_id, "game_research")
        run = research_game(session, game, model)
        cached = before is not None and before.run_id == run.run_id
        results.append(
            {
                "game_id": game.game_id,
                "league_id": game.league_id,
                "status": run.status,
                "run_id": run.run_id,
                "cached": cached,
                "cost_usd": run.cost_usd,
            }
        )
    return results


def research_slate(session: Session, model: LanguageModel, *, per_league: int = 1) -> list[dict[str, Any]]:
    """Run Phase 1/2 research for NFL and FBS independently."""
    rows: list[dict[str, Any]] = []
    for league_id in ("nfl", "fbs"):
        rows.extend(research_board(session, model, limit=per_league, league_id=league_id))
    return rows


def _explain(session: Session, game: Game, model: LanguageModel, research: ResearchOutput) -> None:
    focus = {row.player_id for row in research.signals} | {row.player_id for row in research.opportunities}
    for finding in research.findings:
        focus.update(finding.affected_player_ids)
    expected = build_explanation_context(session, game, limit=12, focus_player_ids=focus)
    if not expected:
        return
    payload = {
        "forecasts": expected,
        "findings": [row.model_dump() for row in research.findings],
        "opportunities": [row.model_dump() for row in research.opportunities],
        "play_enabled": False,
        "note": "Echo every forecast_id in forecasts with the exact recommendation_status and reason_codes. Do not invent Play.",
    }
    digest = packet_hash("explanation", PROMPT_VERSION, _model_name(model), payload)
    if _cached(session, "explanation", digest):
        return
    parsed, usage = model.complete(instructions=EXPLAIN_INSTRUCTIONS, user=_dump(payload), schema=ExplanationOutput)
    errors = validate_explanation(parsed, expected)
    run = _open_run(game, "explanation", datetime.now(UTC), digest, "", usage, parsed, errors)
    session.add(run)
    session.flush()
    if errors:
        return
    for row in expected:
        session.add(
            AiExplanation(
                run_id=run.run_id,
                game_id=game.game_id,
                forecast_id=row["forecast_id"],
                recommendation_status=row["recommendation_status"],
                reason_codes=row["reason_codes"],
                narrative=parsed.narrative,
                factors=parsed.factors,
                assumptions=parsed.assumptions,
                uncertainty=list(parsed.uncertainty) + list(parsed.what_could_change),
            )
        )


def _store_findings(session: Session, run: AiRun, parsed: ResearchOutput) -> None:
    for finding in parsed.findings:
        if misstates_direction(finding.statement):
            run.rejection_reasons = list(run.rejection_reasons or []) + [
                "dropped finding that contradicts its own numbers"
            ]
            continue
        session.add(
            AiFinding(
                run_id=run.run_id,
                game_id=run.game_id,
                topic=finding.topic,
                claim_type=finding.claim_type,
                statement=finding.statement,
                confidence=finding.confidence,
                subject_key=finding.subject_key,
                stance=finding.stance,
                evidence=[row.model_dump() for row in finding.evidence],
                affected_player_ids=finding.affected_player_ids,
            )
        )


def _store_signals(session, run: AiRun, game: Game, parsed: ResearchOutput, packet: dict) -> dict[str, float]:
    injuries = {row["ref"]: row for row in packet["injuries"]}
    overrides: dict[str, float] = {}
    for signal in parsed.signals:
        status = "unverified"
        scenario = "speculative"
        numeric = None
        evidence = stamp_evidence(signal.evidence, packet)
        if signal.claim_type == "fact":
            for ev in signal.evidence:
                row = injuries.get(ev.ref) if ev.source == "injury_events" else None
                if not row or row["player_id"] != signal.player_id:
                    continue
                avail = availability_override(row["designation"])
                if avail is not None:
                    status = "validated"
                    scenario = "validated_research"
                    numeric = {"availability": avail}
                    overrides[signal.player_id] = avail
        session.add(
            AiSignal(
                run_id=run.run_id,
                game_id=game.game_id,
                player_id=signal.player_id,
                metric=signal.metric,
                direction=signal.direction,
                magnitude=signal.magnitude,
                claim_type=signal.claim_type,
                statement=signal.statement,
                confidence=signal.confidence,
                validation_status=status,
                scenario_status=scenario,
                numeric_override=numeric,
                evidence=evidence,
            )
        )
    return overrides


def _store_opportunities(session, run: AiRun, game: Game, parsed: ResearchOutput, packet: dict) -> None:
    for opportunity in parsed.opportunities:
        status = "unverified"
        if opportunity.claim_type == "fact" and opportunity.evidence:
            status = "evidence_backed"
        session.add(
            AiOpportunity(
                run_id=run.run_id,
                game_id=game.game_id,
                player_id=opportunity.player_id,
                market_id=opportunity.market_id,
                claim_type=opportunity.claim_type,
                explanation=opportunity.explanation,
                evidence_confidence=opportunity.evidence_confidence,
                evidence=stamp_evidence(opportunity.evidence, packet),
                missing_information=list(opportunity.missing_information),
                recommended_analysis=opportunity.recommended_analysis,
                validation_status=status,
            )
        )


def _alerts(session: Session, game: Game, run: AiRun, packet: dict, simulated: bool) -> None:
    previous = _latest_accepted(session, game.game_id, "game_research", exclude_run_id=run.run_id)
    if previous is None or previous.fingerprint == run.fingerprint:
        return
    forecasts = [
        row[0]
        for row in session.query(Forecast.forecast_id)
        .join(Outcome, Outcome.outcome_id == Forecast.outcome_id)
        .filter(Outcome.game_id == game.game_id, Forecast.is_official.is_(True))
        .all()
    ]
    response = "research_simulation_only" if simulated else "official_unchanged"
    kinds = []
    if packet["injuries"]:
        kinds.append("injury")
    if packet["weather"]:
        kinds.append("weather")
    if packet["prices"]:
        kinds.append("price")
    if packet.get("depth_chart_changes"):
        kinds.append("depth_chart")
    if not kinds:
        kinds.append("context")
    for kind in kinds:
        session.add(
            AiAlert(
                game_id=game.game_id,
                run_id=run.run_id,
                kind=kind,
                summary=(
                    f"{kind} context changed. Official probabilities were not rewritten. "
                    f"Model response: {response}."
                ),
                affected_forecast_ids=forecasts,
                model_response=response,
            )
        )


def _open_run(game, kind, moment, digest, mark, usage: Usage, parsed, errors: list[str]) -> AiRun:
    return AiRun(
        kind=kind,
        game_id=game.game_id,
        status="rejected" if errors else "accepted",
        as_of=moment,
        input_hash=digest,
        fingerprint=mark,
        model=_model_name_from_settings(),
        prompt_version=PROMPT_VERSION,
        token_input=usage.input_tokens,
        token_output=usage.output_tokens,
        cost_usd=usage.cost_usd,
        rejection_reasons=errors,
        output=parsed.model_dump(),
        data_tier=game.data_tier,
    )


def _cached(session: Session, kind: str, digest: str) -> AiRun | None:
    """Reuse accepted runs only. Rejected packets may be retried after validator/prompt fixes."""
    return (
        session.query(AiRun)
        .filter(AiRun.kind == kind, AiRun.input_hash == digest, AiRun.status == "accepted")
        .one_or_none()
    )


def _clear_rejected(session: Session, kind: str, digest: str) -> None:
    session.query(AiRun).filter(AiRun.kind == kind, AiRun.input_hash == digest, AiRun.status == "rejected").delete()


def _latest_accepted(
    session: Session,
    game_id: str,
    kind: str,
    *,
    exclude_run_id: str | None = None,
) -> AiRun | None:
    query = (
        session.query(AiRun)
        .filter(AiRun.game_id == game_id, AiRun.kind == kind, AiRun.status == "accepted")
        .order_by(AiRun.created_at.desc())
    )
    if exclude_run_id:
        query = query.filter(AiRun.run_id != exclude_run_id)
    return query.first()


def _model_name(model: LanguageModel) -> str:
    return getattr(model, "model", None) or _model_name_from_settings()


def _model_name_from_settings() -> str:
    from spe.config import settings

    return settings.openai_model


def _dump(payload: Any) -> str:
    import json

    return json.dumps(payload, default=str)
