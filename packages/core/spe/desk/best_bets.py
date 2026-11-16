"""Best-bet shortlists from the saved boards.

Spread rank is the points the projection clears the number. The league-wide
cover rate is not a rank. Price rank is our chance minus the chance in the
posted number, even when that number is old.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from spe.market.odds import american_to_implied
from spe.shadow.atd_desk_takes import SCORE_CHANCE_FLOOR, VALUE_HAIRCUT, select_desk_takes

SLATE = ZoneInfo("America/Chicago")
SPREAD_LIMIT = 6
PRICE_LIMIT = 6
DESK_LIMIT = 6
MIN_EDGE_POINTS = 1.0
PROP_MARKETS = ("receptions", "receiving_yards", "rushing_yards")


def slate_day(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip().replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(SLATE).date().isoformat()


def spread_shortlist(
    rows: list[dict[str, Any]],
    *,
    league: str = "all",
    date: str,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Every spread pick on this day, strongest point edge first."""
    best: dict[str, tuple[float, dict[str, Any]]] = {}
    for row in rows:
        if not _spread_ok(row, league=league, date=date):
            continue
        edge = _spread_edge(row)
        if edge is None or edge < MIN_EDGE_POINTS:
            continue
        key = _matchup_key(row)
        prior = best.get(key)
        if prior is None or edge > prior[0]:
            best[key] = (edge, row)
    ordered = sorted(best.values(), key=lambda item: item[0], reverse=True)
    if limit is not None:
        ordered = ordered[:limit]
    return [_spread_card(row, edge) for edge, row in ordered]


def price_shortlist(
    rows: list[dict[str, Any]],
    *,
    limit: int = PRICE_LIMIT,
    haircut: float = VALUE_HAIRCUT,
) -> list[dict[str, Any]]:
    """One player per game whose chance clears the posted price."""
    best: dict[str, tuple[float, dict[str, Any]]] = {}
    for row in rows:
        gap = _price_gap(row, haircut=haircut)
        if gap is None:
            continue
        game_id = str(row.get("game_id") or "")
        if not game_id:
            continue
        prior = best.get(game_id)
        if prior is None or gap > prior[0]:
            best[game_id] = (gap, row)
    ordered = sorted(best.values(), key=lambda item: item[0], reverse=True)
    return [row for _gap, row in ordered[:limit]]


def desk_shortlist(rows: list[dict[str, Any]], *, limit: int = DESK_LIMIT) -> list[dict[str, Any]]:
    return select_desk_takes(rows)[:limit]


def desk_price_cards(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One counting prop per game, with only the fields the card renders."""
    from spe.props.game_picks import select_desk_props

    return [_price_card(row) for row in select_desk_props(rows)]


def _price_card(row: dict[str, Any]) -> dict[str, Any]:
    forecast = row.get("forecast") if isinstance(row.get("forecast"), dict) else {}
    return {
        "game_id": row.get("game_id"),
        "player_id": row.get("player_id"),
        "player_name": row.get("player_name"),
        "team": row.get("team"),
        "away_abbr": row.get("away_abbr"),
        "home_abbr": row.get("home_abbr"),
        "market_id": row.get("market_id"),
        "book_price": row.get("book_price"),
        "bookmaker": row.get("bookmaker"),
        "kickoff_utc": row.get("kickoff_utc"),
        "game_status": row.get("game_status"),
        "result": row.get("result"),
        "actual": row.get("actual"),
        "pick": row.get("pick"),
        "side": row.get("side"),
        "line": row.get("line"),
        "league_id": row.get("league_id"),
        "forecast": {"p_estimate": forecast.get("p_estimate")},
    }


def prop_desk_picks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every model-side counting prop for the day. Touchdowns stay off this card."""
    picks = [row for row in rows if _prop_side(row)]
    order = {market: index for index, market in enumerate(PROP_MARKETS)}
    picks.sort(key=lambda row: (str(row.get("kickoff_utc") or ""), order.get(str(row.get("market_id")), 9), str(row.get("player_name") or "")))
    return picks


def build_best_bets(*, league: str, date: str, with_prices: bool = True) -> dict[str, Any]:
    from spe.models.ats.board_cache import read_board
    from spe.props.board_cache import read_entry

    board = read_board() or {}
    ats_rows = [row for row in (board.get("rows") or []) if isinstance(row, dict)]
    if league not in {"", "all"}:
        ats_rows = [row for row in ats_rows if row.get("league_id") == league]
    spreads = spread_shortlist(ats_rows, league=league, date=date)

    prices: list[dict[str, Any]] = []
    if with_prices:
        league_key = league if league not in {"", "all"} else "all"
        prop_rows: list[dict[str, Any]] = []
        for market in PROP_MARKETS:
            entry = read_entry(market, league_key, date) or {}
            prop_rows.extend(row for row in (entry.get("rows") or []) if isinstance(row, dict))
        if league not in {"", "all"}:
            prop_rows = [row for row in prop_rows if row.get("league_id") == league]
        prices = desk_price_cards(prop_rows)
    return {
        "date": date,
        "league": league or "all",
        "spreads": spreads,
        "prices": prices,
        "scorers": [],
    }


def _spread_ok(row: dict[str, Any], *, league: str, date: str) -> bool:
    if league not in {"", "all"} and row.get("league_id") != league:
        return False
    if slate_day(row.get("kickoff_utc")) != date:
        return False
    status = str(row.get("game_status") or "").lower()
    if status in {"canceled", "cancelled"}:
        return False
    if row.get("projection_withheld") or row.get("pick_market") == "withheld":
        return False
    return bool(row.get("ats_pick_label"))


def _spread_edge(row: dict[str, Any]) -> float | None:
    for key in ("ats_cover_margin", "pick_edge_points"):
        value = row.get(key)
        if isinstance(value, (int, float)):
            return abs(float(value))
    return None


def _matchup_key(row: dict[str, Any]) -> str:
    clubs = tuple(sorted(str(row.get(name) or "") for name in ("home_team_id", "away_team_id")))
    if clubs != ("", ""):
        return "|".join(clubs)
    return str(row.get("game_id") or "")


def _spread_card(row: dict[str, Any], edge: float) -> dict[str, Any]:
    from spe.models.ats.predict import grade_displayed_pick

    status = str(row.get("game_status") or "").lower() or None
    result = row.get("ats_pick_result")
    if result is None:
        result = grade_displayed_pick(
            status=status,
            home_score=row.get("home_score"),
            away_score=row.get("away_score"),
            pick_market="spread",
            favored_side=row.get("ats_side"),
            home_handicap=row.get("home_handicap"),
        )
    return {
        "game_id": row.get("game_id"),
        "league_id": row.get("league_id"),
        "kickoff_utc": row.get("kickoff_utc"),
        "game_status": status,
        "home_score": row.get("home_score"),
        "away_score": row.get("away_score"),
        "home_team_id": row.get("home_team_id"),
        "away_team_id": row.get("away_team_id"),
        "away_team": row.get("away_team"),
        "home_team": row.get("home_team"),
        "away_abbr": row.get("away_abbr"),
        "home_abbr": row.get("home_abbr"),
        "away_mark": row.get("away_mark"),
        "home_mark": row.get("home_mark"),
        "ats_pick_label": row.get("ats_pick_label"),
        "ats_team": row.get("ats_team"),
        "ats_line": row.get("ats_line"),
        "ats_side": row.get("ats_side"),
        "home_handicap": row.get("home_handicap"),
        "pick_market": "spread",
        "ats_pick_result": result,
        "edge_points": round(edge, 1),
        "book_price": _side_price(row),
    }


def _side_price(row: dict[str, Any]) -> int | None:
    """The posted juice on the side we named. A missing price stays missing."""
    raw = row.get("away_spread_price") if row.get("ats_side") == "away" else row.get("home_spread_price")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if isinstance(raw, float) and raw != raw:
        return None
    price = int(raw)
    if abs(price) < 100:
        return None
    return price


def _prop_side(row: dict[str, Any]) -> bool:
    market = str(row.get("market_id") or "")
    if market not in PROP_MARKETS or row.get("model_lean") is not True:
        return False
    if row.get("line") is None or not row.get("side"):
        return False
    forecast = row.get("forecast") or {}
    return str(forecast.get("availability_status") or "").lower() not in {"out", "doubtful"}


def _price_gap(row: dict[str, Any], *, haircut: float) -> float | None:
    forecast = row.get("forecast") or {}
    status = str(forecast.get("availability_status") or "").lower()
    if status in {"out", "doubtful"}:
        return None
    chance = forecast.get("p_estimate")
    price = row.get("book_price")
    if not isinstance(chance, (int, float)) or not isinstance(price, (int, float)):
        return None
    if float(chance) < SCORE_CHANCE_FLOOR:
        return None
    if int(price) > 500:
        return None
    try:
        implied = american_to_implied(int(price))
    except (TypeError, ValueError):
        return None
    gap = float(chance) - implied
    if gap < haircut:
        return None
    return gap
