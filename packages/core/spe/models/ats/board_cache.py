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


def read_board(path: Path | None = None, *, session: Session | None = None) -> dict[str, Any] | None:
    if path is None:
        from spe.publishing.store import read
        return read("ats", db=session)
    target = path or cache_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or "rows" not in payload:
        return None
    return payload


def write_board(payload: dict[str, Any], path: Path | None = None) -> Path:
    if path is None:
        from spe.publishing.store import write
        write("ats", payload)
        return cache_path()
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


_LOCKED_SPREAD_KEYS = (
    "ats_side",
    "ats_team",
    "ats_line",
    "ats_pick_label",
    "ats_cover_margin",
    "ats_probability",
    "ats_basis",
    "ats_locked",
)


def _started(status: object) -> bool:
    return str(status or "").lower() in {"final", "in_progress"}


def keep_finished_spreads(previous: dict[str, Any] | None, payload: dict[str, Any]) -> dict[str, Any]:
    """A rebuild can change the margin. It cannot take a started game's spread off the card."""
    from spe.models.ats.predict import grade_displayed_pick, lays_points_with_fewer_wins

    if not previous:
        return payload
    saved: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {}
    for row in previous.get("rows") or []:
        if row.get("ats_side") not in {"home", "away"} or not row.get("ats_pick_label"):
            continue
        kept = {key: row.get(key) for key in _LOCKED_SPREAD_KEYS}
        kept["ats_locked"] = True
        saved[str(row.get("game_id"))] = (str(row.get("game_status") or ""), kept, row)
    for row in payload.get("rows") or []:
        prior = saved.get(str(row.get("game_id")))
        if prior is None:
            continue
        previous_status, kept, previous_row = prior
        if not (_started(previous_status) or _started(row.get("game_status"))):
            continue
        if (
            previous_row.get("ats_basis") == "number"
            and lays_points_with_fewer_wins(previous_row)
            and row.get("ats_side") in {"home", "away"}
            and row.get("ats_pick_label")
        ):
            if str(previous_status).lower() == "final":
                row["game_status"] = "final"
            row["ats_pick_result"] = grade_displayed_pick(
                status=row.get("game_status"),
                home_score=row.get("home_score"),
                away_score=row.get("away_score"),
                pick_market="spread",
                favored_side=row.get("ats_side"),
                home_handicap=row.get("home_handicap"),
            )
            continue
        row.update(kept)
        if str(previous_status).lower() == "final":
            row["game_status"] = "final"
        line = row.get("ats_line")
        side = row.get("ats_side")
        if line is not None and side in {"home", "away"}:
            row["home_handicap"] = float(line) if side == "home" else -float(line)
        row["ats_pick_result"] = grade_displayed_pick(
            status=row.get("game_status"),
            home_score=row.get("home_score"),
            away_score=row.get("away_score"),
            pick_market="spread",
            favored_side=side,
            home_handicap=row.get("home_handicap"),
        )
    return payload


def refresh_board(session: Session, path: Path | None = None) -> dict[str, Any]:
    from spe.models.ats.predict import predict_upcoming_ats

    previous = read_board(path, session=session)
    payload = predict_upcoming_ats(session, include_simulation=False)
    if not (payload.get("rows") or []) and previous and (previous.get("rows") or []):
        return previous
    payload = keep_finished_spreads(previous, payload)
    write_board(payload, path)
    if path is None:
        archive_days(session, previous or {})
        archive_days(session, payload)
    return payload


def load_board(session: Session, path: Path | None = None, *, date: str | None = None) -> dict[str, Any]:
    """Return a saved board. Rebuild in the background once it is older than the TTL."""
    if date is not None and path is None:
        from spe.publishing.store import read
        archived = read(f"ats-day:{date}", session)
        if archived is not None:
            return {**archived, "archive_note": "Saved ATS selections. Results update from confirmed scores; the saved side and line stay fixed after kickoff."}
    cached = read_board(path, session=session)
    if cached and (cached.get("rows") or []) and is_fresh(cached):
        return cached
    if cached and (cached.get("rows") or []):
        if settings.app_env not in {"prod", "production"}:
            _refresh_in_background(path)
        return cached
    if settings.app_env in {"prod", "production"}:
        return {"rows": [], "degraded": True, "reason": "awaiting_worker"}
    return refresh_board(session, path)


def archive_days(session, payload, now=None):
    """Keep daily selections when a later board no longer contains their games."""
    from collections import defaultdict
    from spe.db.base import as_utc
    from spe.publishing.store import read, write
    from app.queries import kickoff_day
    now = as_utc(now) or datetime.now(UTC)
    days = defaultdict(list)
    for row in payload.get("rows") or []:
        if row.get("kickoff_utc"):
            days[kickoff_day(row["kickoff_utc"])].append(row)
    for day, rows in days.items():
        key = f"ats-day:{day}"
        saved = read(key, session) or {}
        previous = {r["game_id"]: r for r in saved.get("rows", [])}
        for row in rows:
            old = previous.get(row["game_id"])
            if old and as_utc(old["kickoff_utc"]) <= now:
                continue
            stamp = as_utc(payload.get("board_cached_at") or payload.get("as_of"))
            previous[row["game_id"]] = {**row, "saved_at": stamp.isoformat() if stamp else None,
                "record_status": "saved_pregame" if stamp and stamp < as_utc(row["kickoff_utc"]) else "archived_unverified"}
        write(key, {**payload, "date": day, "rows": list(previous.values())}, session)
        # Subsequent archive writes in this transaction must see the current JSON.
        session.expire_all()


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
                db.commit()
            finally:
                db.close()
        finally:
            with _lock:
                _refreshing = False

    threading.Thread(target=run, name="ats-board-refresh", daemon=True).start()
