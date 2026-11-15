"""Grade the day's shown picks from the recorded game and box score.

A final game with no listed stat stays pending. A missing box is not a zero.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from spe.db.models import Game, PublishedStat
from spe.identity.match import match_name
from spe.models.ats.predict import grade_displayed_pick
from spe.shadow.settle import _grade

COUNTING = ("receptions", "rushing_yards", "receiving_yards")
_CHECKED: set[str] = set()


def apply_recorded_results(
    session: Session,
    spreads: list[dict[str, Any]],
    prices: list[dict[str, Any]],
    *,
    fetch=None,
) -> None:
    game_ids = {row.get("game_id") for row in [*spreads, *prices] if row.get("game_id")}
    if not game_ids:
        return
    games = {
        game.game_id: game
        for game in session.scalars(select(Game).where(Game.game_id.in_(game_ids)))
    }
    for row in spreads:
        _grade_spread(row, games.get(row.get("game_id")))
    _fill_counting_stats(session, games, prices, fetch=fetch)
    stats = {
        (stat.game_id, stat.player_id, stat.market_id): stat.actual
        for stat in session.scalars(
            select(PublishedStat).where(
                PublishedStat.game_id.in_(game_ids),
                PublishedStat.market_id.in_(COUNTING),
            )
        )
    }
    for row in prices:
        game = games.get(row.get("game_id"))
        status = None if game is None else str(game.status or "").lower()
        row["game_status"] = status
        actual = stats.get((row.get("game_id"), row.get("player_id"), row.get("market_id")))
        if actual is None or status not in {"final", "post", "in_progress"}:
            row["result"] = "pending"
            continue
        row["actual"] = actual
        row["result"] = _settled_result(str(row.get("side") or "over"), row.get("line"), float(actual), str(row.get("market_id")), status)


def _grade_spread(row: dict[str, Any], game: Game | None) -> None:
    if game is None:
        return
    status = str(game.status or "").lower() or None
    row["game_status"] = status
    row["home_score"] = game.home_score
    row["away_score"] = game.away_score
    graded = grade_displayed_pick(
        status=status,
        home_score=game.home_score,
        away_score=game.away_score,
        pick_market="spread",
        favored_side=row.get("ats_side"),
        home_handicap=row.get("home_handicap"),
    )
    if graded is not None:
        row["ats_pick_result"] = graded


def _settled_result(side: str, line: float | None, actual: float, market_id: str, status: str) -> str:
    """Final games grade normally. A live line grades only when the stat can no longer reverse."""
    if status in {"final", "post"}:
        return _grade(side, line, actual, market_id)
    if line is None:
        return "pending"
    if abs(actual - float(line)) < 1e-9:
        return "pending"
    cleared = actual > float(line)
    if side == "over" and cleared:
        return "win"
    if side == "under" and cleared:
        return "loss"
    return "pending"


def _fill_counting_stats(
    session: Session,
    games: dict[str, Game],
    rows: list[dict[str, Any]],
    *,
    fetch,
) -> None:
    catalog = _catalog(rows)
    finals = [
        game
        for game in games.values()
        if str(game.status).lower() in {"final", "post"}
        and game.game_id in catalog
        and game.game_id not in _CHECKED
    ]
    live = [
        game
        for game in games.values()
        if str(game.status).lower() == "in_progress" and game.game_id in catalog
    ]
    if not finals and not live:
        return
    have = set(
        session.scalars(
            select(PublishedStat.game_id).where(
                PublishedStat.game_id.in_([game.game_id for game in finals]),
                PublishedStat.market_id.in_(COUNTING),
                PublishedStat.source == "espn.summary",
            )
        )
    ) if finals else set()
    needed = [game for game in finals if game.game_id not in have]
    _CHECKED.update(game.game_id for game in finals if game.game_id in have)
    needed.extend(live)
    if not needed:
        return
    from spe.ingest.espn_box import _fetch_summary
    from spe.publishing.boxes import parse

    loader = fetch or _fetch_summary
    live_ids = {game.game_id for game in live}

    def load(game: Game) -> tuple[str, dict[str, Any] | None]:
        event_id = game.game_id.split(":")[-1] if game.game_id.startswith("espn:") else None
        if not event_id:
            return game.game_id, None
        payload = loader(game.league_id, event_id)
        return game.game_id, payload if isinstance(payload, dict) else None

    with ThreadPoolExecutor(max_workers=8) as pool:
        loaded = list(pool.map(load, needed))
    now = datetime.now(UTC)
    for game_id, payload in loaded:
        if payload is None:
            continue
        if game_id not in live_ids:
            _CHECKED.add(game_id)
        names = catalog.get(game_id) or []
        if not names:
            continue
        for person in parse(payload):
            decision = match_name(str(person.get("name") or ""), names)
            if decision.review_status != "auto" or not decision.canonical_id:
                continue
            markets = person.get("markets") or {}
            for market in COUNTING:
                if market not in markets:
                    continue
                key = f"{game_id}|{decision.canonical_id}|{market}"
                stat = session.get(PublishedStat, key)
                if stat is None:
                    stat = PublishedStat(
                        key=key,
                        game_id=game_id,
                        player_id=decision.canonical_id,
                        market_id=market,
                    )
                    session.add(stat)
                stat.actual = float(markets[market])
                stat.observed_at = now
                stat.source = "espn.summary.live" if game_id in live_ids else "espn.summary"
    session.flush()


def _catalog(rows: list[dict[str, Any]]) -> dict[str, list[tuple[str, str]]]:
    catalog: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        game_id = str(row.get("game_id") or "")
        player_id = str(row.get("player_id") or "")
        name = str(row.get("player_name") or "")
        if not game_id or not player_id or not name:
            continue
        pair = (player_id, name)
        found = catalog.setdefault(game_id, [])
        if pair not in found:
            found.append(pair)
    return catalog
