"""Email login. Sessions are random tokens; the database keeps only the hash."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from spe.db.base import as_utc, utcnow
from spe.db.models import AccountUser, AuthSession

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8
MAX_PASSWORD = 200
SESSION_DAYS = 30
PBKDF2_ROUNDS = 210_000

ACTIVE_STATUSES = {"active", "trialing"}


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, digest = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds))
        return hmac.compare_digest(check.hex(), digest)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def public_user(user: AccountUser) -> dict:
    return {
        "email": user.email,
        "subscribed": is_subscribed(user),
        "subscription_status": user.subscription_status,
        "admin": bool(user.is_admin),
    }


def is_subscribed(user: AccountUser | None) -> bool:
    if user is None:
        return False
    if user.is_admin:
        return True
    if user.subscription_status not in ACTIVE_STATUSES:
        return False
    end = as_utc(user.current_period_end)
    if end is not None and end <= datetime.now(UTC):
        return False
    return True


def register_user(session: Session, email: str, password: str) -> AccountUser:
    cleaned = normalize_email(email)
    if not EMAIL_RE.match(cleaned):
        raise AuthError("Enter a valid email.")
    if len(password) < MIN_PASSWORD:
        raise AuthError("Use at least 8 characters.")
    if len(password) > MAX_PASSWORD:
        raise AuthError("That password is too long.")
    user = AccountUser(email=cleaned, password_hash=hash_password(password))
    session.add(user)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise AuthError("An account with that email already exists.", status=409) from None
    return user


def authenticate(session: Session, email: str, password: str) -> AccountUser:
    user = session.query(AccountUser).filter(AccountUser.email == normalize_email(email)).one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        raise AuthError("Email or password is wrong.", status=401)
    return user


def open_session(session: Session, user: AccountUser) -> str:
    token = secrets.token_urlsafe(32)
    row = AuthSession(
        session_id=_token_hash(token),
        user_id=user.user_id,
        expires_at=utcnow() + timedelta(days=SESSION_DAYS),
    )
    session.add(row)
    session.flush()
    return token


def user_for_token(session: Session, token: str | None) -> AccountUser | None:
    if not token:
        return None
    row = session.get(AuthSession, _token_hash(token))
    if row is None:
        return None
    expires = as_utc(row.expires_at)
    if expires is not None and expires <= datetime.now(UTC):
        session.delete(row)
        return None
    return session.get(AccountUser, row.user_id)


def close_session(session: Session, token: str | None) -> None:
    if not token:
        return
    row = session.get(AuthSession, _token_hash(token))
    if row is not None:
        session.delete(row)


def user_by_stripe_customer(session: Session, customer_id: str | None) -> AccountUser | None:
    if not customer_id:
        return None
    return session.query(AccountUser).filter(AccountUser.stripe_customer_id == customer_id).one_or_none()
