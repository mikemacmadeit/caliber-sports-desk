"""Sign up, log in, and the $19.99 Stripe subscription."""

from __future__ import annotations

import time
import hashlib
import hmac
import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from spe.accounts.auth import (
    AuthError,
    authenticate,
    begin_email_verification,
    begin_password_reset,
    close_session,
    finish_password_reset,
    finish_email_verification,
    normalize_email,
    open_session,
    public_user,
    register_user,
    remove_account,
)
from spe.config import settings

from app.access import COOKIE, token_from_request, user_from_request
from app.mail import mail_configured, send_password_reset, send_email_verification
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
_EMAIL_LIMIT = 8
_IP_LIMIT = 30
def caller_address(forwarded: str, client_host: str | None) -> str:
    """The visitor, not the shared proxy hop at the end of X-Forwarded-For."""
    parts = [part.strip() for part in forwarded.split(",") if part.strip()]
    if len(parts) >= 2:
        return parts[-2]
    if parts:
        return parts[0]
    return client_host or "unknown"


def _caller(request: Request) -> str:
    host = request.client.host if request.client is not None else None
    ip = request.headers.get("x-caliber-client-ip", "")
    stamp = request.headers.get("x-caliber-client-time", "")
    signature = request.headers.get("x-caliber-client-signature", "")
    if settings.auth_proxy_secret and ip and stamp:
        expected = hmac.new(settings.auth_proxy_secret.encode(), f"{stamp}:{ip}".encode(), hashlib.sha256).hexdigest()
        try:
            if abs(time.time() - int(stamp)) <= 300 and hmac.compare_digest(signature, expected):
                return str(ipaddress.ip_address(ip))
        except ValueError:
            pass
    # Forwarded headers supplied by arbitrary callers are not an identity boundary.
    return host or "unknown"


def _reserve(request: Request, session: Session, action: str, email: str | None = None) -> None:
    from spe.accounts.limits import consume
    keys = [(f"{action}:ip:{_caller(request)}", _IP_LIMIT)]
    if email is not None:
        keys.append((f"{action}:email:{normalize_email(email)}", _EMAIL_LIMIT))
    if not consume(session.get_bind(), keys):
        raise HTTPException(429, "Too many attempts. Wait a few minutes and try again.", headers={"Retry-After": "600"})


class Credentials(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=200)


class CheckoutConfirm(BaseModel):
    session_id: str = Field(max_length=256)


class ResetRequest(BaseModel):
    email: str = Field(max_length=320)


class ResetPassword(BaseModel):
    token: str = Field(max_length=256)
    password: str = Field(max_length=200)


class VerifyEmail(BaseModel):
    token: str = Field(max_length=256)


class DeleteAccount(BaseModel):
    password: str = Field(max_length=200)


def _set_cookie(response: JSONResponse, token: str) -> None:
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        secure=settings.app_env in {"prod", "production"},
        samesite="lax",
        max_age=_SESSION_SECONDS,
        path="/",
    )


def _auth_error(exc: AuthError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


@router.post("/v1/auth/signup")
def signup(body: Credentials, request: Request, session: Session = Depends(db_session)) -> JSONResponse:
    _reserve(request, session, "signup", body.email)
    try:
        user = register_user(session, body.email, body.password)
        token = open_session(session, user)
    except AuthError as exc:
        raise _auth_error(exc) from exc
    response = JSONResponse({"token": token, "user": public_user(user)})
    _set_cookie(response, token)
    return response


@router.post("/v1/auth/login")
def login(body: Credentials, request: Request, session: Session = Depends(db_session)) -> JSONResponse:
    _reserve(request, session, "login", body.email)
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


@router.post("/v1/auth/forgot")
def forgot(body: ResetRequest, request: Request, session: Session = Depends(db_session)) -> dict:
    if not mail_configured():
        raise HTTPException(503, "Password reset email is not set up yet.")
    _reserve(request, session, "forgot", body.email)
    started = begin_password_reset(session, body.email)
    if started is not None:
        user, token = started
        try:
            send_password_reset(user.email, token)
        except Exception as exc:
            raise HTTPException(503, "Could not send the reset email. Try again.") from exc
    return {"ok": True}


@router.post("/v1/auth/reset")
def reset(body: ResetPassword, request: Request, session: Session = Depends(db_session)) -> JSONResponse:
    _reserve(request, session, "reset")
    try:
        user = finish_password_reset(session, body.token, body.password)
        token = open_session(session, user)
    except AuthError as exc:
        raise _auth_error(exc) from exc
    response = JSONResponse({"token": token, "user": public_user(user)})
    _set_cookie(response, token)
    return response


@router.get("/v1/auth/me")
def me(request: Request, session: Session = Depends(db_session)) -> dict:
    user = user_from_request(request, session)
    return {"user": None if user is None else public_user(user)}


@router.post("/v1/auth/verification")
def request_verification(request: Request, session: Session = Depends(db_session)):
    _reserve(request, session, "verification")
    user = user_from_request(request, session)
    if user is None:
        raise HTTPException(401, "Log in first.")
    if user.email_verified_at:
        return {"ok": True}
    if not mail_configured():
        raise HTTPException(503, "Verification email is not set up yet.")
    token = begin_email_verification(session, user)
    try:
        send_email_verification(user.email, token)
    except Exception:
        raise HTTPException(503, "Could not send the email. Please try again.") from None
    return {"ok": True}


@router.post("/v1/auth/verify")
def verify_email(body: VerifyEmail, request: Request, session: Session = Depends(db_session)):
    _reserve(request, session, "verify")
    try:
        finish_email_verification(session, body.token)
    except AuthError as exc:
        raise _auth_error(exc) from exc
    return {"ok": True}


@router.post("/v1/auth/delete")
def delete_account(body: DeleteAccount, request: Request, session: Session = Depends(db_session)):
    _reserve(request, session, "delete")
    user = user_from_request(request, session)
    if user is None:
        raise HTTPException(401, "Log in first.")
    if user.stripe_customer_id:
        from app.billing import _stripe, _as_dict
        try:
            subscriptions = _as_dict(_stripe().Subscription.list(customer=user.stripe_customer_id, status="all", limit=100))
        except Exception:
            raise HTTPException(503, "Billing must be checked before deleting this account. Please try again.") from None
        if any(s.get("status") in {"active", "trialing", "past_due", "unpaid", "incomplete"} and not s.get("cancel_at_period_end")
               for s in subscriptions.get("data", [])):
            raise HTTPException(409, "Cancel your subscription in billing before deleting your account.")
    try:
        remove_account(session, user, body.password)
    except AuthError as exc:
        raise _auth_error(exc) from exc
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    return response


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
        apply_stripe_event(session, event)
    except BillingError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    return {"ok": True}
