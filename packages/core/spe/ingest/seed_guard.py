"""Stop the demo seed from writing into a database that already has football data."""

from __future__ import annotations

from sqlalchemy.orm import Session

from spe.db.models import Game, Sport


def seed_block_reason(session: Session) -> str | None:
    if session.get(Sport, "football") is not None:
        return "football sport already exists"
    real = session.query(Game.game_id).filter(Game.data_tier != "demo").first()
    if real is not None:
        return "non-demo games already exist"
    return None
