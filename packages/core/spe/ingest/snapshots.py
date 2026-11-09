from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from spe.db.base import new_id
from spe.db.models import CreditUsage, RawSnapshot
from spe.licensing import license_for


def append_raw(
    session: Session,
    *,
    provider: str,
    endpoint: str,
    provider_record_id: str,
    payload: dict[str, Any],
    observed_at: datetime,
    credit_cost: int = 0,
) -> RawSnapshot:
    lic = license_for(provider)
    row = RawSnapshot(
        snapshot_id=new_id(),
        provider=provider,
        endpoint=endpoint,
        provider_record_id=provider_record_id,
        observed_at=observed_at,
        payload=payload,
        license_name=lic.license_name,
        credit_cost=credit_cost,
    )
    session.add(row)
    if credit_cost:
        _add_credits(session, provider, credit_cost)
    return row


def remaining_credits(session: Session, provider: str, budget: int, day: str | None = None) -> int:
    if provider == "odds_api":
        from spe.ingest.odds_budget import remaining_soft_budget

        # Soft budget is settings-driven; ``budget`` arg kept for call-site compat.
        _ = budget
        return remaining_soft_budget(session, day=day)
    day = day or datetime.now(UTC).date().isoformat()
    existing = session.query(CreditUsage).filter_by(provider=provider, day=day).one_or_none()
    used = existing.credits_used if existing else 0
    return max(0, budget - used)


def _add_credits(session: Session, provider: str, cost: int) -> None:
    """Legacy additive path. Odds API live/spreads should prefer odds_budget.reserve_credits."""
    if provider == "odds_api":
        from spe.ingest.odds_budget import record_provider_actual, reserve_credits

        # Prefer soft-cap reservation; if already billed/recorded past the gate,
        # append to the ledger for truth without authorizing a new spend path.
        if not reserve_credits(session, cost):
            record_provider_actual(session, cost)
        return
    day = datetime.now(UTC).date().isoformat()
    row = session.query(CreditUsage).filter_by(provider=provider, day=day).one_or_none()
    if row is None:
        from spe.config import settings

        row = CreditUsage(
            provider=provider,
            day=day,
            credits_used=0,
            budget=settings.odds_credit_budget_per_day if provider == "odds_api" else 0,
        )
        session.add(row)
    row.credits_used += cost


def _credit_pk(session: Session, provider: str, day: str) -> str | None:
    row = session.query(CreditUsage).filter_by(provider=provider, day=day).one_or_none()
    return row.id if row else None
