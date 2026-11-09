"""GDELT headlines with the time GDELT first saw them.

Articles are never written as injury events. A missing article is not a healthy player.
"""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from spe.adapters.base import AdapterError
from spe.adapters.gdelt import GdeltAdapter
from spe.config import settings
from spe.db.models import InjuryEvent, NewsArticle
from spe.ingest.store import upsert_coverage

PROVIDER = "gdelt"
THEMES = {
    "injury_news": '(injury OR injured OR "ruled out" OR questionable OR concussion)',
    "personnel_news": '(waived OR "practice squad" OR activated OR "injured reserve")',
    "coaching_news": '(coach OR coordinator) (hired OR fired OR named)',
    "team_announcement": '("named starter" OR "announced starter" OR "starting quarterback")',
}


def theme_query(league_id: str, theme: str) -> str:
    sport = '"NFL"' if league_id == "nfl" else '"college football"'
    return f"{sport} {THEMES[theme]} sourcelang:english"


def parse_seen(value: Any) -> datetime | None:
    text = str(value or "").strip().replace("Z", "").replace("T", "")
    if len(text) < 14 or not text[:14].isdigit():
        return None
    try:
        return datetime.strptime(text[:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def ingest_news_records(
    session: Session,
    records: list[dict[str, Any]],
    *,
    league_id: str,
    theme: str,
    query: str,
    observed_at: datetime | None = None,
) -> dict[str, int]:
    observed = observed_at or datetime.now(UTC)
    existing = {row[0] for row in session.query(NewsArticle.provider_record_id).filter(NewsArticle.provider == PROVIDER)}
    rows = []
    skipped = 0
    for article in records:
        url = str(article.get("url") or "").strip()
        seen = parse_seen(article.get("seendate"))
        if not url or seen is None:
            skipped += 1
            continue
        record_id = hashlib.sha1(url.encode("utf-8")).hexdigest()[:32]
        if record_id in existing:
            skipped += 1
            continue
        existing.add(record_id)
        rows.append(
            {
                "article_id": str(uuid4()),
                "url": url,
                "title": str(article.get("title") or "")[:2000],
                "domain": (str(article.get("domain"))[:128] if article.get("domain") else None),
                "language": (str(article.get("language"))[:16] if article.get("language") else None),
                "source_country": (str(article.get("sourcecountry"))[:8] if article.get("sourcecountry") else None),
                "theme": theme,
                "league_id": league_id,
                "seen_at": seen,
                "query": query,
                "verified_injury": False,
                "provider": PROVIDER,
                "provider_record_id": record_id,
                "observed_at": observed,
                "effective_at": seen,
                "ingested_at": observed,
                "revision": 1,
                "quality_status": "unverified_news",
            }
        )
    if rows:
        session.execute(NewsArticle.__table__.insert(), rows)
        session.flush()
    return {"rows": len(rows), "skipped": skipped, "injury_events": session.query(InjuryEvent).count()}


def ingest_gdelt_news(
    session: Session,
    *,
    adapter: GdeltAdapter | None = None,
    now: datetime | None = None,
    pause_s: float = 8.0,
) -> dict[str, Any]:
    moment = now or datetime.now(UTC)
    start = moment - timedelta(days=max(1, settings.gdelt_lookback_days))
    start_s = start.strftime("%Y%m%d%H%M%S")
    end_s = moment.strftime("%Y%m%d%H%M%S")
    client = adapter or GdeltAdapter()
    stored = 0
    errors: list[str] = []
    calls = 0
    for league in ("nfl", "fbs"):
        for theme in THEMES:
            query = theme_query(league, theme)
            calls += 1
            try:
                result = client.artlist(query, start=start_s, end=end_s)
            except AdapterError as exc:
                if "429" not in str(exc):
                    errors.append(f"{league}:{theme}: {exc}")
                    result = None
                else:
                    time.sleep(pause_s)
                    try:
                        result = client.artlist(query, start=start_s, end=end_s)
                    except AdapterError as retry_exc:
                        errors.append(f"{league}:{theme}: {retry_exc}")
                        result = None
            if result is not None:
                outcome = ingest_news_records(
                    session,
                    result.records,
                    league_id=league,
                    theme=theme,
                    query=query,
                    observed_at=moment,
                )
                stored += outcome["rows"]
            if pause_s and calls < len(THEMES) * 2:
                time.sleep(pause_s)
    earliest = session.query(NewsArticle.seen_at).order_by(NewsArticle.seen_at.asc()).first()
    latest = session.query(NewsArticle.seen_at).order_by(NewsArticle.seen_at.desc()).first()
    notes = (
        f"DOC 2.0 artlist, max 75 articles per query, window {start.date().isoformat()} to {moment.date().isoformat()}. "
        "seendate is the availability time. verified_injury is always false. "
        "Articles are not injury designations and are not linked to players. "
        f"errors={len(errors)}."
    )
    for league in ("nfl", "fbs"):
        count = session.query(NewsArticle).filter(NewsArticle.league_id == league, NewsArticle.provider == PROVIDER).count()
        upsert_coverage(
            session,
            provider=PROVIDER,
            dataset="news",
            league_id=league,
            season_year=0,
            row_count=count,
            status="partial_window" if count else "empty",
            notes=notes + (f" earliest={earliest[0].isoformat() if earliest else None} latest={latest[0].isoformat() if latest else None}"),
        )
    session.flush()
    return {"status": "partial_window", "rows": stored, "errors": errors, "window_start": start.isoformat()}
