"""Grade the day's shown picks from the recorded game and box score.

A box that fails to load stays pending. A finished box that omits a player,
or omits one of that player's counting stats, recorded a zero.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from spe.db.models import Game, PublishedStat
from spe.identity.match import normalize_name
from spe.models.ats.predict import grade_displayed_pick
from spe.publishing.boxes import box_actual, parse
from spe.shadow.settle import _grade

COUNTING = ("receptions", "rushing_yards", "receiving_yards")
TOUCHDOWN = "anytime_td"
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
    _fill_counting_stats(session, games, prices, fetch=fetch)
    for row in spreads:
        _grade_spread(row, games.get(row.get("game_id")))
    stats = {
        (stat.game_id, stat.player_id, stat.market_id): stat.actual
        for stat in session.scalars(
            select(PublishedStat).where(
                PublishedStat.game_id.in_(game_ids),
                PublishedStat.market_id.in_((*COUNTING, TOUCHDOWN)),
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
    if market_id == TOUCHDOWN and status not in {"final", "post"}:
        # A touchdown already scored cannot be taken back. A zero can still change.
        return "win" if actual > 0 else "pending"
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
    missing = _missing_stats(session, rows)
    live_touchdowns = _live_touchdowns(session, games, rows)
    missing_games = {item[0] for item in missing}
    touchdown_games = {item[0] for item in live_touchdowns}
    now = datetime.now(UTC)
    needed = [
        game
        for game in games.values()
        if game.game_id in touchdown_games or _needs_a_box(game, missing_games, now)
    ]
    if not needed:
        return
    from spe.ingest.espn_box import _espn_event_id, _fetch_summary

    loader = fetch or _fetch_summary
    targets = [(game, _espn_event_id(session, game)) for game in needed]

    def load(item: tuple[Game, str | None]) -> tuple[str, dict[str, Any] | None]:
        game, event_id = item
        if not event_id:
            return game.game_id, None
        payload = loader(game.league_id, event_id)
        return game.game_id, payload if isinstance(payload, dict) else None

    with ThreadPoolExecutor(max_workers=8) as pool:
        loaded = list(pool.map(load, targets))
    now = datetime.now(UTC)
    for game_id, payload in loaded:
        game = games.get(game_id)
        if game is None or payload is None:
            continue
        finished = _apply_summary(game, payload)
        people = parse(payload)
        if finished and (people or game_id not in missing_games):
            _CHECKED.add(game_id)
        if not people:
            continue
        by_espn = {str(person.get("espn_id")): person for person in people if person.get("espn_id")}
        by_name = {normalize_name(str(person.get("name") or "")): person for person in people}
        refreshing = [
            item for item in [*missing, *live_touchdowns]
            if item[0] == game_id
        ]
        # autoflush is off, so a second line for the same player is invisible to
        # session.get until the write. Adding it again takes the whole board down.
        staged: dict[str, PublishedStat] = {}
        for _game_id, player_id, name, market, key in refreshing:
            person = by_espn.get(str(player_id).split(":")[-1]) or by_name.get(normalize_name(name))
            markets = None if person is None else (person.get("markets") or {})
            actual = box_actual(markets, market, final=finished)
            if actual is None:
                continue
            stat = staged.get(key)
            if stat is None:
                stat = session.get(PublishedStat, key)
            if stat is None:
                stat = PublishedStat(
                    key=key, game_id=game_id, player_id=player_id, market_id=market,
                )
                session.add(stat)
            staged[key] = stat
            stat.actual = actual
            stat.observed_at = now
            # A live touchdown can show green. It is not the official final box.
            if market == TOUCHDOWN and not finished:
                stat.source = "espn.live"
            else:
                stat.source = "espn.summary" if finished else "espn.summary.live"
    session.flush()


def refresh_live_touchdowns(session: Session, games: list[Game]) -> None:
    """Write live touchdowns for saved picks before the card is graded."""
    started = [
        game for game in games
        if str(game.status or "").lower() not in {"canceled", "cancelled"}
    ]
    if not started:
        return
    from sqlalchemy import or_

    from spe.db.models import BoardSnapshot

    filters = []
    for game in started:
        gid = game.game_id
        filters.append(BoardSnapshot.key.in_([f"scorer-desk-v1:{gid}", f"guarded-desk-v1:{gid}"]))
        filters.append(BoardSnapshot.key.like(f"retrained-desk-v1:{gid}:%"))
        filters.append(BoardSnapshot.key.like(f"scorer-replay-v1:{gid}:%"))
        filters.append(BoardSnapshot.key.like(f"combined-replay-v4-free:{gid}:%"))
    snaps = session.scalars(select(BoardSnapshot).where(or_(*filters))).all()
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for snap in snaps:
        payload = snap.payload or {}
        saved = payload.get("row") or {}
        game_id = str(saved.get("game_id") or payload.get("game_id") or "")
        decision = saved.get("decision") or {}
        names = [
            (saved.get("player_id"), saved.get("player_name")),
            ((decision.get("most_likely") or {}).get("player_id"), (decision.get("most_likely") or {}).get("name")),
            ((decision.get("value_candidate") or {}).get("player_id"), (decision.get("value_candidate") or {}).get("name")),
        ]
        for player_id, name in names:
            if not game_id or not player_id:
                continue
            key = (game_id, str(player_id))
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "game_id": game_id,
                "player_id": str(player_id),
                "player_name": str(name or ""),
                "market_id": TOUCHDOWN,
                "side": "yes",
            })
    if rows:
        apply_recorded_results(session, [], rows)


def _live_touchdowns(
    session: Session,
    games: dict[str, Game],
    rows: list[dict[str, Any]],
) -> list[tuple[str, str, str, str, str]]:
    """Anytime rows still waiting on a box.

    A live game is always refreshed, so a new score can replace a stored zero.
    A final game is fetched until the official box is saved.
    """
    wanted: list[tuple[str, str, str, str, str]] = []
    finals: list[tuple[str, str, str, str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if str(row.get("market_id") or "") != TOUCHDOWN:
            continue
        game = games.get(str(row.get("game_id") or ""))
        if game is None or str(game.status or "").lower() in {"canceled", "cancelled"}:
            continue
        game_id = str(row.get("game_id") or "")
        player_id = str(row.get("player_id") or "")
        if not game_id or not player_id:
            continue
        key = f"{game_id}|{player_id}|{TOUCHDOWN}"
        if key in seen:
            continue
        seen.add(key)
        item = (game_id, player_id, str(row.get("player_name") or ""), TOUCHDOWN, key)
        if str(game.status or "").lower() in {"final", "post"}:
            finals.append(item)
        else:
            wanted.append(item)
    if not finals:
        return wanted
    saved = {
        stat.key: stat
        for stat in session.scalars(select(PublishedStat).where(PublishedStat.key.in_([item[4] for item in finals])))
    }
    for item in finals:
        stat = saved.get(item[4])
        if stat is not None and str(stat.source or "").startswith("espn.summary"):
            continue
        wanted.append(item)
    return wanted


def _needs_a_box(game: Game, missing_games: set[str], now: datetime) -> bool:
    """A game that has not kicked off has no box. Fetching it only slows the page."""
    status = str(game.status or "").lower()
    if status in {"canceled", "cancelled"} or game.game_id in _CHECKED:
        return False
    if status in {"final", "post"}:
        return game.game_id in missing_games
    if status in {"in_progress", "halftime"}:
        return True
    kick = game.kickoff_utc
    if kick is not None and kick.tzinfo is None:
        kick = kick.replace(tzinfo=UTC)
    return kick is None or kick <= now


def _missing_stats(session: Session, rows: list[dict[str, Any]]) -> list[tuple[str, str, str, str, str]]:
    wanted: list[tuple[str, str, str, str, str]] = []
    for row in rows:
        game_id = str(row.get("game_id") or "")
        player_id = str(row.get("player_id") or "")
        market = str(row.get("market_id") or "")
        if market not in COUNTING or not game_id or not player_id:
            continue
        wanted.append((game_id, player_id, str(row.get("player_name") or ""), market, f"{game_id}|{player_id}|{market}"))
    if not wanted:
        return []
    existing = set(
        session.scalars(select(PublishedStat.key).where(PublishedStat.key.in_([item[4] for item in wanted])))
    )
    return [item for item in wanted if item[4] not in existing]


def _apply_summary(game: Game, payload: dict[str, Any]) -> bool:
    """Copy a final score onto the game. Returns whether the game is now final."""
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    competitions = header.get("competitions") or []
    if not competitions or not isinstance(competitions[0], dict):
        return str(game.status or "").lower() in {"final", "post"}
    competition = competitions[0]
    status_name = str(((competition.get("status") or {}).get("type") or {}).get("name") or "")
    scores: dict[str, int] = {}
    for side in competition.get("competitors") or []:
        if not isinstance(side, dict):
            continue
        place = str(side.get("homeAway") or "")
        try:
            scores[place] = int(side.get("score"))
        except (TypeError, ValueError):
            continue
    if status_name in {"STATUS_FINAL", "STATUS_FULL_TIME"} and "home" in scores and "away" in scores:
        game.status = "final"
        game.home_score = scores["home"]
        game.away_score = scores["away"]
        return True
    if str(game.status or "").lower() != "final" and status_name in {"STATUS_IN_PROGRESS", "STATUS_HALFTIME", "STATUS_END_PERIOD"}:
        if "home" in scores and "away" in scores:
            game.status = "in_progress"
            game.home_score = scores["home"]
            game.away_score = scores["away"]
    return str(game.status or "").lower() in {"final", "post"}

