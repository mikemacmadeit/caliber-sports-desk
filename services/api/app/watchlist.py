"""Account-owned research bookmarks. Client annotations never become public picks."""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.access import user_from_request
from app.deps import db_session
from spe.db.base import as_utc, utcnow
from spe.db.models import AccountUser, WatchlistEntry

router = APIRouter()


class Selection(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{24}$")
    player: str = Field(min_length=1, max_length=150)
    pick: str = Field(min_length=1, max_length=200)
    kickoff: datetime
    book: str | None = Field(None, max_length=100)
    price: int | None = Field(None, ge=-100000, le=100000)
    game_id: str = Field(min_length=1, max_length=64)
    team: str | None = Field(None, max_length=32)


class Change(BaseModel):
    action: Literal["save", "remove"]
    id: str = Field(pattern=r"^[a-f0-9]{24}$")
    selection: Selection | None = None


def owner(request, db):
    user = user_from_request(request, db)
    if user is None:
        raise HTTPException(401, "Sign in to save research across devices.")
    return user


def entries(db, user_id):
    return {"saved": [{**r.payload, "saved_at": as_utc(r.saved_at).isoformat()} for r in
        db.query(WatchlistEntry).filter_by(user_id=user_id).order_by(WatchlistEntry.saved_at)]}


@router.get("/v1/watchlist")
def read_watchlist(request: Request, db: Session = Depends(db_session)):
    return entries(db, owner(request, db).user_id)


@router.post("/v1/watchlist")
def change_watchlist(change: Change, request: Request, db: Session = Depends(db_session)):
    user = owner(request, db)
    # Serialize writes from multiple devices, including the 100-item capacity check.
    db.query(AccountUser).filter_by(user_id=user.user_id).with_for_update().one()
    existing = db.get(WatchlistEntry, (user.user_id, change.id))
    if change.action == "remove":
        if existing:
            db.delete(existing)
    else:
        if change.selection is None or change.selection.id != change.id:
            raise HTTPException(422, "A matching selection is required.")
        if existing is None:
            if db.query(WatchlistEntry).filter_by(user_id=user.user_id).count() >= 100:
                raise HTTPException(409, "Your watchlist is full. Remove a selection first.")
            db.add(WatchlistEntry(user_id=user.user_id, selection_id=change.id,
                payload=change.selection.model_dump(mode="json"), saved_at=utcnow()))
    db.flush()
    return entries(db, user.user_id)
