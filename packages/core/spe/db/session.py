from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from spe.config import settings
from spe.db.base import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _normalize_database_url(url: str) -> str:
    """Render/Railway often hand back postgres:// — SQLAlchemy wants postgresql+psycopg://."""
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://") and "+psycopg" not in url:
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def set_busy_timeout(session: Session, ms: int = 300_000) -> None:
    """SQLite only. No-op on Postgres so cloud workers don't crash."""
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "sqlite":
        return
    session.connection().exec_driver_sql(f"PRAGMA busy_timeout={int(ms)}")


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        url = _normalize_database_url(settings.database_url)
        if url.startswith("sqlite"):
            db_path = url.split("///", 1)[-1]
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            _engine = create_engine(url, future=True, echo=False)
            _enable_sqlite_fk(_engine)
        else:
            _engine = create_engine(url, future=True, echo=False, pool_pre_ping=True)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def _enable_sqlite_fk(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        # API reads must fail before the website's 15s AbortSignal timeout.
        # Long ingest jobs set a higher busy_timeout on their own connections.
        cursor.execute("PRAGMA busy_timeout=8000")
        cursor.close()


def get_session() -> Iterator[Session]:
    get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def session_factory() -> Session:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def init_db() -> None:
    get_engine()
    import spe.db.models  # noqa: F401
    from sqlalchemy.exc import OperationalError

    from spe.db.migrate import ensure_schema

    engine = get_engine()
    try:
        # A long SQLite ingest can hold the write lock. Fail that attempt quickly
        # and keep serving the tables that are already there. Skip on Postgres.
        with engine.begin() as conn:
            if engine.dialect.name == "sqlite":
                conn.exec_driver_sql("PRAGMA busy_timeout=2500")
            Base.metadata.create_all(bind=conn)
        ensure_schema(engine)
    except OperationalError:
        pass
    settings.parquet_dir.mkdir(parents=True, exist_ok=True)
