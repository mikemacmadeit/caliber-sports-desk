"""Assemble a day's parlay card from props that already clear their price.

Soft lines, where the model is in the 80s or 90s because the number sits far
from the projection, stay off the card. Cross-game tickets multiply leg
chances. A same-game ticket is two anytime touchdowns, and only when a
shared-draw joint is supplied.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from spe.db.base import as_utc
from spe.market.odds import american_to_decimal, american_to_implied

CARD_MARKETS = ("anytime_td", "receptions", "receiving_yards", "rushing_yards")
SKILL_POSITIONS = {"RB", "WR", "TE"}
MIN_HAIRCUT = 0.0
# A leg above this is a soft number. It clears the price because the line is
# easy, and it makes a parlay read like a lock.
MAX_LEG_P = 0.66
STANDARD_MIN_P = 0.50
LONG_MIN_P = 0.30
LOTTERY_MIN_P = 0.18
LOTTERY_MAX_P = 0.42
LOTTERY_MIN_PRICE = 300
TARGET_P = 0.58
# A model chance more than this many times the price is too far from the number to print.
MAX_PRICE_GAP = 2.75
TD_MIN_P = 0.18
TD_MAX_P = 0.75
TD_LONG_MIN_PRICE = 150
TD_LONG_LEGS = 6
# A quote can sit past the bet-now window and still be the last number for a game that has not kicked off.
DISPLAY_PRICE_MINUTES = 180.0


def build_parlay_card(
    rows: list[dict[str, Any]],
    joints: dict[tuple[str, str, str], float] | None = None,
    spreads: list[dict[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the tickets for one slate.

    ``joints`` maps ``(game_id, player_lo, player_hi)`` to a same-draw
    anytime-touchdown joint. Pairs without an entry are left off the card.
    """
    standard = _collapse(_priced(rows, _standard))
    long_pool = _collapse(_priced(rows, _longshot))
    lottery_pool = _priced(rows, _lottery)
    tickets: list[dict[str, Any]] = []
    used_players: set[str] = set()

    pair = _best_pair(standard)
    if pair is not None:
        tickets.append(
            _cross_ticket(
                pair,
                ticket_id="best-chance",
                kind="best_chance",
                title="Best chance",
                summary="Two games and two markets. Each chance sits next to the number.",
            )
        )
        used_players.update(str(row["player_id"]) for row in pair)

    mix = _mix(standard, avoid_players=used_players)
    if mix is None:
        mix = _mix(standard, avoid_players=set())
    if mix is not None and not _same_legs_as(mix, tickets):
        tickets.append(
            _cross_ticket(
                mix,
                ticket_id="mix",
                kind="mix",
                title="Mix",
                summary="Three markets, three games. The chance they all hit is the product of the legs.",
            )
        )
        used_players.update(str(row["player_id"]) for row in mix)

    long_rows = _long_ticket(long_pool, standard, avoid_players=used_players)
    if long_rows is not None and not _same_legs_as(long_rows, tickets):
        tickets.append(
            _cross_ticket(
                long_rows,
                ticket_id="long-shot",
                kind="long_shot",
                title="Long shot",
                summary="Plus-money legs from different games. Stacking them is what makes the ticket long.",
            )
        )

    lottery_rows = _lottery_ticket(lottery_pool)
    if lottery_rows is not None:
        tickets.append(
            _cross_ticket(
                lottery_rows,
                ticket_id="lottery",
                kind="lottery",
                title="Lottery",
                summary="One anytime touchdown from each game, all at a long plus-money price.",
            )
        )

    same = _same_game_ticket(standard, joints or {})
    if same is not None:
        tickets.append(same)

    tickets.extend(_anytime_tickets(rows, now=now or datetime.now(UTC)))
    tickets.extend(_spread_tickets(spreads or [], now=now or datetime.now(UTC)))

    return {
        "tickets": tickets,
        "eligible_legs": len(standard) + len(long_pool) + len({str(row["player_id"]) for row in lottery_pool}),
        "games_with_legs": len({str(row["game_id"]) for row in standard}),
    }


def best_same_game_pair(rows: list[dict[str, Any]]) -> tuple[str, str, str] | None:
    """The skill-position anytime pair in the believable chance band."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in _collapse(_priced(rows, _standard)):
        if row.get("market_id") != "anytime_td" or not _skill_position(row):
            continue
        grouped.setdefault(str(row["game_id"]), []).append(row)

    best: tuple[str, str, str] | None = None
    best_product = -1.0
    for game_id, group in grouped.items():
        seen: dict[str, dict[str, Any]] = {}
        for row in sorted(group, key=lambda item: -_p(item)):
            player_id = str(row.get("player_id") or "")
            if not player_id or player_id in seen:
                continue
            seen[player_id] = row
        top = list(seen.values())[:2]
        if len(top) < 2:
            continue
        product = _p(top[0]) * _p(top[1])
        if product > best_product:
            best_product = product
            best = (game_id, str(top[0]["player_id"]), str(top[1]["player_id"]))
    return best


def _priced(rows: list[dict[str, Any]], pred) -> list[dict[str, Any]]:
    return [row for row in rows if _base_ok(row) and pred(row)]


def _base_ok(row: dict[str, Any]) -> bool:
    if row.get("market_id") not in CARD_MARKETS or not row.get("player_id"):
        return False
    if row.get("market_id") == "anytime_td" and str(row.get("side") or "yes") not in {"yes", "over"}:
        return False
    if _trivial_floor(row) or row.get("book_price") is None:
        return False
    freshness = row.get("price_freshness") or (row.get("forecast") or {}).get("price_freshness") or {}
    if freshness.get("status") != "fresh" or not freshness.get("is_current_opportunity"):
        return False
    forecast = row.get("forecast") or {}
    if str(forecast.get("availability_status") or "") == "out":
        return False
    if forecast.get("p_estimate") is None:
        return False
    haircut = forecast.get("ev_after_haircut")
    if haircut is None or float(haircut) < MIN_HAIRCUT:
        return False
    implied = american_to_implied(int(row["book_price"]))
    return implied > 0 and _p(row) <= implied * MAX_PRICE_GAP


def _standard(row: dict[str, Any]) -> bool:
    return STANDARD_MIN_P <= _p(row) <= MAX_LEG_P


def _longshot(row: dict[str, Any]) -> bool:
    price = int(row["book_price"])
    return price > 0 and LONG_MIN_P <= _p(row) < STANDARD_MIN_P


def _lottery(row: dict[str, Any]) -> bool:
    price = int(row["book_price"])
    return (
        row.get("market_id") == "anytime_td"
        and price >= LOTTERY_MIN_PRICE
        and LOTTERY_MIN_P <= _p(row) <= LOTTERY_MAX_P
    )


def _collapse(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One quote per player and market, the line closest to a real pick."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row["game_id"]), str(row["player_id"]), str(row["market_id"]))
        groups.setdefault(key, []).append(row)
    chosen = []
    for group in groups.values():
        group.sort(key=_standard_rank)
        chosen.append(group[0])
    return chosen


def _trivial_floor(row: dict[str, Any]) -> bool:
    """A half-catch, or a fade of a one-catch line, is not a ticket leg."""
    if row.get("market_id") != "receptions":
        return False
    line = row.get("line")
    if line is None:
        return False
    side = str(row.get("side") or "")
    number = float(line)
    if side == "over" and number < 1.5:
        return True
    return side == "under" and number <= 1.5


def _best_pair(legs: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    pool = _one_per_game(legs)
    best: list[dict[str, Any]] | None = None
    best_key: tuple[int, float] | None = None
    for i, left in enumerate(pool):
        for right in pool[i + 1 :]:
            same_market = 1 if left["market_id"] == right["market_id"] else 0
            key = (same_market, -(_p(left) * _p(right)))
            if best_key is None or key < best_key:
                best_key = key
                best = [left, right]
    return best


def _mix(legs: list[dict[str, Any]], *, avoid_players: set[str]) -> list[dict[str, Any]] | None:
    by_market: dict[str, list[dict[str, Any]]] = {}
    for row in legs:
        if str(row["player_id"]) in avoid_players:
            continue
        by_market.setdefault(str(row["market_id"]), []).append(row)
    markets = sorted(by_market, key=lambda market: -max(_p(row) for row in by_market[market]))
    chosen: list[dict[str, Any]] = []
    used_games: set[str] = set()
    used_players: set[str] = set(avoid_players)
    for market in markets:
        if len(chosen) >= 3:
            break
        options = [
            row
            for row in by_market[market]
            if str(row["game_id"]) not in used_games and str(row["player_id"]) not in used_players
        ]
        if not options:
            continue
        options.sort(key=_standard_rank)
        pick = options[0]
        chosen.append(pick)
        used_games.add(str(pick["game_id"]))
        used_players.add(str(pick["player_id"]))
    if len(chosen) < 3 or len({str(row["market_id"]) for row in chosen}) < 3:
        return None
    return chosen


def _long_ticket(
    long_pool: list[dict[str, Any]],
    standard: list[dict[str, Any]],
    *,
    avoid_players: set[str],
) -> list[dict[str, Any]] | None:
    plus = [row for row in standard if int(row["book_price"]) > 0]
    pool = long_pool + plus
    chosen = _diverse_plus(pool, avoid_players)
    if chosen is not None:
        return chosen
    return _diverse_plus(pool, set())


def _diverse_plus(pool: list[dict[str, Any]], avoid_players: set[str]) -> list[dict[str, Any]] | None:
    """Up to three plus-money legs, different games, at least two markets."""
    available = [row for row in pool if str(row["player_id"]) not in avoid_players]
    by_market: dict[str, list[dict[str, Any]]] = {}
    for row in available:
        by_market.setdefault(str(row["market_id"]), []).append(row)
    for group in by_market.values():
        group.sort(key=lambda row: (-int(row["book_price"]), -_p(row)))
    order = sorted(
        by_market,
        key=lambda market: (0 if market == "anytime_td" else 1, -int(by_market[market][0]["book_price"])),
    )
    chosen: list[dict[str, Any]] = []
    used_games: set[str] = set()
    used_players: set[str] = set()

    def take(row: dict[str, Any]) -> None:
        chosen.append(row)
        used_games.add(str(row["game_id"]))
        used_players.add(str(row["player_id"]))

    for market in order:
        if len(chosen) >= 3:
            break
        for row in by_market[market]:
            if str(row["game_id"]) in used_games or str(row["player_id"]) in used_players:
                continue
            take(row)
            break
    if len({str(row["market_id"]) for row in chosen}) >= 2:
        rest = sorted(available, key=lambda row: (-int(row["book_price"]), -_p(row)))
        for row in rest:
            if len(chosen) >= 3:
                break
            if str(row["game_id"]) in used_games or str(row["player_id"]) in used_players:
                continue
            take(row)
    if len(chosen) < 2 or len({str(row["market_id"]) for row in chosen}) < 2:
        return None
    return chosen


def _anytime_tickets(rows: list[dict[str, Any]], *, now: datetime) -> list[dict[str, Any]]:
    """Scorer tickets. A positive price edge is not required. An out player is."""
    by_game: dict[str, dict[str, Any]] = {}
    long_by_game: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not _td_leg(row, now):
            continue
        game_id = str(row["game_id"])
        current = by_game.get(game_id)
        if current is None or _p(row) > _p(current):
            by_game[game_id] = row
        if int(row["book_price"]) >= TD_LONG_MIN_PRICE:
            current_long = long_by_game.get(game_id)
            if current_long is None or _p(row) > _p(current_long):
                long_by_game[game_id] = row

    ranked = sorted(by_game.values(), key=lambda row: -_p(row))
    tickets: list[dict[str, Any]] = []
    if len(ranked) >= 2:
        tickets.append(
            _cross_ticket(
                ranked[:2],
                ticket_id="anytime-two",
                kind="anytime",
                title="Anytime TD",
                summary="The most likely scorer in two different games. The chance both score is the product of the two chances. The price is the last number posted.",
            )
        )
    if len(ranked) >= 3:
        tickets.append(
            _cross_ticket(
                ranked[:3],
                ticket_id="anytime-three",
                kind="anytime_three",
                title="Anytime TD, three",
                summary="Three games, the most likely scorer in each. All three have to find the end zone. The price is the last number posted.",
            )
        )
    long_ranked = sorted(long_by_game.values(), key=lambda row: -_p(row))
    if len(long_ranked) >= TD_LONG_LEGS:
        tickets.append(
            _cross_ticket(
                long_ranked[:TD_LONG_LEGS],
                ticket_id="anytime-six",
                kind="anytime_long",
                title="Anytime TD, six",
                summary="Six players, six games, each at plus money. Every one has to score. A price the chance cannot explain stays off.",
            )
        )
    return tickets


def _td_leg(row: dict[str, Any], now: datetime) -> bool:
    if row.get("market_id") != "anytime_td" or not row.get("player_id"):
        return False
    if str(row.get("side") or "yes") not in {"yes", "over"}:
        return False
    if not _skill_position(row) or row.get("book_price") is None:
        return False
    forecast = row.get("forecast") or {}
    if str(forecast.get("availability_status") or "") == "out":
        return False
    if not _price_still_posted(row, now):
        return False
    if not (TD_MIN_P <= _p(row) <= TD_MAX_P):
        return False
    implied = american_to_implied(int(row["book_price"]))
    return implied > 0 and _p(row) <= implied * MAX_PRICE_GAP


def _price_still_posted(row: dict[str, Any], now: datetime) -> bool:
    """Fresh, or the last quote for a game that has not started."""
    freshness = row.get("price_freshness") or (row.get("forecast") or {}).get("price_freshness") or {}
    if freshness.get("status") == "fresh" and freshness.get("is_current_opportunity"):
        return True
    age = freshness.get("age_minutes")
    if age is None or float(age) > DISPLAY_PRICE_MINUTES:
        return False
    kick = _kickoff(row.get("kickoff_utc"))
    return kick is not None and kick > as_utc(now)


def _spread_tickets(rows: list[dict[str, Any]], *, now: datetime) -> list[dict[str, Any]]:
    legs = [leg for leg in (_spread_leg(row, now) for row in rows) if leg is not None]
    legs.sort(key=lambda row: str(row.get("kickoff_utc") or ""))
    summary = (
        "The model's side against the number. The chance is that side's cover chance. "
        "It is a research lean, not a play, and it does not rank one game as stronger than another."
    )
    tickets: list[dict[str, Any]] = []
    if len(legs) >= 2:
        tickets.append(
            _cross_ticket(
                legs[:2],
                ticket_id="spread-two",
                kind="spread",
                title="Spread, two teams",
                summary=summary,
            )
        )
    if len(legs) >= 5:
        tickets.append(
            _cross_ticket(
                legs[2:5],
                ticket_id="spread-three",
                kind="spread_three",
                title="Spread, three teams",
                summary=summary,
            )
        )
    return tickets


def _spread_leg(row: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    if row.get("projection_withheld"):
        return None
    if str(row.get("game_status") or "scheduled") not in {"scheduled", "preview", "pregame"}:
        return None
    kick = _kickoff(row.get("kickoff_utc"))
    if kick is None or kick <= as_utc(now):
        return None
    side = row.get("ats_side") or row.get("favored_side")
    if side not in {"home", "away"}:
        return None
    chance = row.get("ats_probability")
    if chance is None:
        chance = row.get("p_away_cover") if side == "away" else row.get("p_home_cover")
    price = row.get("away_spread_price") if side == "away" else row.get("home_spread_price")
    line = row.get("ats_line")
    if line is None:
        line = row.get("favored_side_line")
    if chance is None or price is None or line is None:
        return None
    team = row.get("ats_team") or (row.get("away_team") if side == "away" else row.get("home_team"))
    other = row.get("home_team") if side == "away" else row.get("away_team")
    away_abbr, home_abbr = _matchup_abbrs(str(row.get("game_id") or ""), str(row.get("away_team") or ""), str(row.get("home_team") or ""))
    return {
        "outcome_id": f"{row.get('game_id')}:spread:{side}",
        "game_id": row.get("game_id"),
        "league_id": row.get("league_id"),
        "player_id": f"spread:{side}",
        "player_name": team or "Side",
        "position": None,
        "team": (away_abbr if side == "away" else home_abbr) or team or "",
        "opponent": other or "",
        "away_abbr": away_abbr,
        "home_abbr": home_abbr,
        "kickoff_utc": row.get("kickoff_utc"),
        "market_id": "point_spread",
        "side": side,
        "line": float(line),
        "book_price": int(price),
        "forecast": {"p_estimate": float(chance), "availability_status": "probable"},
    }


def _matchup_abbrs(game_id: str, away: str, home: str) -> tuple[str, str]:
    parts = game_id.split(":")[-1].split("_")
    if len(parts) >= 2 and parts[-2].isalpha() and parts[-1].isalpha():
        return parts[-2], parts[-1]
    return away, home


def _kickoff(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return as_utc(value)
    if isinstance(value, str) and value:
        return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    return None


def _lottery_ticket(rows: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    by_game: dict[str, dict[str, Any]] = {}
    for row in rows:
        game_id = str(row["game_id"])
        current = by_game.get(game_id)
        if current is None or _p(row) > _p(current):
            by_game[game_id] = row
    chosen = sorted(by_game.values(), key=lambda row: -_p(row))[:3]
    if len(chosen) < 3:
        return None
    return chosen


def _one_per_game(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for row in legs:
        game_id = str(row["game_id"])
        current = best.get(game_id)
        if current is None or _standard_rank(row) < _standard_rank(current):
            best[game_id] = row
    return list(best.values())


def _same_legs_as(rows: list[dict[str, Any]], tickets: list[dict[str, Any]]) -> bool:
    ids = frozenset(str(row.get("outcome_id")) for row in rows)
    return any(frozenset(str(leg.get("outcome_id")) for leg in ticket["legs"]) == ids for ticket in tickets)


def _same_game_ticket(
    legs: list[dict[str, Any]],
    joints: dict[tuple[str, str, str], float],
) -> dict[str, Any] | None:
    pair = best_same_game_pair(legs)
    if pair is None:
        return None
    game_id, player_a, player_b = pair
    lo, hi = sorted((player_a, player_b))
    joint = joints.get((game_id, lo, hi))
    if joint is None:
        return None
    by_player = {
        str(row["player_id"]): row
        for row in legs
        if str(row["game_id"]) == game_id and row.get("market_id") == "anytime_td"
    }
    first = by_player.get(player_a)
    second = by_player.get(player_b)
    if first is None or second is None:
        return None
    ordered = sorted((first, second), key=lambda row: -_p(row))
    independent = _p(ordered[0]) * _p(ordered[1])
    away = ordered[0].get("away_abbr") or "Away"
    home = ordered[0].get("home_abbr") or "Home"
    return {
        "id": "same-game",
        "kind": "same_game",
        "title": "Same game",
        "summary": (
            f"Both score in {away} at {home}. The chance is counted on shared draws. "
            f"Multiplying the two chances would read {independent * 100:.0f}%."
        ),
        "method": "same_draw_joint",
        "joint_probability": float(joint),
        "independent_probability": independent,
        "price_american": None,
        "ev": None,
        "legs": [_leg_out(row) for row in ordered],
    }


def _cross_ticket(
    rows: list[dict[str, Any]],
    *,
    ticket_id: str,
    kind: str,
    title: str,
    summary: str,
) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: (str(row.get("kickoff_utc") or ""), str(row.get("player_name") or "")))
    joint = 1.0
    decimal = 1.0
    for row in ordered:
        joint *= _p(row)
        decimal *= american_to_decimal(int(row["book_price"]))
    ev = joint * (decimal - 1.0) - (1.0 - joint)
    return {
        "id": ticket_id,
        "kind": kind,
        "title": title,
        "summary": summary,
        "method": "independent_product",
        "joint_probability": joint,
        "independent_probability": joint,
        "price_american": _decimal_to_american(decimal),
        "ev": ev,
        "legs": [_leg_out(row) for row in ordered],
    }


def _leg_out(row: dict[str, Any]) -> dict[str, Any]:
    kick = row.get("kickoff_utc")
    if hasattr(kick, "isoformat"):
        kick = kick.isoformat()
    return {
        "outcome_id": row.get("outcome_id"),
        "game_id": row.get("game_id"),
        "league_id": row.get("league_id"),
        "player_id": row.get("player_id"),
        "player_name": row.get("player_name"),
        "position": row.get("position"),
        "team": row.get("team"),
        "opponent": row.get("opponent"),
        "away_abbr": row.get("away_abbr"),
        "home_abbr": row.get("home_abbr"),
        "kickoff_utc": kick,
        "market_id": row.get("market_id"),
        "side": row.get("side"),
        "line": row.get("line"),
        "book_price": row.get("book_price"),
        "p": _p(row),
    }


def _skill_position(row: dict[str, Any]) -> bool:
    position = str(row.get("position") or "").upper()
    if not position:
        return True
    return position in SKILL_POSITIONS


def _p(row: dict[str, Any]) -> float:
    return float((row.get("forecast") or {}).get("p_estimate") or 0.0)


def _standard_rank(row: dict[str, Any]) -> tuple[float, float, float, float, str]:
    haircut = float((row.get("forecast") or {}).get("ev_after_haircut") or 0.0)
    position = str(row.get("position") or "").upper()
    skill = 0.0 if position in SKILL_POSITIONS or not position else 1.0
    under = 1.0 if str(row.get("side") or "") == "under" else 0.0
    return (skill, under, abs(_p(row) - TARGET_P), -haircut, str(row.get("outcome_id") or ""))


def _decimal_to_american(decimal: float) -> int:
    if decimal >= 2.0:
        return int(round((decimal - 1.0) * 100))
    return int(round(-100.0 / (decimal - 1.0)))

