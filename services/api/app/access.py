"""Resolve the desk viewer from the session cookie or a bearer token."""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from spe.accounts.auth import is_subscribed, user_for_token
from spe.db.models import AccountUser

COOKIE = "caliber_session"


def token_from_request(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        return token or None
    cookie = request.cookies.get(COOKIE)
    return cookie or None


def user_from_request(request: Request, session: Session) -> AccountUser | None:
    return user_for_token(session, token_from_request(request))


def request_is_subscribed(request: Request, session: Session) -> bool:
    return is_subscribed(user_from_request(request, session))
