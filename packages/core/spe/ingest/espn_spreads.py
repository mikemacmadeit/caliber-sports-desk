"""Point spreads from the public ESPN scoreboard.

The paid odds feed stops buying lines once the daily credit reserve is gone.
Saturday's number is already on the scoreboard, so the ATS card can use that
quote instead of publishing a blank board.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from spe.db.base import new_id
from spe.db.models import Bookmaker, Game, OddsTick, Outcome
from spe.ingest.ats_scores import ensure_point_spread_market

log = logging.getLogger(__name__)

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/{league}/scoreboard"
_PATHS = {"nfl": "nfl", "fbs": "college-football"}
_CHICAGO = ZoneInfo("America/Chicago")


def quotes_from_scoreboard(payload: dict[str, Any], league_id: str) -> list[dict[str, Any]]:
    """One home/away pair per event that posts a spread."""
    quotes: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        competitions = event.get("competitions") or []
        if not competitions or not isinstance(competitions[0], dict):
            continue
        odds_list = competitions[0].get("odds") or []
        parsed = _parse_odds(odds_list[0]) if odds_list else None
        event_id = str(event.get("id") or "")
        if parsed is None or not event_id:
            continue
        quotes.append({"game_id": f"espn:{league_id}:{event_id}", **parsed})
    return quotes


def store_public_spreads(
    session: Session,
    *,
    now: datetime | None = None,
    leagues: tuple[str, ...] = ("nfl", "fbs"),
    quotes: list[dict[str, Any]] | None = None,
) -> int:
    """Save scoreboard spreads onto games we already have. Returns ticks written."""
    now = now or datetime.now(UTC)
    ensure_point_spread_market(session)
    if quotes is None:
        quotes = _fetch_quotes(now, leagues)
    if not quotes:
        return 0
    games = {
        game.game_id: game
        for game in session.query(Game).filter(Game.game_id.in_([row["game_id"] for row in quotes])).all()
    }
    written = 0
    for quote in quotes:
        game = games.get(quote["game_id"])
        if game is None:
            continue
        book = str(quote["bookmaker_id"])
        if session.get(Bookmaker, book) is None:
            session.add(Bookmaker(bookmaker_id=book, name=book))
            session.flush()
        for side, line, price in (
            ("home", quote["home_line"], quote["home_price"]),
            ("away", quote["away_line"], quote["away_price"]),
        ):
            written += _save_side(session, game, side, float(line), int(price), book, now)
    session.flush()
    return written


def _fetch_quotes(now: datetime, leagues: tuple[str, ...]) -> list[dict[str, Any]]:
    slate = now.astimezone(_CHICAGO).date()
    dates = [slate, slate + timedelta(days=1)]
    quotes: list[dict[str, Any]] = []
    for league_id in leagues:
        path = _PATHS.get(league_id)
        if path is None:
            continue
        for day in dates:
            url = SCOREBOARD.format(league=path) + f"?dates={day.strftime('%Y%m%d')}&limit=400"
            payload = _get_json(url)
            if payload:
                quotes.extend(quotes_from_scoreboard(payload, league_id))
    return quotes


def _get_json(url: str) -> dict[str, Any] | None:
    request = urllib.request.Request(url, headers={"User-Agent": "caliber-scoreboard/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        log.warning("Scoreboard spreads unavailable: %s", url)
        return None
    return payload if isinstance(payload, dict) else None


def _parse_odds(odds: dict[str, Any]) -> dict[str, Any] | None:
    spread = odds.get("pointSpread") or {}
    home = ((spread.get("home") or {}).get("close") or {})
    away = ((spread.get("away") or {}).get("close") or {})
    home_line = _number(home.get("line"))
    if home_line is None:
        home_line = _number(odds.get("spread"))
    away_line = _number(away.get("line"))
    if home_line is None:
        return None
    if away_line is None:
        away_line = -home_line
    if abs(home_line + away_line) > 0.05:
        return None
    home_price = _american(home.get("odds"))
    away_price = _american(away.get("odds"))
    if home_price is None or away_price is None:
        return None
    provider = odds.get("provider") or {}
    name = str(provider.get("name") or "DraftKings")
    book = "".join(ch for ch in name.lower() if ch.isalnum()) or "draftkings"
    return {
        "home_line": home_line,
        "away_line": away_line,
        "home_price": home_price,
        "away_price": away_price,
        "bookmaker_id": book,
    }


def _number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace("+", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _american(value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    return int(round(number))


def _save_side(
    session: Session,
    game: Game,
    side: str,
    line: float,
    price: int,
    book: str,
    now: datetime,
) -> int:
    outcome = (
        session.query(Outcome)
        .filter(
            Outcome.game_id == game.game_id,
            Outcome.market_id == "point_spread",
            Outcome.side == side,
            Outcome.line == line,
        )
        .one_or_none()
    )
    if outcome is None:
        outcome = Outcome(
            outcome_id=new_id(),
            market_id="point_spread",
            game_id=game.game_id,
            player_id=None,
            side=side,
            line=line,
            description=f"{side} {line:+g}",
        )
        session.add(outcome)
        session.flush()
    latest = (
        session.query(OddsTick)
        .filter(OddsTick.outcome_id == outcome.outcome_id, OddsTick.bookmaker_id == book)
        .order_by(OddsTick.captured_at.desc())
        .first()
    )
    if latest is not None and latest.price_american == price:
        latest.observed_at = now
        latest.captured_at = now
        latest.effective_at = now
        latest.quality_status = "ok"
        return 1
    session.add(
        OddsTick(
            tick_id=new_id(),
            outcome_id=outcome.outcome_id,
            bookmaker_id=book,
            price_american=price,
            captured_at=now,
            source_at=now,
            is_live=False,
            credit_cost=0,
            provider="espn",
            provider_record_id=f"espn:{game.game_id}:{book}:{side}:{line}",
            observed_at=now,
            effective_at=now,
            quality_status="ok",
        )
    )
    return 1
