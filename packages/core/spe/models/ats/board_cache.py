"""Serve the last finished ATS board instead of rebuilding it on every page view.

The strength frame takes longer than the website is willing to wait. A saved
board is returned immediately. A stale file is still returned, and one
background rebuild replaces it.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from spe.config import settings

TTL_SEC = 15 * 60
_lock = threading.Lock()
_refreshing = False


def cache_path() -> Path:
    return Path(settings.parquet_dir).parent / "cache" / "ats_board.json"


def read_board(path: Path | None = None) -> dict[str, Any] | None:
    target = path or cache_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or "rows" not in payload:
        return None
    return payload


def write_board(payload: dict[str, Any], path: Path | None = None) -> Path:
    target = path or cache_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body["board_cached_at"] = datetime.now(UTC).isoformat()
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, default=str), encoding="utf-8")
    tmp.replace(target)
    return target


def age_sec(payload: dict[str, Any], now: datetime | None = None) -> float | None:
    raw = payload.get("board_cached_at") or payload.get("as_of")
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    return (now - stamp).total_seconds()


def is_fresh(payload: dict[str, Any], *, ttl_sec: int = TTL_SEC, now: datetime | None = None) -> bool:
    age = age_sec(payload, now)
    return age is not None and age <= ttl_sec


def refresh_board(session: Session, path: Path | None = None) -> dict[str, Any]:
    from spe.models.ats.predict import predict_upcoming_ats

    payload = predict_upcoming_ats(session, include_simulation=False)
    # Never clobber a usable board with an empty rebuild (common on cold cloud DBs).
    existing = read_board(path)
    new_rows = payload.get("rows") or []
    if existing and (existing.get("rows") or []) and not new_rows:
        return existing
    write_board(payload, path)
    return payload


def load_board(session: Session, path: Path | None = None) -> dict[str, Any]:
    """Return a saved board. Rebuild in the background once it is older than the TTL."""
    cached = read_board(path)
    if cached and (cached.get("rows") or []) and is_fresh(cached):
        return cached
    if cached and (cached.get("rows") or []):
        _refresh_in_background(path)
        return cached
    return refresh_board(session, path)


def _refresh_in_background(path: Path | None) -> None:
    global _refreshing
    with _lock:
        if _refreshing:
            return
        _refreshing = True

    def run() -> None:
        global _refreshing
        try:
            from spe.db.session import session_factory

            db = session_factory()
            try:
                refresh_board(db, path)
            finally:
                db.close()
        finally:
            with _lock:
                _refreshing = False

    threading.Thread(target=run, name="ats-board-refresh", daemon=True).start()
