"""Phase 7 continuous improvement. AI may propose tests; it never retrains production models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from spe.db.models import (
    AiReview,
    AiRun,
    BacktestPrediction,
    Game,
    InjuryEvent,
    PlayerGameStat,
    ShadowPrediction,
    ShadowSettlement,
)
from spe.intelligence import PROMPT_VERSION
from spe.intelligence.client import LanguageModel
from spe.intelligence.context import packet_hash
from spe.intelligence.review import EXPERIMENTS

MissOrigin = Literal[
    "playing_time",
    "opportunity",
    "defensive_performance",
    "game_script",
    "injuries",
    "coaching_tendencies",
    "workload",
    "matchup",
    "calibration",
    "random_variation",
    "unknown",
]

IMPROVE_EXPERIMENTS: dict[str, str] = {
    **EXPERIMENTS,
    "playing_time": "Hold snap share at the realized pregame depth-chart value and rescore the window.",
    "opportunity": "Compare pregame target/carry expectations to realized opportunity after settlement.",
    "defensive_performance": "Split the next backtest by opponent adjusted defensive EPA available before kickoff.",
    "game_script": "Split the next backtest by pregame pace and expected score margin.",
    "coaching_tendencies": "Do not change production weights. Collect a verified coaching/usage feature before any experiment.",
}


class ImprovementHypothesis(BaseModel):
    forecast_id: str
    origin: MissOrigin
    claim_type: Literal["inference", "hypothetical"] = "inference"
    statement: str
    confidence: float = Field(ge=0, le=1)
    recommended_experiment: str
    missing_information: list[str] = Field(default_factory=list)


class ImprovementOutput(BaseModel):
    hypotheses: list[ImprovementHypothesis] = Field(default_factory=list)
    abstain_reason: str | None = None


IMPROVE_INSTRUCTIONS = """
You are Caliber's post-game improvement desk.
Use only the settlement packet. Do not invent box-score numbers.
Propose testable hypotheses for model research.
Never retrain, never change production weights, never set applied=true.
Never invent a probability, edge, Play, or No bet.
Origins must be one of: playing_time, opportunity, defensive_performance,
game_script, injuries, coaching_tendencies, workload, matchup, calibration,
random_variation, unknown.
Keep NFL and FBS separate. Distinguish inference from hypothetical.
If a miss includes a market and a line, name the prop as player, side, line,
and market before explaining the miss. Do not discuss "the prop" without that pick.
""".strip()


def classify_settled_miss(
    *,
    p: float,
    y: float,
    actual_value: float | None = None,
    line: float | None = None,
    market_id: str | None = None,
    stats: dict[str, float] | None = None,
    availability: float | None = None,
    inactive: bool = False,
) -> MissOrigin:
    if inactive or (availability is not None and availability < 0.75):
        return "injuries"
    if abs(p - y) <= 0.08:
        return "random_variation"

    stats = stats or {}
    snaps_proxy = float(stats.get("carries", 0) or 0) + float(stats.get("targets", 0) or 0)
    if p >= 0.45 and y == 0 and snaps_proxy <= 1:
        return "playing_time"
    if market_id == "anytime_td" and p >= 0.45 and y == 0:
        if snaps_proxy >= 8:
            return "opportunity"
        if float(stats.get("rush_yards", 0) or 0) + float(stats.get("rec_yards", 0) or 0) >= 60:
            return "defensive_performance"
        return "game_script"
    if market_id in {"receptions", "receiving_yards", "opportunity"} and p >= 0.55 and y == 0:
        if float(stats.get("targets", 0) or 0) <= 2:
            return "playing_time"
        return "opportunity"
    if market_id == "rushing_yards" and p >= 0.55 and y == 0:
        if float(stats.get("carries", 0) or 0) <= 3:
            return "playing_time"
        return "game_script"
    if p <= 0.25 and y == 1:
        return "matchup"
    if p >= 0.55 and y == 0:
        return "workload"
    return "calibration"


def review_settled_misses(session: Session, *, limit: int = 40, league_id: str | None = None) -> dict[str, Any]:
    """Rule-based miss classification from backtests and shadow settlements. Never applied."""
    written = 0
    origins: dict[str, int] = {}

    # 1) Backtest predictions with optional box-score enrichment.
    bt_q = session.query(BacktestPrediction).order_by(BacktestPrediction.as_of.desc())
    backtests = bt_q.limit(limit * 3).all()
    for row in backtests:
        if written >= limit:
            break
        game = session.get(Game, row.game_id)
        if game is None:
            continue
        if league_id and game.league_id != league_id:
            continue
        stats = _stats(session, row.game_id, row.player_id)
        inactive = _inactive(session, row.game_id, row.player_id)
        origin = classify_settled_miss(
            p=row.p,
            y=row.y,
            stats=stats,
            inactive=inactive,
            market_id="anytime_td",
        )
        if _write_review(
            session,
            game_id=row.game_id,
            forecast_id=row.id,
            origin=origin,
            evidence=[
                {
                    "source": "backtest_predictions",
                    "ref": row.id,
                    "p": row.p,
                    "y": row.y,
                    "player_id": row.player_id,
                    "stats": stats,
                    "inactive": inactive,
                }
            ],
        ):
            written += 1
            origins[origin] = origins.get(origin, 0) + 1
            if written % 5 == 0:
                session.commit()

    # 2) Shadow settlements when present.
    shadow_rows = (
        session.query(ShadowPrediction, ShadowSettlement)
        .join(ShadowSettlement, ShadowSettlement.shadow_id == ShadowPrediction.shadow_id)
        .filter(ShadowSettlement.result.in_(("win", "loss")))
        .order_by(ShadowSettlement.settled_at.desc())
        .limit(limit)
        .all()
    )
    for pred, settle in shadow_rows:
        if written >= limit:
            break
        if league_id and pred.league_id != league_id:
            continue
        y = 1.0 if settle.result == "win" else 0.0
        stats = _stats(session, pred.game_id, pred.player_id)
        inactive = settle.result == "void" or _inactive(session, pred.game_id, pred.player_id)
        origin = classify_settled_miss(
            p=pred.forecast_probability,
            y=y,
            actual_value=settle.actual_value,
            line=pred.line,
            market_id=pred.market_id,
            stats=stats,
            inactive=inactive,
        )
        if _write_review(
            session,
            game_id=pred.game_id,
            forecast_id=pred.shadow_id,
            origin=origin,
            evidence=[
                {
                    "source": "shadow_settlements",
                    "ref": settle.settlement_id,
                    "shadow_id": pred.shadow_id,
                    "p": pred.forecast_probability,
                    "y": y,
                    "result": settle.result,
                    "actual_value": settle.actual_value,
                    "market_id": pred.market_id,
                    "stats": stats,
                }
            ],
        ):
            written += 1
            origins[origin] = origins.get(origin, 0) + 1

    return {
        "reviews_written": written,
        "origins": origins,
        "applied": False,
        "play_enabled": False,
        "retrain": False,
    }


def improve_with_ai(
    session: Session,
    model: LanguageModel,
    *,
    limit: int = 12,
    league_id: str | None = None,
) -> dict[str, Any]:
    """Ask OpenAI for testable hypotheses from verified settlement packets. Never applies them."""
    # Ensure rule-based reviews exist first.
    base = review_settled_misses(session, limit=limit, league_id=league_id)
    rows = (
        session.query(AiReview)
        .filter(AiReview.applied.is_(False))
        .order_by(AiReview.created_at.desc())
        .limit(limit)
        .all()
    )
    if league_id:
        filtered = []
        for row in rows:
            game = session.get(Game, row.game_id)
            if game and game.league_id == league_id:
                filtered.append(row)
        rows = filtered
    if not rows:
        return {**base, "ai_status": "no_reviews", "hypotheses": 0, "cost_usd": 0.0}

    packet = {
        "as_of": datetime.now(UTC).isoformat(),
        "play_enabled": False,
        "retrain_allowed": False,
        "note": "Hypotheses only. applied must remain false.",
        "misses": [
            {
                "forecast_id": row.forecast_id,
                "game_id": row.game_id,
                "origin": row.origin,
                "suggested_experiment": row.suggested_experiment,
                "evidence": row.evidence,
            }
            for row in rows
        ],
        "allowed_origins": list(IMPROVE_EXPERIMENTS),
    }
    digest = packet_hash("settlement_improve", PROMPT_VERSION, _model_name(model), packet)
    cached = (
        session.query(AiRun)
        .filter(AiRun.kind == "settlement_improve", AiRun.input_hash == digest, AiRun.status == "accepted")
        .one_or_none()
    )
    if cached:
        return {
            **base,
            "ai_status": "cached",
            "run_id": cached.run_id,
            "hypotheses": len((cached.output or {}).get("hypotheses") or []),
            "cost_usd": 0.0,
            "applied": False,
            "retrain": False,
        }

    parsed, usage = model.complete(instructions=IMPROVE_INSTRUCTIONS, user=_dump(packet), schema=ImprovementOutput)
    errors = _validate_improve(parsed, {row.forecast_id for row in rows})
    run = AiRun(
        kind="settlement_improve",
        game_id=None,
        status="rejected" if errors else "accepted",
        as_of=datetime.now(UTC),
        input_hash=digest,
        fingerprint="",
        model=_model_name(model),
        prompt_version=PROMPT_VERSION,
        token_input=usage.input_tokens,
        token_output=usage.output_tokens,
        cost_usd=usage.cost_usd,
        rejection_reasons=errors,
        output=parsed.model_dump(),
        data_tier="historical",
    )
    session.add(run)
    session.flush()
    written = 0
    if not errors:
        for hyp in parsed.hypotheses:
            if _write_review(
                session,
                game_id=_game_for(rows, hyp.forecast_id),
                forecast_id=hyp.forecast_id,
                origin=hyp.origin,
                evidence=[
                    {
                        "source": "settlement_improve",
                        "statement": hyp.statement,
                        "claim_type": hyp.claim_type,
                        "confidence": hyp.confidence,
                        "missing_information": hyp.missing_information,
                        "recommended_experiment": hyp.recommended_experiment,
                    }
                ],
                experiment=hyp.recommended_experiment,
            ):
                written += 1
    return {
        **base,
        "ai_status": run.status,
        "run_id": run.run_id,
        "hypotheses": written,
        "cost_usd": run.cost_usd,
        "rejection_reasons": errors,
        "applied": False,
        "retrain": False,
        "play_enabled": False,
    }


def _validate_improve(output: ImprovementOutput, known_ids: set[str]) -> list[str]:
    errors: list[str] = []
    for i, hyp in enumerate(output.hypotheses):
        if hyp.forecast_id not in known_ids:
            errors.append(f"hypothesis {i} names unknown forecast {hyp.forecast_id}")
        if not hyp.recommended_experiment.strip():
            errors.append(f"hypothesis {i} missing recommended_experiment")
        if hyp.claim_type == "fact":
            errors.append(f"hypothesis {i} cannot be labeled fact without new verified rows")
        lowered = (hyp.statement + " " + hyp.recommended_experiment).lower()
        if any(token in lowered for token in ("retrain", "update production", "enable play", "edge of", "override p")):
            errors.append(f"hypothesis {i} attempts to retrain, invent an edge, or enable Play")
    return errors


def _write_review(
    session: Session,
    *,
    game_id: str,
    forecast_id: str,
    origin: str,
    evidence: list[dict[str, Any]],
    experiment: str | None = None,
) -> bool:
    suggested = experiment or IMPROVE_EXPERIMENTS.get(origin, IMPROVE_EXPERIMENTS["unknown"])
    query = session.query(AiReview).filter(AiReview.forecast_id == forecast_id, AiReview.origin == origin)
    # AI hypotheses may share an origin with the rule-based row; keep distinct experiments.
    if experiment:
        query = query.filter(AiReview.suggested_experiment == suggested)
    if query.first():
        return False
    session.add(
        AiReview(
            game_id=game_id,
            forecast_id=forecast_id,
            origin=origin,
            suggested_experiment=suggested,
            applied=False,
            evidence=evidence,
        )
    )
    return True


def _stats(session: Session, game_id: str, player_id: str | None) -> dict[str, float]:
    if not player_id:
        return {}
    row = (
        session.query(PlayerGameStat)
        .filter(PlayerGameStat.game_id == game_id, PlayerGameStat.player_id == player_id)
        .first()
    )
    if row is None:
        return {}
    return {
        "targets": float(row.targets or 0),
        "receptions": float(row.receptions or 0),
        "rec_yards": float(row.rec_yards or 0),
        "rush_yards": float(row.rush_yards or 0),
        "carries": float(row.carries or 0),
        "rush_td": float(row.rush_td or 0),
        "rec_td": float(row.rec_td or 0),
    }


def _inactive(session: Session, game_id: str, player_id: str | None) -> bool:
    if not player_id:
        return False
    game = session.get(Game, game_id)
    if game is None:
        return False
    rows = (
        session.query(InjuryEvent)
        .filter(
            InjuryEvent.game_id == game_id,
            InjuryEvent.player_id == player_id,
            InjuryEvent.reported_at < game.kickoff_utc,
        )
        .order_by(InjuryEvent.reported_at.desc())
        .limit(1)
        .all()
    )
    if not rows:
        return False
    return rows[0].designation.strip().lower() in {"out", "ir", "injured reserve", "pup", "suspended"}


def _game_for(rows: list[AiReview], forecast_id: str) -> str:
    for row in rows:
        if row.forecast_id == forecast_id:
            return row.game_id
    return ""


def _model_name(model: LanguageModel) -> str:
    from spe.config import settings

    return getattr(model, "model", None) or settings.openai_model


def _dump(payload: Any) -> str:
    import json

    return json.dumps(payload, default=str)
