"""Prediction review and post-game miss analysis. Never edits production models."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from spe.db.models import AiReview, AiRun, BacktestPrediction, Game
from spe.intelligence import PROMPT_VERSION
from spe.intelligence.client import LanguageModel
from spe.intelligence.context import build_explanation_context, build_research_context, packet_hash, research_as_of
from spe.intelligence.schema import ReviewOutput
from spe.intelligence.validate import repair_research_evidence

EXPERIMENTS = {
    "workload": "Rerun the backtest holding snap share and target share fixed at the pregame value.",
    "game_script": "Split the next backtest by pregame pace and expected score margin.",
    "injuries": "Repeat the window using only injury rows timestamped before kickoff.",
    "matchup": "Score a receiver-versus-coverage split after charting is licensed. Do not invent routes.",
    "calibration": "Fit the calibration map on an earlier window and score the following window.",
    "random_variation": "No parameter change. The result sits inside the stated probability interval.",
    "unknown": "Collect the missing pregame feature before any model change.",
    "assumption": "Hold out the disputed assumption and resimulate without changing production weights.",
    "opportunity_estimate": "Compare pregame opportunity features to realized targets/carries after settlement.",
    "injury_uncertainty": "Repeat the window using only injury rows timestamped before kickoff.",
    "simulation_consistency": "Re-run the same seed with draw_count doubled and compare interval coverage.",
    "missing_information": "Collect the missing pregame feature before any model change.",
}

REVIEW_INSTRUCTIONS = """
You are Caliber's prediction review desk.
Use only the supplied research packet and Python forecasts.
Flag suspicious forecasts for further testing. Do not change probabilities.
Each forecast has a "pick" field. Name that pick (player, side, line, market)
before you discuss its probability. Do not review a prop you cannot name.
Do not issue Play or No bet. Do not invent an edge.
Every flag needs a forecast_id from the packet and a recommended_test.
Facts require evidence refs from packet.evidence.
Distinguish fact, inference, and hypothetical.
Keep NFL and FBS separate.
""".strip()


def classify_miss(
    p: float,
    y: float,
    p_low: float | None = None,
    p_high: float | None = None,
    availability: float | None = None,
) -> str:
    if availability is not None and availability < 0.75:
        return "injuries"
    if p_low is not None and p_high is not None and p_low <= y <= p_high:
        return "random_variation"
    if abs(p - y) <= 0.08:
        return "random_variation"
    if p >= 0.55 and y == 0:
        return "workload"
    if p <= 0.25 and y == 1:
        return "matchup"
    return "calibration"


def review_backtests(session: Session, *, limit: int = 20) -> int:
    rows = session.query(BacktestPrediction).order_by(BacktestPrediction.as_of.desc()).limit(limit).all()
    written = 0
    for row in rows:
        origin = classify_miss(row.p, row.y)
        exists = (
            session.query(AiReview)
            .filter(AiReview.forecast_id == row.id, AiReview.origin == origin)
            .first()
        )
        if exists:
            continue
        session.add(
            AiReview(
                game_id=row.game_id,
                forecast_id=row.id,
                origin=origin,
                suggested_experiment=EXPERIMENTS[origin],
                applied=False,
                evidence=[{"source": "backtest_predictions", "ref": row.id, "p": row.p, "y": row.y}],
            )
        )
        written += 1
    return written


def review_game_forecasts(
    session: Session,
    game: Game,
    model: LanguageModel,
    *,
    research: dict[str, Any] | None = None,
) -> AiRun | None:
    """Phase 4: AI reviews Python forecasts. Cannot override probabilities or Play rules."""
    focus: set[str] = set()
    if research:
        for key in ("signals", "opportunities"):
            for row in research.get(key) or []:
                if row.get("player_id"):
                    focus.add(row["player_id"])
        for row in research.get("findings") or []:
            focus.update(row.get("affected_player_ids") or [])
    forecasts = build_explanation_context(session, game, limit=16, focus_player_ids=focus)
    if not forecasts:
        return None
    moment = research_as_of(game, None)
    packet = build_research_context(session, game, moment)
    payload = {
        "game": packet["game"],
        "league_rules": packet["league_rules"],
        "gaps": packet["gaps"],
        "evidence": packet["evidence"],
        "forecasts": forecasts,
        "research": research or {},
        "play_enabled": False,
        "note": "AI may flag tests only. Python owns p and recommendation_status. Review only the listed forecasts. Each forecast.pick is the prop under review.",
    }
    digest = packet_hash("prediction_review", PROMPT_VERSION, _model_name(model), payload)
    cached = (
        session.query(AiRun)
        .filter(AiRun.kind == "prediction_review", AiRun.input_hash == digest, AiRun.status == "accepted")
        .one_or_none()
    )
    if cached:
        return cached

    parsed, usage = model.complete(instructions=REVIEW_INSTRUCTIONS, user=_dump(payload), schema=ReviewOutput)
    evidence = {key: set(value) for key, value in packet["evidence"].items()}
    from spe.intelligence.schema import Finding, ResearchOutput

    fixed_flags = []
    for flag in parsed.flags:
        tmp = ResearchOutput(
            findings=[
                Finding(
                    topic="matchup",
                    claim_type=flag.claim_type,
                    statement=flag.statement,
                    confidence=flag.confidence,
                    evidence=flag.evidence,
                )
            ]
        )
        tmp = repair_research_evidence(tmp, evidence)
        fixed_flags.append(flag.model_copy(update={"evidence": tmp.findings[0].evidence}))
    parsed = parsed.model_copy(update={"flags": fixed_flags})
    errors = _validate_review(parsed, forecasts, evidence)
    run = AiRun(
        kind="prediction_review",
        game_id=game.game_id,
        status="rejected" if errors else "accepted",
        as_of=moment,
        input_hash=digest,
        fingerprint="",
        model=_model_name(model),
        prompt_version=PROMPT_VERSION,
        token_input=usage.input_tokens,
        token_output=usage.output_tokens,
        cost_usd=usage.cost_usd,
        rejection_reasons=errors,
        output=parsed.model_dump(),
        data_tier=game.data_tier,
    )
    session.add(run)
    session.flush()
    if errors:
        return run
    for flag in parsed.flags:
        session.add(
            AiReview(
                game_id=game.game_id,
                forecast_id=flag.forecast_id,
                origin=flag.concern,
                suggested_experiment=flag.recommended_test or EXPERIMENTS.get(flag.concern, EXPERIMENTS["unknown"]),
                applied=False,
                evidence=[
                    {
                        "source": "prediction_review",
                        "statement": flag.statement,
                        "claim_type": flag.claim_type,
                        "confidence": flag.confidence,
                        "refs": [row.model_dump() for row in flag.evidence],
                    }
                ],
            )
        )
    return run


def review_board(session: Session, model: LanguageModel, *, limit: int = 1, league_id: str | None = None) -> list[dict]:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy.orm import joinedload

    now = datetime.now(UTC)
    query = (
        session.query(Game)
        .options(joinedload(Game.home_team), joinedload(Game.away_team), joinedload(Game.venue), joinedload(Game.season))
        .filter(Game.data_tier.notin_(("historical", "season")), Game.kickoff_utc >= now - timedelta(hours=6))
    )
    if league_id:
        query = query.filter(Game.league_id == league_id)
    games = query.order_by(Game.kickoff_utc).limit(limit).all()
    rows = []
    for game in games:
        research = (
            session.query(AiRun)
            .filter(AiRun.game_id == game.game_id, AiRun.kind == "game_research", AiRun.status == "accepted")
            .order_by(AiRun.created_at.desc())
            .first()
        )
        run = review_game_forecasts(session, game, model, research=(research.output if research else None))
        if run is None:
            rows.append({"game_id": game.game_id, "status": "no_forecasts"})
            continue
        rows.append(
            {
                "game_id": game.game_id,
                "league_id": game.league_id,
                "status": run.status,
                "run_id": run.run_id,
                "flags": len((run.output or {}).get("flags") or []),
                "cost_usd": run.cost_usd,
                "play_enabled": False,
            }
        )
    return rows


def _validate_review(output: ReviewOutput, forecasts: list[dict], evidence: dict[str, set[str]]) -> list[str]:
    errors: list[str] = []
    known = {row["forecast_id"] for row in forecasts}
    for i, flag in enumerate(output.flags):
        if flag.forecast_id not in known:
            errors.append(f"flag {i} names unknown forecast {flag.forecast_id}")
        if not flag.recommended_test.strip():
            errors.append(f"flag {i} missing recommended_test")
        if flag.claim_type == "fact" and not flag.evidence:
            errors.append(f"flag {i} is a fact with no source")
        for ev in flag.evidence:
            refs = evidence.get(ev.source)
            if refs is None or ev.ref not in refs:
                errors.append(f"flag {i} cites missing {ev.source}:{ev.ref}")
        lowered = (flag.statement + " " + flag.recommended_test).lower()
        if any(token in lowered for token in ("override p", "change the probability", "make it a play", "edge of")):
            errors.append(f"flag {i} attempts to override probabilities or invent an edge")
    return errors


def _model_name(model: LanguageModel) -> str:
    from spe.config import settings

    return getattr(model, "model", None) or settings.openai_model


def _dump(payload: Any) -> str:
    import json

    return json.dumps(payload, default=str)
