from __future__ import annotations

from datetime import datetime


class LeakageError(ValueError):
    pass


def assert_event_time(
    *,
    as_of: datetime,
    observed_at: datetime,
    ingested_at: datetime | None = None,
    effective_at: datetime | None = None,
) -> None:
    """Point-in-time join rule: never treat ingested_at or future revisions as historically available."""
    if observed_at > as_of:
        raise LeakageError(
            f"observed_at {observed_at.isoformat()} is after forecast as_of {as_of.isoformat()}"
        )
    if effective_at is not None and effective_at > as_of:
        raise LeakageError("effective_at is in the future relative to as_of")
    if ingested_at is not None and ingested_at < observed_at:
        # ingested_at may be later than observed_at (queue delay). It must never be used as as_of.
        pass
