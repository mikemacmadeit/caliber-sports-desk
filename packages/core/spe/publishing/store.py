"""Database-backed snapshots shared by independent API and worker containers."""

import json
from datetime import UTC, datetime
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from spe.db.models import BoardSnapshot
from spe.db.session import session_factory


def read(key):
    with session_factory() as db:
        row = db.get(BoardSnapshot, key)
        return row.payload if row else None


def write(key, payload):
    now = datetime.now(UTC)
    body = json.loads(json.dumps({**payload, "board_cached_at": now.isoformat()}, default=str))
    with session_factory() as db:
        insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        stmt = insert(BoardSnapshot).values(key=key, payload=body, updated_at=now)
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=["key"], set_={"payload": stmt.excluded.payload, "updated_at": now}
            )
        )
        db.commit()
    return body
