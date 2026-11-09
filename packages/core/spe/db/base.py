from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import DateTime, Integer, String


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(dt: datetime | str | None) -> datetime | None:
    """SQLite returns naive datetimes (and sometimes ISO strings); treat them as UTC."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class ProvenanceMixin:
    """Every fact carries source provenance. Never treat ingested_at as historically available."""

    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider_record_id: Mapped[str] = mapped_column(String(160), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    quality_status: Mapped[str] = mapped_column(String(32), nullable=False, default="ok")
