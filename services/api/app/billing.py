"""Stripe Checkout for the $19.99 monthly desk. Cards never touch this server."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from spe.accounts.auth import user_by_stripe_customer
from spe.config import settings
from spe.db.models import AccountUser

PRICE_CENTS = 1999
PRODUCT_NAME = "Caliber"


class BillingError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def stripe_ready() -> bool:
    return bool(settings.stripe_secret_key)


def _stripe():
    if not stripe_ready():
        raise BillingError("Stripe is not configured yet.", status=503)
    import stripe

    stripe.api_key = settings.stripe_secret_key
    return stripe


def _as_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return dict(obj)


def _period_end(sub: dict[str, Any]) -> datetime | None:
    raw = sub.get("current_period_end")
    if raw is None:
        items = (sub.get("items") or {}).get("data") or []
        if items:
            raw = items[0].get("current_period_end")
    if raw is None:
        return None
    return datetime.fromtimestamp(int(raw), UTC)


def apply_subscription(user: AccountUser, sub: Any) -> None:
    data = _as_dict(sub)
    user.subscription_id = data.get("id") or user.subscription_id
    user.subscription_status = str(data.get("status") or "canceled")
    customer = data.get("customer")
    if customer:
        user.stripe_customer_id = str(customer)
    end = _period_end(data)
    if end is not None:
        user.current_period_end = end


def apply_stripe_event(session: Session, event: dict[str, Any]) -> None:
    kind = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    if kind == "checkout.session.completed":
        _apply_checkout(session, obj)
        return
    if kind in {"customer.subscription.updated", "customer.subscription.deleted"}:
        _apply_subscription_event(session, obj)


def _apply_checkout(session: Session, checkout: dict[str, Any]) -> None:
    user_id = checkout.get("client_reference_id") or (checkout.get("metadata") or {}).get("user_id")
    user = session.get(AccountUser, user_id) if user_id else None
    if user is None:
        user = user_by_stripe_customer(session, checkout.get("customer"))
    if user is None:
        return
    customer = checkout.get("customer")
    if customer:
        user.stripe_customer_id = str(customer)
    subscription = checkout.get("subscription")
    if isinstance(subscription, dict):
        apply_subscription(user, subscription)
        return
    if subscription and stripe_ready():
        stripe = _stripe()
        apply_subscription(user, stripe.Subscription.retrieve(str(subscription)))
        return
    if checkout.get("status") == "complete" and checkout.get("mode") == "subscription":
        user.subscription_status = "active"


def _apply_subscription_event(session: Session, sub: dict[str, Any]) -> None:
    user_id = (sub.get("metadata") or {}).get("user_id")
    user = session.get(AccountUser, user_id) if user_id else None
    if user is None:
        user = user_by_stripe_customer(session, sub.get("customer"))
    if user is None:
        return
    apply_subscription(user, sub)


def _line_item() -> dict[str, Any]:
    if settings.stripe_price_id:
        return {"price": settings.stripe_price_id, "quantity": 1}
    cents = int(settings.subscription_price_cents or PRICE_CENTS)
    return {
        "quantity": 1,
        "price_data": {
            "currency": "usd",
            "unit_amount": cents,
            "recurring": {"interval": "month"},
            "product_data": {"name": PRODUCT_NAME},
        },
    }


def _web_origin() -> str:
    for part in str(settings.web_origin or "").split(","):
        origin = part.strip().rstrip("/")
        if origin:
            return origin
    return "http://localhost:3000"


def create_checkout(user: AccountUser) -> str:
    stripe = _stripe()
    origin = _web_origin()
    params: dict[str, Any] = {
        "mode": "subscription",
        "client_reference_id": user.user_id,
        "line_items": [_line_item()],
        "success_url": f"{origin}/account?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{origin}/account?checkout=cancel",
        "metadata": {"user_id": user.user_id},
        "subscription_data": {"metadata": {"user_id": user.user_id}},
        "allow_promotion_codes": True,
    }
    if user.stripe_customer_id:
        params["customer"] = user.stripe_customer_id
    else:
        params["customer_email"] = user.email
    created = stripe.checkout.Session.create(**params)
    url = created.get("url") if isinstance(created, dict) else created.url
    if not url:
        raise BillingError("Stripe did not return a checkout link.", status=502)
    return str(url)


def create_portal(user: AccountUser) -> str:
    if not user.stripe_customer_id:
        raise BillingError("No Stripe customer on this account yet.", status=400)
    stripe = _stripe()
    origin = _web_origin()
    created = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=f"{origin}/account",
    )
    url = created.get("url") if isinstance(created, dict) else created.url
    if not url:
        raise BillingError("Stripe did not return a billing link.", status=502)
    return str(url)


def confirm_checkout(session: Session, user: AccountUser, checkout_session_id: str) -> AccountUser:
    stripe = _stripe()
    checkout = _as_dict(stripe.checkout.Session.retrieve(checkout_session_id))
    owner = checkout.get("client_reference_id") or (checkout.get("metadata") or {}).get("user_id")
    if owner != user.user_id:
        raise BillingError("That checkout belongs to a different account.", status=403)
    _apply_checkout(session, checkout)
    session.flush()
    return user


def construct_event(payload: bytes, signature: str | None) -> dict[str, Any]:
    if not settings.stripe_webhook_secret:
        raise BillingError("Stripe webhook secret is not configured.", status=503)
    stripe = _stripe()
    try:
        event = stripe.Webhook.construct_event(payload, signature or "", settings.stripe_webhook_secret)
    except Exception as exc:
        raise BillingError("Invalid Stripe signature.", status=400) from exc
    return _as_dict(event)
