"""Regression: Anytime TD research must not hang behind ingest SQLite locks."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError


@pytest.fixture()
def finder_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    research = tmp_path / "research" / "atd_unified"
    research.mkdir(parents=True)
    path = research / "anytime_td_finder.json"
    payload = {
        "status": "ok",
        "as_of": "2026-11-07T00:00:00+00:00",
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "validated_play": False,
        "n_games": 1,
        "games": [
            {
                "game_id": "espn:fbs:test",
                "league_id": "fbs",
                "matchup": "A @ B",
                "players": [],
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    def _path() -> Path:
        return path

    monkeypatch.setattr("spe.research.atd_guardrails.finder_payload_path", _path)
    return path


def test_research_serves_file_without_db_session(finder_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """File-backed research must not open a SQLite session (ingest cannot block it)."""
    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        raise AssertionError("session_factory must not run when finder JSON exists")

    monkeypatch.setattr("spe.db.session.session_factory", _boom)
    monkeypatch.setattr("spe.db.session.init_db", lambda: None)

    from app.main import app

    client = TestClient(app)
    res = client.get("/v1/anytime-td/research?league=fbs")
    assert res.status_code == 200
    body = res.json()
    assert body["payload_source"] == "file"
    assert body["n_games"] == 1
    assert body["games"][0]["league_id"] == "fbs"
    assert body["play_enabled"] is False
    assert body["validated_play"] is False
    assert calls["n"] == 0
    assert finder_file.exists()


def test_api_busy_timeout_is_below_website_abort() -> None:
    from spe.db import session as sess

    # Re-read pragma constants from source contract: API default must be < 15000ms.
    src = Path(sess.__file__).read_text(encoding="utf-8")
    assert "busy_timeout=8000" in src
    assert "busy_timeout=120000" not in src


def test_locked_database_maps_to_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "lock.db"
    # Seed minimal sqlite file
    seed = sqlite3.connect(str(db))
    seed.execute("CREATE TABLE ping(x INTEGER)")
    seed.execute("INSERT INTO ping VALUES (1)")
    seed.commit()
    seed.close()

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    import spe.db.session as sessmod

    sessmod._engine = None
    sessmod._SessionLocal = None

    holder = sqlite3.connect(str(db), timeout=1)
    holder.execute("PRAGMA journal_mode=DELETE")  # force lock contention on readers/writers
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("INSERT INTO ping VALUES (2)")

    from spe.db.session import get_engine

    eng = get_engine()
    err: Exception | None = None
    t0 = time.perf_counter()
    try:
        with eng.connect() as conn:
            conn.exec_driver_sql("SELECT x FROM ping")
    except Exception as e:  # noqa: BLE001 — assert type below
        err = e
    elapsed_ms = (time.perf_counter() - t0) * 1000
    holder.rollback()
    holder.close()

    assert err is not None
    assert isinstance(err, OperationalError) or "locked" in str(err).lower()
    # Fail well before the website's 15s AbortSignal.
    assert elapsed_ms < 15000


def test_exception_handler_returns_503_for_locked() -> None:
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import OperationalError

    from app.main import sqlite_operational_error_handler

    mini = FastAPI()
    mini.add_exception_handler(OperationalError, sqlite_operational_error_handler)

    @mini.get("/boom")
    def boom() -> None:
        raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    client = TestClient(mini, raise_server_exceptions=False)
    res = client.get("/boom")
    assert res.status_code == 503
    assert res.json()["detail"] == "database_busy"
