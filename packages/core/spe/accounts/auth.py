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
from spe.db.models import AccountUser, AuthSession, EmailVerification, PasswordReset

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
        "billing_available": bool(user.stripe_customer_id),
        "email_verified": bool(user.email_verified_at),
    }


def is_subscribed(user: AccountUser | None) -> bool:
    if user is None or user.deleted_at:
        return False
    if user.is_admin:
        return True
    if user.subscription_status not in ACTIVE_STATUSES:
        return False
    end = as_utc(user.current_period_end)
    if end is None or end <= datetime.now(UTC):
        return False
    return True


def register_user(session: Session, email: str, password: str) -> AccountUser:
    cleaned = normalize_email(email)
    if not EMAIL_RE.match(cleaned):
        raise AuthError("Enter a valid email.")
    _check_password(password)
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
    if user is None or user.deleted_at or not verify_password(password, user.password_hash):
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


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError("Use at least 8 characters.")
    if len(password) > MAX_PASSWORD:
        raise AuthError("That password is too long.")


def begin_password_reset(session: Session, email: str) -> tuple[AccountUser, str] | None:
    """Return the account and a one-hour token. Unknown emails return nothing."""
    user = session.query(AccountUser).filter(AccountUser.email == normalize_email(email)).one_or_none()
    if user is None:
        return None
    session.query(PasswordReset).filter(PasswordReset.user_id == user.user_id).delete()
    token = secrets.token_urlsafe(32)
    session.add(
        PasswordReset(
            token_id=_token_hash(token),
            user_id=user.user_id,
            expires_at=utcnow() + timedelta(hours=1),
        )
    )
    session.flush()
    return user, token


def finish_password_reset(session: Session, token: str, password: str) -> AccountUser:
    _check_password(password)
    row = session.query(PasswordReset).filter_by(token_id=_token_hash(token or "")).with_for_update().one_or_none()
    expires = as_utc(row.expires_at) if row is not None else None
    if row is None or expires is None or expires <= datetime.now(UTC):
        raise AuthError("That reset link is no longer valid.")
    user = session.get(AccountUser, row.user_id)
    if user is None:
        raise AuthError("That reset link is no longer valid.")
    user.password_hash = hash_password(password)
    user.email_verified_at = utcnow()
    session.query(AuthSession).filter(AuthSession.user_id == user.user_id).delete()
    session.delete(row)
    session.flush()
    return user


def user_by_stripe_customer(session: Session, customer_id: str | None) -> AccountUser | None:
    if not customer_id:
        return None
    return session.query(AccountUser).filter(AccountUser.stripe_customer_id == customer_id).one_or_none()


def begin_email_verification(session: Session, user: AccountUser) -> str:
    session.query(EmailVerification).filter_by(user_id=user.user_id).delete()
    token = secrets.token_urlsafe(32)
    session.add(EmailVerification(token_id=_token_hash(token), user_id=user.user_id,
                                  expires_at=utcnow() + timedelta(hours=1)))
    session.flush()
    return token


def finish_email_verification(session: Session, token: str) -> AccountUser:
    row = session.query(EmailVerification).filter_by(token_id=_token_hash(token)).with_for_update().one_or_none()
    if row is None or as_utc(row.expires_at) <= utcnow():
        raise AuthError("That verification link is no longer valid.")
    user = session.get(AccountUser, row.user_id)
    if user is None or user.deleted_at:
        raise AuthError("That verification link is no longer valid.")
    user.email_verified_at = utcnow()
    session.delete(row)
    session.flush()
    return user


def remove_account(session: Session, user: AccountUser, password: str) -> None:
    if not verify_password(password, user.password_hash):
        raise AuthError("Your password is incorrect.", status=401)
    from spe.db.models import WatchlistEntry
    for table in (AuthSession, PasswordReset, EmailVerification, WatchlistEntry):
        session.query(table).filter_by(user_id=user.user_id).delete()
    # Keep the billing reference for audit/reconciliation; remove login and contact data.
    user.email = f"deleted-{user.user_id}@deleted.invalid"
    user.password_hash = hash_password(secrets.token_urlsafe(48))
    user.email_verified_at = None
    user.deleted_at = utcnow()
    user.is_admin = False
    session.flush()
