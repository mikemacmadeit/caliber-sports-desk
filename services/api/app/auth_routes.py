"""Sign up, log in, and the $19.99 Stripe subscription."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from spe.accounts.auth import (
    AuthError,
    authenticate,
    close_session,
    open_session,
    public_user,
    register_user,
)
from spe.config import settings

from app.access import COOKIE, token_from_request, user_from_request
from app.billing import (
    BillingError,
    apply_stripe_event,
    confirm_checkout,
    construct_event,
    create_checkout,
    create_portal,
)
from app.deps import db_session

router = APIRouter()

_SESSION_SECONDS = 60 * 60 * 24 * 30


class Credentials(BaseModel):
    email: str
    password: str


class CheckoutConfirm(BaseModel):
    session_id: str


def _set_cookie(response: JSONResponse, token: str) -> None:
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        secure=settings.app_env == "prod",
        samesite="lax",
        max_age=_SESSION_SECONDS,
        path="/",
    )


def _auth_error(exc: AuthError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


@router.post("/v1/auth/signup")
def signup(body: Credentials, session: Session = Depends(db_session)) -> JSONResponse:
    try:
        user = register_user(session, body.email, body.password)
        token = open_session(session, user)
    except AuthError as exc:
        raise _auth_error(exc) from exc
    response = JSONResponse({"token": token, "user": public_user(user)})
    _set_cookie(response, token)
    return response


@router.post("/v1/auth/login")
def login(body: Credentials, session: Session = Depends(db_session)) -> JSONResponse:
    try:
        user = authenticate(session, body.email, body.password)
        token = open_session(session, user)
    except AuthError as exc:
        raise _auth_error(exc) from exc
    response = JSONResponse({"token": token, "user": public_user(user)})
    _set_cookie(response, token)
    return response


@router.post("/v1/auth/logout")
def logout(request: Request, session: Session = Depends(db_session)) -> JSONResponse:
    close_session(session, token_from_request(request))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    return response


@router.get("/v1/auth/me")
def me(request: Request, session: Session = Depends(db_session)) -> dict:
    user = user_from_request(request, session)
    return {"user": None if user is None else public_user(user)}


@router.post("/v1/billing/checkout")
def checkout(request: Request, session: Session = Depends(db_session)) -> dict:
    user = user_from_request(request, session)
    if user is None:
        raise HTTPException(401, "Log in first.")
    try:
        url = create_checkout(user)
    except BillingError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    return {"url": url}


@router.post("/v1/billing/portal")
def portal(request: Request, session: Session = Depends(db_session)) -> dict:
    user = user_from_request(request, session)
    if user is None:
        raise HTTPException(401, "Log in first.")
    try:
        url = create_portal(user)
    except BillingError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    return {"url": url}


@router.post("/v1/billing/confirm")
def confirm(body: CheckoutConfirm, request: Request, session: Session = Depends(db_session)) -> dict:
    user = user_from_request(request, session)
    if user is None:
        raise HTTPException(401, "Log in first.")
    try:
        confirm_checkout(session, user, body.session_id)
    except BillingError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    return {"user": public_user(user)}


@router.post("/v1/billing/webhook")
async def stripe_webhook(request: Request, session: Session = Depends(db_session)) -> dict:
    payload = await request.body()
    try:
        event = construct_event(payload, request.headers.get("stripe-signature"))
    except BillingError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    apply_stripe_event(session, event)
    return {"ok": True}
