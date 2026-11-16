"""Hide pre-kickoff calls from anyone who is not subscribed.

The ledger of finished games stays visible. A future kickoff loses the side,
the player, and the price. The schedule and the matchup stay.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from spe.db.base import as_utc

_STARTED = {"final", "in_progress", "complete", "closed"}
_OPEN_SETTLEMENTS = {"", "pending", "open"}

_ATS_HIDE = {
    "pick_label",
    "pick_team",
    "pick_line",
    "pick_market",
    "pick_probability",
    "pick_chance_label",
    "pick_edge_points",
    "pick_result",
    "pick_price",
    "ats_pick_label",
    "ats_side",
    "ats_team",
    "ats_line",
    "ats_cover_margin",
    "ats_pick_result",
    "ats_price",
    "p_cover",
    "p_home",
    "p_away",
    "favored_side",
    "model_vs_market",
    "expected_value",
    "predicted_margin",
    "predicted_home_score",
    "predicted_away_score",
    "projection",
    "cover_probability",
    "home_cover_prob",
    "away_cover_prob",
    "win_probability",
    "home_win_prob",
    "away_win_prob",
}


def should_redact(path: str) -> bool:
    # The settled ATS ledger stays public. A call that has not kicked off does not.
    if path == "/v1/ats/track-record":
        return False
    prefixes = (
        "/v1/ats",
        "/v1/props",
        "/v1/anytime-td",
        "/v1/parlays",
        "/v1/games/",
        "/v1/slate",
        "/v1/shadow/track-record",
        "/v1/recommendations/history",
        "/v1/desk",
        "/v1/best-bets",
        "/v1/published",
    )
    return any(path == prefix or path.startswith(prefix) for prefix in prefixes)


def subscription_required(method: str, path: str) -> bool:
    """Tools that answer a fresh question about a game still to be played."""
    if method == "POST" and path in {"/v1/simulations", "/v1/parlays/evaluate"}:
        return True
    if method == "GET" and path.startswith("/v1/simulations/"):
        return True
    return method == "GET" and path.startswith("/v1/players/") and path.endswith("/forecast")


def board_query_for(sort: str, side: str, *, subscribed: bool) -> tuple[str, str]:
    """Free viewers do not get a side filter or a confidence sort. Both encode the call."""
    if subscribed:
        return sort, side
    return "kickoff", "all"


def redact(path: str, payload: dict[str, Any], *, subscribed: bool) -> dict[str, Any]:
    if subscribed:
        return payload
    data = copy.deepcopy(payload)
    data["access"] = "free"
    if "/research" in path:
        return _lock_research(data)
    if path.startswith("/v1/shadow/track-record"):
        _strip_open_takes(data)
        return data
    if path.startswith("/v1/recommendations"):
        _lock_open_recs(data)
        return data
    if path.startswith("/v1/games/"):
        return _lock_game_detail(data)
    if path.startswith("/v1/slate"):
        for game in data.get("games") or []:
            if isinstance(game, dict) and not started(game):
                game["research"] = None
                game["locked"] = True
        return data
    if path.startswith("/v1/props/picks") and isinstance(data.get("picks"), list):
        data["picks"] = [_lock_pick(row) if isinstance(row, dict) and not started(row) else row for row in data["picks"]]
        if isinstance(data.get("day"), list):
            data["day"] = [_lock_pick(row) if isinstance(row, dict) and not started(row) else row for row in data["day"]]
        if any(isinstance(row, dict) and row.get("locked") for row in (data["picks"] + (data.get("day") or []))):
            data["locked"] = True
        return data
    if path.startswith("/v1/props/desk"):
        return _drop_unstarted_list(data, "calls")
    if path.startswith("/v1/desk"):
        return _lock_home_desk(data)
    if path.startswith("/v1/best-bets"):
        _drop_unstarted_list(data, "spreads")
        _drop_unstarted_list(data, "prices")
        return data
    if path.startswith("/v1/parlays"):
        return _lock_parlays(data)
    if path.startswith("/v1/props") and isinstance(data.get("rows"), list):
        return _drop_unstarted_list(data, "rows")
    if path.startswith("/v1/published") and isinstance(data.get("rows"), list):
        return _drop_unstarted_list(data, "rows")
    if isinstance(data.get("rows"), list):
        data["rows"] = _lock_rows(data["rows"])
        if any(isinstance(row, dict) and row.get("locked") for row in data["rows"]):
            data["locked"] = True
        return data
    return data


def started(row: dict[str, Any], *, now: datetime | None = None) -> bool:
    status = str(row.get("game_status") or row.get("status") or "").lower()
    if status in _STARTED:
        return True
    kick = _parse_time(row.get("kickoff_utc") or row.get("kickoff"))
    if kick is None:
        game = row.get("game")
        if isinstance(game, dict):
            return started(game, now=now)
        return False
    return kick <= (now or datetime.now(UTC))


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return as_utc(value)
    if isinstance(value, str) and value:
        try:
            return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _drop_unstarted_list(payload: dict[str, Any], key: str) -> dict[str, Any]:
    """Remove a call that has not kicked off. Graded rows stay."""
    rows = payload.get(key)
    if not isinstance(rows, list):
        return payload
    kept: list[Any] = []
    locked = False
    for row in rows:
        if isinstance(row, dict) and not started(row):
            locked = True
            continue
        kept.append(row)
    payload[key] = kept
    if locked:
        payload["locked"] = True
        if isinstance(payload.get("summary"), dict):
            payload["summary"]["pending"] = sum(
                1 for row in kept if isinstance(row, dict) and row.get("result") not in {"win", "loss", "push", "correct", "miss"}
            )
    return payload


def _lock_home_desk(payload: dict[str, Any]) -> dict[str, Any]:
    """The home board is only pre-kickoff research. The schedule stays."""
    upcoming = payload.get("games") or []
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    if not upcoming and not rows:
        return payload
    payload["locked"] = True
    payload["rows"] = []
    payload["total"] = 0
    payload["qualified"] = 0
    payload["changed"] = 0
    payload["blockers"] = []
    payload["books"] = []
    payload["market_counts"] = {}
    return payload


def _lock_parlays(payload: dict[str, Any]) -> dict[str, Any]:
    tickets = payload.get("tickets")
    if not isinstance(tickets, list):
        return payload
    kept: list[Any] = []
    locked = False
    for ticket in tickets:
        if not isinstance(ticket, dict):
            kept.append(ticket)
            continue
        legs = ticket.get("legs") if isinstance(ticket.get("legs"), list) else []
        if not legs or any(isinstance(leg, dict) and not started(leg) for leg in legs):
            locked = True
            continue
        kept.append(ticket)
    payload["tickets"] = kept
    if locked:
        payload["locked"] = True
        payload["eligible_legs"] = sum(len(ticket.get("legs") or []) for ticket in kept if isinstance(ticket, dict))
    return payload


def _is_prop(row: dict[str, Any]) -> bool:
    if row.get("locked"):
        return True
    return "player_name" in row or "forecast" in row or ("sentence" in row and "market_id" in row)


def _lock_rows(rows: list[Any]) -> list[Any]:
    kept: list[Any] = []
    for row in rows:
        if not isinstance(row, dict):
            kept.append(row)
            continue
        if started(row):
            kept.append(row)
            continue
        if _is_prop(row):
            kept.append(_lock_prop(row))
            continue
        kept.append(_lock_ats(row))
    kept.sort(key=lambda row: str(row.get("kickoff_utc") or "") if isinstance(row, dict) else "")
    return kept


def _model_opinion(key: str) -> bool:
    if key in _ATS_HIDE or key in {"factors", "margin_sigma", "ats_probability", "confidence"}:
        return True
    return key.startswith("p_")


def _lock_ats(row: dict[str, Any]) -> dict[str, Any]:
    for key in list(row):
        if _model_opinion(key):
            row[key] = None
    row["locked"] = True
    return row


def _lock_prop(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "locked": True,
        "game_id": row.get("game_id"),
        "league_id": row.get("league_id") or row.get("league"),
        "away_abbr": row.get("away_abbr"),
        "home_abbr": row.get("home_abbr"),
        "kickoff_utc": row.get("kickoff_utc") or row.get("kickoff"),
        "market_id": row.get("market_id") or row.get("market"),
        "game_status": row.get("game_status") or row.get("status"),
    }


def _lock_pick(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "locked": True,
        "game_id": row.get("game_id"),
        "league_id": row.get("league_id"),
        "away_abbr": row.get("away_abbr"),
        "home_abbr": row.get("home_abbr"),
        "kickoff_utc": row.get("kickoff_utc"),
        "market_id": row.get("market_id"),
        "player_name": "",
        "sentence": "",
        "p": None,
        "book_price": None,
        "loud": False,
        "price_ahead": False,
    }


def _lock_game_detail(payload: dict[str, Any]) -> dict[str, Any]:
    game = payload.get("game") if isinstance(payload.get("game"), dict) else {}
    if started(game):
        return payload
    payload["locked"] = True
    payload["props"] = []
    payload["model_disagreement"] = []
    payload["research"] = None
    payload["key_features"] = []
    game["research"] = None
    game["locked"] = True
    return payload


def _lock_research(payload: dict[str, Any]) -> dict[str, Any]:
    prediction = payload.get("prediction")
    if isinstance(prediction, dict) and started(prediction):
        return payload
    games = payload.get("games")
    if isinstance(games, list) and games and all(isinstance(game, dict) and started(game) for game in games):
        return payload
    return {
        "locked": True,
        "access": "free",
        "games": [],
        "disclaimer": payload.get("disclaimer"),
    }


def _lock_open_recs(payload: dict[str, Any]) -> None:
    for row in payload.get("rows") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("settlement") or "").lower() in _OPEN_SETTLEMENTS:
            row["player"] = None
            row["p"] = None
            row["ev"] = None
            row["ev_after_haircut"] = None
            row["locked"] = True


def _strip_open_takes(payload: dict[str, Any]) -> None:
    if "open_and_settled_takes" in payload:
        payload["open_and_settled_takes"] = _graded_only(payload.get("open_and_settled_takes"))
    card = payload.get("atd_scorecard")
    if isinstance(card, dict) and "open_and_settled_takes" in card:
        card["open_and_settled_takes"] = _graded_only(card.get("open_and_settled_takes"))


def _graded_only(takes: Any) -> list[Any]:
    if not isinstance(takes, list):
        return []
    kept = []
    for take in takes:
        if not isinstance(take, dict):
            continue
        if str(take.get("settlement") or "").lower() in _OPEN_SETTLEMENTS:
            continue
        kept.append(take)
    return kept
