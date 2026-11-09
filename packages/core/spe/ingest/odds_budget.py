"""Concurrency-safe Odds API soft-budget ledger shared by all consumers.

Local soft ceiling is independent of the provider quota. Both are reported.
Workers reserve credits before calling the provider and commit/release after.

Near-kickoff reserve is held *inside* the effective soft ceiling — never above it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.config import settings
from spe.db.models import CreditUsage


PROVIDER = "odds_api"

# Operator-approved Option B (2026-11-07): temporary TOTAL soft ceiling 2312 until
# 2026-11-08T00:00:00Z. With ~2051 already used that day, remaining ≈ 261 — not +312
# from the current balance. Restores to odds_credit_budget_per_day at expiry.


def budget_day(now: datetime | None = None) -> str:
    """Soft budget resets at UTC midnight."""
    return (now or datetime.now(UTC)).astimezone(UTC).date().isoformat()


def _parse_until(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def effective_soft_budget(now: datetime | None = None) -> int:
    """Active soft ceiling: temporary approved total, else configured daily.

    Temporary ceiling is a raised *total* for the soft day, not an additive
    grant on top of credits already spent.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    standard = int(settings.odds_credit_budget_per_day)
    temporary = int(getattr(settings, "odds_credit_budget_temporary", 0) or 0)
    until_raw = str(getattr(settings, "odds_credit_budget_temporary_until", "") or "")
    until = _parse_until(until_raw) if until_raw else None
    if temporary > standard and until is not None and now < until:
        return temporary
    return standard


def temporary_budget_active(now: datetime | None = None) -> bool:
    return effective_soft_budget(now) > int(settings.odds_credit_budget_per_day)


def ensure_credit_row(session: Session, *, day: str | None = None) -> CreditUsage:
    day = day or budget_day()
    ceiling = effective_soft_budget()
    row = session.query(CreditUsage).filter_by(provider=PROVIDER, day=day).one_or_none()
    if row is None:
        row = CreditUsage(
            provider=PROVIDER,
            day=day,
            credits_used=0,
            budget=ceiling,
            notes=None,
        )
        session.add(row)
        session.flush()
    else:
        row.budget = ceiling
        if temporary_budget_active() and (not row.notes or "Option B" not in str(row.notes)):
            row.notes = (
                (str(row.notes) + " | " if row.notes else "")
                + "Option B approved temporary soft ceiling 2312 until 2026-11-08T00:00:00Z; "
                "near reserve within cap; no plan purchase."
            )
    return row


def used_credits(session: Session, *, day: str | None = None) -> int:
    day = day or budget_day()
    row = session.query(CreditUsage).filter_by(provider=PROVIDER, day=day).one_or_none()
    return int(row.credits_used) if row else 0


def remaining_soft_budget(session: Session, *, day: str | None = None) -> int:
    day = day or budget_day()
    used = used_credits(session, day=day)
    return max(0, effective_soft_budget() - used)


def soft_overspend(session: Session, *, day: str | None = None) -> int:
    """Credits used above the *effective* soft ceiling (should stay 0 for new spends)."""
    day = day or budget_day()
    used = used_credits(session, day=day)
    return max(0, used - effective_soft_budget())


def reserve_credits(session: Session, amount: int, *, day: str | None = None) -> bool:
    """Atomically reserve ``amount`` soft credits. Returns False if insufficient.

    Uses a SQLite write lock via BEGIN IMMEDIATE semantics through a conditional
    UPDATE so concurrent workers cannot spend the same remaining allowance twice.
    """
    amount = int(amount)
    if amount <= 0:
        return True
    day = day or budget_day()
    ensure_credit_row(session, day=day)
    budget = effective_soft_budget()
    result = session.execute(
        text(
            """
            UPDATE credit_usage
            SET credits_used = credits_used + :amount
            WHERE provider = :provider
              AND day = :day
              AND credits_used + :amount <= :budget
            """
        ),
        {"amount": amount, "provider": PROVIDER, "day": day, "budget": budget},
    )
    session.flush()
    return bool(result.rowcount)


def force_spend_near_emergency(session: Session, amount: int, *, day: str | None = None) -> bool:
    """DEPRECATED alias — near reserve must stay inside the soft cap."""
    return reserve_credits(session, amount, day=day)


def record_provider_actual(
    session: Session,
    amount: int,
    *,
    day: str | None = None,
) -> None:
    """Record provider-reported cost after a call already completed.

    May leave the soft ledger above the configured ceiling for audit truth when
    actual cost exceeded the pre-call reservation. Does not authorize new spends.
    """
    amount = int(amount)
    if amount <= 0:
        return
    day = day or budget_day()
    ensure_credit_row(session, day=day)
    session.execute(
        text(
            """
            UPDATE credit_usage
            SET credits_used = credits_used + :amount
            WHERE provider = :provider AND day = :day
            """
        ),
        {"amount": amount, "provider": PROVIDER, "day": day},
    )
    session.flush()


def release_reservation(session: Session, amount: int, *, day: str | None = None) -> None:
    """Return unused reserved credits (e.g. fixture/no-op path)."""
    amount = int(amount)
    if amount <= 0:
        return
    day = day or budget_day()
    ensure_credit_row(session, day=day)
    session.execute(
        text(
            """
            UPDATE credit_usage
            SET credits_used = CASE
                WHEN credits_used >= :amount THEN credits_used - :amount
                ELSE 0
            END
            WHERE provider = :provider AND day = :day
            """
        ),
        {"amount": amount, "provider": PROVIDER, "day": day},
    )
    session.flush()


def reconcile_actual_cost(
    session: Session,
    reserved: int,
    actual: int,
    *,
    day: str | None = None,
) -> None:
    """After a provider call, adjust the reservation to the reported actual cost."""
    reserved = int(reserved)
    actual = int(actual)
    if actual == reserved:
        return
    if actual < reserved:
        release_reservation(session, reserved - actual, day=day)
        return
    extra = actual - reserved
    if reserve_credits(session, extra, day=day):
        return
    record_provider_actual(session, extra, day=day)


def spendable_for_bucket(
    session: Session,
    bucket: str,
    *,
    allow_near_emergency: bool = False,
) -> int:
    """How many soft credits this bucket may still spend.

    Mid/far leave the near-reserve untouched *within* the effective ceiling.
    No path spends past the ceiling.
    """
    left = remaining_soft_budget(session)
    near_reserve = max(0, int(settings.odds_near_reserve_credits))
    _ = allow_near_emergency
    if bucket == "near":
        return left
    return max(0, left - near_reserve)


def budget_status(session: Session, *, provider_remaining: int | None = None) -> dict[str, Any]:
    day = budget_day()
    used = used_credits(session, day=day)
    soft = effective_soft_budget()
    standard = int(settings.odds_credit_budget_per_day)
    return {
        "provider": PROVIDER,
        "local_day_utc": day,
        "local_reset": "UTC midnight",
        "local_soft_budget": soft,
        "standard_daily_budget": standard,
        "temporary_ceiling_active": temporary_budget_active(),
        "temporary_ceiling": int(getattr(settings, "odds_credit_budget_temporary", 0) or 0) or None,
        "temporary_until": str(getattr(settings, "odds_credit_budget_temporary_until", "") or "") or None,
        "local_credits_used": used,
        "local_credits_remaining": max(0, soft - used),
        "near_reserve_credits": int(settings.odds_near_reserve_credits),
        "near_reserve_policy": "within_soft_cap_only",
        "near_emergency_overspend": soft_overspend(session, day=day),
        "soft_cap_hard": True,
        "provider_quota_remaining": provider_remaining,
        "provider_reset": "provider monthly/plan cycle (see Odds API dashboard)",
        "play_enabled": False,
    }
