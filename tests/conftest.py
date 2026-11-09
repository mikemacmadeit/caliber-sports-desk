from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from spe.config import settings
from spe.db import session as sess
from spe.db.session import init_db, session_factory


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    url = f"sqlite:///{tmp_path.as_posix()}/ingest.db"
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "parquet_dir", tmp_path / "parquet")
    monkeypatch.setattr(settings, "cfbd_api_key", "")
    monkeypatch.setattr(settings, "odds_api_key", "")
    sess._engine = None
    sess._SessionLocal = None
    init_db()
    s = session_factory()
    try:
        yield s
    finally:
        s.close()
        sess._engine = None
        sess._SessionLocal = None
