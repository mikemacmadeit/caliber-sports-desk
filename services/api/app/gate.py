"""Hide pre-kickoff calls from anyone who is not subscribed.

The ledger (finals) stays visible. A future kickoff loses the side, the player,
and the price edge. Ranking leaks are closed by dropping player rows down to one
stub per game and forcing kickoff order.
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
    # The one named scorer is the public claim. The rest of the board stays locked.
    if path == "/v1/ats/track-record" or path.startswith("/v1/anytime-td/calls"):
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
    kick = _parse_time(row.get("kickoff_utc"))
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
        if started(row) or _is_prop(row):
            kept.append(row)
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
