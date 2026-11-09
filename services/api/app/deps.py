from collections.abc import Iterator

from sqlalchemy.orm import Session

from spe.config import settings
from spe.db.session import session_factory


def db_session() -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def disclaimer() -> str:
    return settings.disclaimer
