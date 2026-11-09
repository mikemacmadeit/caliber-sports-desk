from __future__ import annotations

from typing import Any

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from spe.db.models import AiAlert, AiExplanation, AiFinding, AiOpportunity, AiReview, AiRun, AiSignal

MARKET_METRIC = {
    "anytime_td": "touchdowns",
    "receptions": "targets",
    "receiving_yards": "yards",
    "rushing_yards": "yards",
    "opportunity": "targets",
}


def tables_ready(session: Session) -> bool:
    cached = session.info.get("ai_tables")
    if cached is not None:
        return bool(cached)
    if session.get_bind().dialect.name != "sqlite":
        session.info["ai_tables"] = True
        return True
    found = session.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='ai_runs'")).scalar()
    session.info["ai_tables"] = bool(found)
    return bool(found)


def research_summary(session: Session, game_id: str) -> dict[str, Any] | None:
    if not tables_ready(session):
        return None
    run = _latest(session, game_id, "game_research")
    if run is None:
        return None
    finding = (
        session.query(AiFinding)
        .filter(AiFinding.run_id == run.run_id, AiFinding.claim_type == "fact")
        .order_by(AiFinding.confidence.desc())
        .first()
    )
    headline = finding.statement if finding else (run.output or {}).get("abstain_reason")
    signals = session.query(func.count(AiSignal.signal_id)).filter(AiSignal.run_id == run.run_id).scalar() or 0
    findings = session.query(func.count(AiFinding.finding_id)).filter(AiFinding.run_id == run.run_id).scalar() or 0
    opportunities = (
        session.query(func.count(AiOpportunity.opportunity_id)).filter(AiOpportunity.run_id == run.run_id).scalar() or 0
    )
    return {
        "status": run.status,
        "as_of": run.as_of.isoformat() if hasattr(run.as_of, "isoformat") else run.as_of,
        "data_tier": run.data_tier,
        "headline": (headline or "")[:160] or None,
        "finding_count": findings,
        "signal_count": signals,
        "opportunity_count": opportunities,
        "official_unchanged": True,
        "play_enabled": False,
    }


def research_detail(session: Session, game_id: str) -> dict[str, Any] | None:
    if not tables_ready(session):
        return None
    summary = research_summary(session, game_id)
    if summary is None:
        return None
    run = _latest(session, game_id, "game_research")
    findings = session.query(AiFinding).filter(AiFinding.run_id == run.run_id).all()
    signals = session.query(AiSignal).filter(AiSignal.run_id == run.run_id).all()
    opportunities = session.query(AiOpportunity).filter(AiOpportunity.run_id == run.run_id).all()
    alerts = session.query(AiAlert).filter(AiAlert.game_id == game_id).order_by(AiAlert.created_at.desc()).limit(8).all()
    explanations = (
        session.query(AiExplanation).filter(AiExplanation.game_id == game_id).order_by(AiExplanation.created_at.desc()).limit(6).all()
    )
    reviews = (
        session.query(AiReview)
        .filter(AiReview.game_id == game_id, AiReview.applied.is_(False))
        .order_by(AiReview.created_at.desc())
        .limit(12)
        .all()
    )
    return {
        **summary,
        "model": run.model,
        "prompt_version": run.prompt_version,
        "findings": [_finding(row) for row in findings],
        "signals": [_signal(row) for row in signals],
        "opportunities": [_opportunity(row) for row in opportunities],
        "alerts": [
            {
                "kind": row.kind,
                "summary": row.summary,
                "model_response": row.model_response,
                "affected_forecast_ids": row.affected_forecast_ids,
            }
            for row in alerts
        ],
        "explanations": [
            {
                "forecast_id": row.forecast_id,
                "recommendation_status": row.recommendation_status,
                "reason_codes": row.reason_codes,
                "narrative": row.narrative,
                "factors": row.factors,
                "assumptions": row.assumptions,
                "uncertainty": row.uncertainty,
            }
            for row in explanations
        ],
        "prediction_reviews": [
            {
                "forecast_id": row.forecast_id,
                "origin": row.origin,
                "suggested_experiment": row.suggested_experiment,
                "applied": row.applied,
            }
            for row in reviews
        ],
    }


def signal_for(session: Session, game_id: str, player_id: str | None, market_id: str) -> dict[str, Any] | None:
    if not tables_ready(session):
        return None
    if not player_id:
        return None
    opportunity = (
        session.query(AiOpportunity)
        .filter(AiOpportunity.game_id == game_id, AiOpportunity.player_id == player_id, AiOpportunity.market_id == market_id)
        .order_by(AiOpportunity.created_at.desc())
        .first()
    )
    if opportunity is not None:
        return _opportunity(opportunity)
    metric = MARKET_METRIC.get(market_id)
    if not metric:
        return None
    row = (
        session.query(AiSignal)
        .filter(AiSignal.game_id == game_id, AiSignal.player_id == player_id, AiSignal.metric == metric)
        .order_by(AiSignal.created_at.desc())
        .first()
    )
    return None if row is None else _signal(row)


def usage_summary(session: Session) -> dict[str, Any]:
    if not tables_ready(session):
        return {
            "configured_note": "OPENAI_API_KEY stays on the server.",
            "runs": 0,
            "accepted": 0,
            "rejected": 0,
            "token_input": 0,
            "token_output": 0,
            "cost_usd": 0,
            "models": [],
            "prompt_version": [],
            "official_probabilities": "python",
            "play_enabled": False,
        }
    runs = session.query(AiRun).all()
    return {
        "configured_note": "OPENAI_API_KEY stays on the server.",
        "runs": len(runs),
        "accepted": sum(1 for row in runs if row.status == "accepted"),
        "rejected": sum(1 for row in runs if row.status == "rejected"),
        "token_input": sum(row.token_input for row in runs),
        "token_output": sum(row.token_output for row in runs),
        "cost_usd": round(sum(row.cost_usd for row in runs), 6),
        "models": sorted({row.model for row in runs}),
        "prompt_version": sorted({row.prompt_version for row in runs}),
        "official_probabilities": "python",
        "play_enabled": False,
        "by_league": _cost_by_league(session),
    }


def review_rows(session: Session, limit: int = 12) -> list[dict[str, Any]]:
    if not tables_ready(session):
        return []
    rows = session.query(AiReview).order_by(AiReview.created_at.desc()).limit(limit).all()
    return [
        {
            "origin": row.origin,
            "suggested_experiment": row.suggested_experiment,
            "applied": row.applied,
            "game_id": row.game_id,
            "forecast_id": row.forecast_id,
            "retrain": False,
            "play_enabled": False,
        }
        for row in rows
    ]


def improvement_summary(session: Session) -> dict[str, Any]:
    if not tables_ready(session):
        return {"reviews": 0, "applied": 0, "origins": {}, "play_enabled": False, "retrain": False}
    rows = session.query(AiReview).all()
    origins: dict[str, int] = {}
    for row in rows:
        origins[row.origin] = origins.get(row.origin, 0) + 1
    return {
        "reviews": len(rows),
        "applied": sum(1 for row in rows if row.applied),
        "origins": origins,
        "play_enabled": False,
        "retrain": False,
        "note": "AI hypotheses never auto-retrain production models.",
    }


def _cost_by_league(session: Session) -> dict[str, Any]:
    from spe.db.models import Game

    rows = (
        session.query(Game.league_id, func.sum(AiRun.cost_usd), func.count(AiRun.run_id))
        .join(AiRun, AiRun.game_id == Game.game_id)
        .group_by(Game.league_id)
        .all()
    )
    return {
        league_id: {"cost_usd": round(float(cost or 0), 6), "runs": int(count or 0)}
        for league_id, cost, count in rows
    }


def _latest(session: Session, game_id: str, kind: str) -> AiRun | None:
    return (
        session.query(AiRun)
        .filter(AiRun.game_id == game_id, AiRun.kind == kind, AiRun.status == "accepted")
        .order_by(AiRun.created_at.desc())
        .first()
    )


def _finding(row: AiFinding) -> dict[str, Any]:
    return {
        "topic": row.topic,
        "claim_type": row.claim_type,
        "statement": row.statement,
        "confidence": row.confidence,
        "affected_player_ids": row.affected_player_ids,
        "evidence": row.evidence,
    }


def _signal(row: AiSignal) -> dict[str, Any]:
    return {
        "player_id": row.player_id,
        "metric": row.metric,
        "direction": row.direction,
        "magnitude": row.magnitude,
        "claim_type": row.claim_type,
        "statement": row.statement,
        "confidence": row.confidence,
        "validation_status": row.validation_status,
        "scenario_status": row.scenario_status,
        "research_only": row.scenario_status != "official",
        "evidence": row.evidence,
    }


def _opportunity(row: AiOpportunity) -> dict[str, Any]:
    return {
        "player_id": row.player_id,
        "game_id": row.game_id,
        "market_id": row.market_id,
        "claim_type": row.claim_type,
        "explanation": row.explanation,
        "statement": row.explanation,
        "evidence_confidence": row.evidence_confidence,
        "confidence": row.evidence_confidence,
        "evidence": row.evidence,
        "missing_information": row.missing_information,
        "recommended_analysis": row.recommended_analysis,
        "validation_status": row.validation_status,
        "scenario_status": "research_only",
        "research_only": True,
        "numeric_edge": None,
        "play_enabled": False,
    }
