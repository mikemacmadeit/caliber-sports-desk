"""Serve the last finished props board instead of rebuilding it on every page view.

``list_props`` walks outcomes, rosters, forecasts, and ticks. That is fine for a
worker, not for every Anytime TD / Props click. A saved board returns immediately.
A stale entry is still returned while one background rebuild replaces it.
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
_refreshing: set[str] = set()

# Markets the product boards hit most. Worker warm-up covers these.
WARM_MARKETS = ("anytime_td", "rushing_yards", "receiving_yards", "receptions")
WARM_LEAGUES = ("nfl", "fbs")


def cache_path() -> Path:
    return Path(settings.parquet_dir).parent / "cache" / "props_board.json"


def entry_key(market: str | None, league: str | None, date: str | None) -> str:
    return f"{market or 'all'}|{league or 'all'}|{date or 'all'}"


def read_store(path: Path | None = None) -> dict[str, Any]:
    target = path or cache_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"entries": {}}
    if not isinstance(payload, dict):
        return {"entries": {}}
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        payload["entries"] = {}
    return payload


def write_store(payload: dict[str, Any], path: Path | None = None) -> Path:
    target = path or cache_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body["board_cached_at"] = datetime.now(UTC).isoformat()
    if "entries" not in body or not isinstance(body["entries"], dict):
        body["entries"] = {}
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, default=str), encoding="utf-8")
    tmp.replace(target)
    return target


def read_entry(
    market: str | None,
    league: str | None,
    date: str | None,
    path: Path | None = None,
) -> dict[str, Any] | None:
    store = read_store(path)
    entry = (store.get("entries") or {}).get(entry_key(market, league, date))
    if not isinstance(entry, dict) or "rows" not in entry:
        return None
    return entry


def write_entry(
    market: str | None,
    league: str | None,
    date: str | None,
    rows: list[dict[str, Any]],
    path: Path | None = None,
) -> dict[str, Any]:
    store = read_store(path)
    entries = dict(store.get("entries") or {})
    entry = {
        "market": market or "all",
        "league": league or "all",
        "date": date or "all",
        "rows": rows,
        "board_cached_at": datetime.now(UTC).isoformat(),
    }
    entries[entry_key(market, league, date)] = entry
    # Drop very old keys so the file cannot grow without bound.
    if len(entries) > 80:
        ranked = sorted(
            entries.items(),
            key=lambda item: str((item[1] or {}).get("board_cached_at") or ""),
            reverse=True,
        )
        entries = dict(ranked[:60])
    store["entries"] = entries
    write_store(store, path)
    return entry


def age_sec(payload: dict[str, Any], now: datetime | None = None) -> float | None:
    raw = payload.get("board_cached_at")
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


def _filter_rows(
    rows: list[dict[str, Any]],
    *,
    min_odds: int | None,
    min_ev: float | None,
) -> list[dict[str, Any]]:
    out = rows
    if min_odds is not None:
        out = [r for r in out if r.get("book_price") is not None and int(r["book_price"]) >= min_odds]
    if min_ev is not None:
        filtered: list[dict[str, Any]] = []
        for row in out:
            forecast = row.get("forecast") or {}
            ev = forecast.get("ev")
            if ev is not None and float(ev) >= min_ev:
                filtered.append(row)
        out = filtered
    return out


def refresh_entry(
    session: Session,
    market: str | None,
    league: str | None,
    date: str | None,
    path: Path | None = None,
) -> dict[str, Any]:
    from app.queries import list_props

    rows = list_props(session, market, None, None, league, date)
    existing = read_entry(market, league, date, path)
    if existing and (existing.get("rows") or []) and not rows:
        return existing
    return write_entry(market, league, date, rows, path)


def load_props(
    session: Session,
    market: str | None,
    min_odds: int | None,
    min_ev: float | None,
    league: str | None,
    date: str | None,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return saved props for this board. Rebuild in the background when stale."""
    cached = read_entry(market, league, date, path)
    if cached and (cached.get("rows") or []) and is_fresh(cached):
        return _filter_rows(list(cached.get("rows") or []), min_odds=min_odds, min_ev=min_ev)
    if cached and (cached.get("rows") or []):
        _refresh_in_background(market, league, date, path)
        return _filter_rows(list(cached.get("rows") or []), min_odds=min_odds, min_ev=min_ev)
    entry = refresh_entry(session, market, league, date, path)
    return _filter_rows(list(entry.get("rows") or []), min_odds=min_odds, min_ev=min_ev)


def load_slate_props(
    session: Session,
    league: str | None,
    date: str | None,
    *,
    markets: tuple[str, ...] = WARM_MARKETS,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Merge the warmed market boards for picks / tickets. Avoids a full all-market scan."""
    rows: list[dict[str, Any]] = []
    for market in markets:
        rows.extend(load_props(session, market, None, None, league, date, path))
    return rows


def refresh_near_slate(session: Session, path: Path | None = None) -> dict[str, Any]:
    """Warm the boards the website opens first after an odds/forecast refresh."""
    from datetime import timedelta

    from app.queries import games_for_slate, kickoff_day, slate_today

    today = slate_today()
    horizon = (datetime.fromisoformat(today) + timedelta(days=10)).date().isoformat()
    dates = {today}
    for league in WARM_LEAGUES:
        for game in games_for_slate(session, None, league):
            day = kickoff_day(game.kickoff_utc)
            if day and today <= day <= horizon:
                dates.add(day)
    warmed: list[str] = []
    leagues = ("all",) + WARM_LEAGUES
    for day in sorted(dates):
        for league in leagues:
            for market in WARM_MARKETS:
                refresh_entry(session, market, league, day, path)
                warmed.append(entry_key(market, league, day))
    return {"warmed": len(warmed), "keys": warmed[:20], "dates": sorted(dates)}


def _refresh_in_background(
    market: str | None,
    league: str | None,
    date: str | None,
    path: Path | None,
) -> None:
    key = entry_key(market, league, date)
    with _lock:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def run() -> None:
        try:
            from spe.db.session import session_factory

            db = session_factory()
            try:
                refresh_entry(db, market, league, date, path)
            finally:
                db.close()
        finally:
            with _lock:
                _refreshing.discard(key)

    threading.Thread(target=run, name=f"props-board-refresh-{key}", daemon=True).start()
