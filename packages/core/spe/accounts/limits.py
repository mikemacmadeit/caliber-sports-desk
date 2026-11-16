"""Reserve auth attempts atomically across API workers, including failed requests."""
from datetime import UTC, datetime, timedelta
import hashlib

from sqlalchemy import delete
from sqlalchemy.orm import Session

from spe.db.models import AuthRateLimit


def consume(engine, keys: list[tuple[str, int]], *, now=None, window_seconds=600) -> bool:
    now = now or datetime.now(UTC)
    bucket = int(now.timestamp()) // window_seconds
    end = datetime.fromtimestamp((bucket + 1) * window_seconds, UTC)
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    allowed = True
    # A separate committed transaction survives a bad password/HTTP error rollback.
    with Session(engine) as db, db.begin():
        db.execute(delete(AuthRateLimit).where(AuthRateLimit.expires_at < now - timedelta(days=1)))
        for identity, limit in sorted(keys):
            key = hashlib.sha256(f"{bucket}:{identity}".encode()).hexdigest()
            stmt = insert(AuthRateLimit).values(key=key, expires_at=end, attempts=1)
            stmt = stmt.on_conflict_do_update(
                index_elements=[AuthRateLimit.key],
                set_={"attempts": AuthRateLimit.attempts + 1},
            ).returning(AuthRateLimit.attempts)
            allowed = db.execute(stmt).scalar_one() <= limit and allowed
    return allowed
