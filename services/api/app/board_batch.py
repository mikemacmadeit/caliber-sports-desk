"""Request-scoped bulk reads for board generation; never a cross-request cache."""
from contextlib import contextmanager

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import joinedload

from spe.db.models import (
    AiOpportunity, AiSignal, Bookmaker, Forecast, Game, OddsTick, Player,
    Recommendation, Team,
)


def latest_rows(db, model, partition, order, where):
    primary = list(model.__table__.primary_key.columns)[0]
    ranked = select(
        primary.label("id"),
        func.row_number().over(partition_by=partition, order_by=[order, primary.desc()]).label("rn"),
    ).where(*where).subquery()
    return list(db.scalars(select(model).join(ranked, primary == ranked.c.id).where(ranked.c.rn == 1)))


@contextmanager
def board_batch(db, outcomes):
    """Keep ORM objects alive so session.get also avoids per-selection SQL."""
    ids = [o.outcome_id for o in outcomes]
    games = list(db.scalars(select(Game).options(
        joinedload(Game.home_team), joinedload(Game.away_team), joinedload(Game.venue),
    ).where(Game.game_id.in_({o.game_id for o in outcomes}))))
    players = list(db.scalars(select(Player).where(Player.player_id.in_({o.player_id for o in outcomes if o.player_id}))))
    books = list(db.scalars(select(Bookmaker)))
    teams = list(db.scalars(select(Team).where(Team.team_id.in_({t for g in games for t in (g.home_team_id, g.away_team_id)}))))
    forecasts, quotes, recommendations = {}, {}, {}
    # Stay under SQLite bind limits, and bound each Postgres window scan.
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        latest = latest_rows(db, Forecast, Forecast.outcome_id, Forecast.as_of.desc(), [
            Forecast.outcome_id.in_(chunk), Forecast.is_official.is_(True),
            or_(Forecast.reason_codes.is_(None), ~cast(Forecast.reason_codes, String).like('%IDENTITY_CONFLICT%')),
        ])
        forecasts.update({f.outcome_id: f for f in latest})
        for tick in latest_rows(db, OddsTick, [OddsTick.outcome_id, OddsTick.bookmaker_id], OddsTick.captured_at.desc(), [OddsTick.outcome_id.in_(chunk)]):
            quotes.setdefault(tick.outcome_id, []).append(tick)
        for rec in latest_rows(db, Recommendation, Recommendation.forecast_id, Recommendation.created_at.desc(), [Recommendation.forecast_id.in_([f.forecast_id for f in latest])]):
            recommendations[rec.forecast_id] = rec
    game_ids = [g.game_id for g in games]
    opportunities = latest_rows(db, AiOpportunity, [AiOpportunity.game_id, AiOpportunity.player_id, AiOpportunity.market_id], AiOpportunity.created_at.desc(), [AiOpportunity.game_id.in_(game_ids)])
    signals = latest_rows(db, AiSignal, [AiSignal.game_id, AiSignal.player_id, AiSignal.metric], AiSignal.created_at.desc(), [AiSignal.game_id.in_(game_ids)])
    data = {
        "forecasts": forecasts, "quotes": quotes, "recommendations": recommendations,
        "opportunities": {(r.game_id, r.player_id, r.market_id): r for r in opportunities},
        "signals": {(r.game_id, r.player_id, r.metric): r for r in signals},
        "held": [games, players, books, teams],
    }
    previous = db.info.get("board_batch")
    db.info["board_batch"] = data
    try:
        yield data
    finally:
        if previous is None:
            db.info.pop("board_batch", None)
        else:
            db.info["board_batch"] = previous
