"""One prop call per game and market, from lines that already have a fresh price.

A chance several times the book's number stays off. A soft line in the 80s or 90s
stays off. Only verified prices with sufficient conservative value qualify.
An empty card is a valid outcome.
"""

from __future__ import annotations

import math
from typing import Any
from datetime import datetime
from spe.publishing.policy import assess

from spe.market.odds import american_to_implied
from spe.shadow.atd_desk_takes import desk_pick_score

MARKETS = ("anytime_td", "rushing_yards", "receiving_yards", "receptions")
MARKET_LABEL = {
    "anytime_td": "anytime touchdown",
    "rushing_yards": "rushing yards",
    "receiving_yards": "receiving yards",
    "receptions": "receptions",
}
MAX_PRICE_GAP = 2.75
ANYTIME_MIN_P = 0.18
ANYTIME_MAX_P = 0.75
COUNT_MIN_P = 0.42
COUNT_MAX_P = 0.66
TARGET_P = 0.58


def build_game_prop_picks(rows: list[dict[str, Any]], game_ids: set[str] | None = None, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Return the published prop calls. ``game_ids`` limits the slate to games still open."""
    allowed = None if game_ids is None else {str(gid) for gid in game_ids}
    pools: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        if not _publishable(row) or not assess(row, now)["eligible"]:
            continue
        game_id = str(row.get("game_id") or "")
        if allowed is not None and game_id not in allowed:
            continue
        key = (game_id, str(row.get("market_id")), str(row.get("player_id")))
        pools.setdefault(key, []).append(row)

    by_market: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for (game_id, market_id, _player_id), group in pools.items():
        chosen = min(group, key=_line_rank)
        by_market.setdefault((game_id, market_id), []).append(chosen)

    picks: list[dict[str, Any]] = []
    for (game_id, market_id), group in by_market.items():
        winner = max(group, key=lambda row: (desk_pick_score(row)["total"], str(row.get("player_name") or "")))
        pick = _public_pick(winner, price_ahead=False, stale=False)
        pick.update(outcome_id=winner.get("outcome_id"), player_id=winner.get("player_id"),
                    bookmaker=winner.get("bookmaker"), qualification=assess(winner, now),
                    forecast=winner.get("forecast"), price_freshness=winner.get("price_freshness"),
                    data_tier=winner.get("data_tier"))
        picks.append(pick)

    picks.sort(key=lambda pick: (str(pick.get("kickoff_utc") or ""), str(pick.get("game_id")), MARKETS.index(pick["market_id"])))
    return picks


def _publishable(row: dict[str, Any]) -> bool:
    market = str(row.get("market_id") or "")
    if market not in MARKETS or not row.get("player_id"):
        return False
    if market == "anytime_td" and str(row.get("side") or "yes") not in {"yes", "over"}:
        return False
    if row.get("book_price") is None or _trivial_floor(row):
        return False
    forecast = row.get("forecast") or {}
    if str(forecast.get("availability_status") or "").lower() in {"out", "doubtful"}:
        return False
    p = _p(row)
    if p is None:
        return False
    if market == "anytime_td":
        if not ANYTIME_MIN_P <= p <= ANYTIME_MAX_P:
            return False
    elif not COUNT_MIN_P <= p <= COUNT_MAX_P:
        return False
    implied = american_to_implied(int(row["book_price"]))
    return implied > 0 and p <= implied * MAX_PRICE_GAP


def _public_pick(row: dict[str, Any], *, price_ahead: bool, stale: bool) -> dict[str, Any]:
    p = float(_p(row) or 0)
    loud = (not price_ahead) and (not stale) and p >= 0.55
    return {
        "game_id": row.get("game_id"),
        "league_id": row.get("league_id"),
        "away_abbr": row.get("away_abbr"),
        "home_abbr": row.get("home_abbr"),
        "kickoff_utc": _iso(row.get("kickoff_utc")),
        "market_id": row.get("market_id"),
        "player_name": row.get("player_name"),
        "team": row.get("team"),
        "side": row.get("side"),
        "line": row.get("line"),
        "p": p,
        "book_price": int(row["book_price"]),
        "loud": loud,
        "price_ahead": price_ahead,
        "stale": stale,
        "sentence": _sentence(row, price_ahead=price_ahead, stale=stale),
        "research_only": True,
    }


def _fresh(row: dict[str, Any]) -> bool:
    freshness = row.get("price_freshness") or (row.get("forecast") or {}).get("price_freshness") or {}
    return freshness.get("status") == "fresh" and bool(freshness.get("is_current_opportunity"))


def _sentence(row: dict[str, Any], *, price_ahead: bool, stale: bool) -> str:
    p = float(_p(row) or 0)
    times = _times(p)
    price = _american(int(row["book_price"]))
    market = str(row.get("market_id"))
    name = str(row.get("player_name") or "Player")
    if market == "anytime_td":
        text = f"{name} anytime touchdown, {times}, pays {price}."
    else:
        side = "under" if str(row.get("side") or "") == "under" else "over"
        line = _line(row.get("line"))
        label = MARKET_LABEL[market]
        text = f"{name} {side} {line} {label}, {times}, pays {price}."
    notes: list[str] = []
    if stale:
        notes.append("The last price is stale.")
    if price_ahead:
        notes.append("The price is ahead of this chance.")
    if not notes:
        return text
    return text[:-1] + ". " + " ".join(notes)


def _line_rank(row: dict[str, Any]) -> tuple[float, float]:
    p = _p(row) or 0.0
    return (abs(p - TARGET_P), -_haircut(row))


def _haircut(row: dict[str, Any]) -> float:
    forecast = row.get("forecast") or {}
    value = forecast.get("ev_after_haircut")
    if value is None:
        value = forecast.get("ev")
    if value is None:
        return -1.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return -1.0
    if math.isnan(number):
        return -1.0
    return number


def _p(row: dict[str, Any]) -> float | None:
    forecast = row.get("forecast") or {}
    value = forecast.get("p_estimate")
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def _trivial_floor(row: dict[str, Any]) -> bool:
    if row.get("market_id") != "receptions" or row.get("line") is None:
        return False
    number = float(row["line"])
    side = str(row.get("side") or "")
    if side == "over" and number < 1.5:
        return True
    return side == "under" and number <= 1.5


def _times(p: float) -> str:
    n = int(round(p * 100))
    n = max(0, min(100, n))
    if n <= 0:
        return "almost never in 100"
    if n >= 100:
        return "almost always"
    return f"about {n} times in 100"


def _american(price: int) -> str:
    return f"+{price}" if price > 0 else str(price)


def _line(value: Any) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}"


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
