"""Publication is an insert, never a recalculation of history."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from collections import defaultdict
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from sqlalchemy import event, func
from sqlalchemy.orm import aliased
from sqlalchemy.exc import IntegrityError
from spe.db.base import as_utc, new_id
from spe.db.models import PublishedPick, PublishedResult, PublishedCorrection, Game, PublishedStat
from spe.market.odds import american_to_decimal
from spe.props.game_picks import build_game_prop_picks
from spe.publishing.policy import assess
from spe.shadow.settle import _grade


@event.listens_for(PublishedPick, "before_update")
@event.listens_for(PublishedPick, "before_delete")
@event.listens_for(PublishedResult, "before_update")
@event.listens_for(PublishedResult, "before_delete")
@event.listens_for(PublishedCorrection, "before_update")
@event.listens_for(PublishedCorrection, "before_delete")
def immutable(mapper, connection, target):
    raise ValueError("Published history is append-only")


def record_frozen_desk_picks(db):
    """Copy the last pre-kickoff Desk pick into the public record.

    A later pass before kickoff stays a pass. A decision made after kickoff
    never becomes the published pick. An existing publication is left as it is.
    """
    from spe.db.models import DeskDecision
    from spe.props.game_picks import _public_pick

    latest: dict[str, tuple[datetime, DeskDecision]] = {}
    for decision in db.query(DeskDecision).all():
        payload = decision.payload or {}
        kick = as_utc(payload.get("kickoff_utc"))
        decided = as_utc(decision.decided_at)
        if kick is None or decided is None or decided >= kick:
            continue
        current = latest.get(decision.game_id)
        if current is None or decided >= current[0]:
            latest[decision.game_id] = (decided, decision)
    written = 0
    for game_id, (decided, decision) in latest.items():
        source = (decision.payload or {}).get("row")
        if not isinstance(source, dict) or not source.get("player_id") or source.get("book_price") is None:
            continue
        if str(source.get("market_id") or "") != "anytime_td":
            continue
        game = db.get(Game, game_id)
        if game is None or game.data_tier == "demo":
            continue
        kick = as_utc(game.kickoff_utc)
        if kick is None or decided >= kick:
            continue
        if _game_already_published(db, game.game_id):
            continue
        row = dict(source)
        if not row.get("side"):
            row["side"] = "yes"
        try:
            snapshot = _public_pick(row, price_ahead=False, stale=False)
        except (TypeError, ValueError, KeyError):
            continue
        snapshot["player_id"] = row.get("player_id")
        snapshot["outcome_id"] = row.get("outcome_id")
        snapshot["bookmaker"] = row.get("bookmaker")
        snapshot["forecast"] = {"model_version": (row.get("forecast") or {}).get("model_version")}
        try:
            with db.begin_nested():
                db.add(
                    PublishedPick(
                        pick_id=new_id(),
                        game_id=game.game_id,
                        league_id=game.league_id,
                        market_id="anytime_td",
                        published_at=decided,
                        kickoff_utc=kick,
                        snapshot=json.loads(json.dumps(snapshot, default=str)),
                    )
                )
                db.flush()
            written += 1
        except IntegrityError:
            if not _game_already_published(db, game.game_id):
                raise
    return {"recorded": written}


def publish(db, rows, now=None):
    now = as_utc(now) or datetime.now(UTC)
    selected = build_game_prop_picks(rows, now=now)
    written = 0
    for pick in selected:
        game = db.get(Game, pick["game_id"])
        if (
            not game
            or game.data_tier == "demo"
            or game.status not in {"scheduled", "pre", "upcoming"}
        ):
            continue
        kick = as_utc(game.kickoff_utc)
        if not kick or kick <= now:
            continue
        if _game_already_published(db, pick["game_id"]):
            continue
        snapshot = json.loads(json.dumps(pick, default=str))
        try:
            with db.begin_nested():
                db.add(
                    PublishedPick(
                        pick_id=new_id(),
                        game_id=game.game_id,
                        league_id=game.league_id,
                        market_id=pick["market_id"],
                        published_at=now,
                        kickoff_utc=kick,
                        snapshot=snapshot,
                    )
                )
                db.flush()
            written += 1
        except IntegrityError:
            # Another worker already published this game. Never add a second prop.
            if not _game_already_published(db, game.game_id):
                raise
    return {"status": "ok", "published": written, "eligible": len(selected)}


def _game_already_published(db, game_id: str) -> bool:
    return db.query(PublishedPick.pick_id).filter_by(game_id=game_id).first() is not None


SLATE = ZoneInfo("America/Chicago")
_MARKET_ORDER = {"point_spread": 0, "anytime_td": 1, "anytime_td_secondary": 2, "receptions": 3, "rushing_yards": 4, "receiving_yards": 5}


def slate_dates(db, month=None) -> list[str]:
    """Days that already have a saved card, a frozen pick, or a spread on the board."""
    from spe.db.models import BoardSnapshot

    found: set[str] = set()
    keys = db.query(BoardSnapshot.key)
    if month:
        keys = keys.filter(BoardSnapshot.key.contains(month + "-"))
    for item in keys:
        key = str(_column(item))
        if key.startswith("ats-day:"):
            found.add(key.split(":")[1])
        if key.startswith("atd-desk-card-v1:"):
            found.add(key.split(":")[1])
        parts = key.split("|")
        if len(parts) == 3 and len(parts[2]) == 10 and parts[2][4] == "-" and parts[2][7] == "-":
            found.add(parts[2])
    picks = db.query(PublishedPick.kickoff_utc)
    if month:
        start, _ = _day_bounds(month + "-01")
        end = (start.astimezone(SLATE).replace(day=28) + timedelta(days=4)).replace(day=1).astimezone(UTC)
        picks = picks.filter(PublishedPick.kickoff_utc >= start, PublishedPick.kickoff_utc < end)
    for item in picks:
        day = _slate_day(_column(item))
        if day:
            found.add(day)
    from spe.models.ats.board_cache import read_board

    board = read_board(session=db) or {}
    for row in board.get("rows") or []:
        if isinstance(row, dict):
            day = _slate_day(row.get("kickoff_utc"))
            if day:
                found.add(day)
    return sorted(found)


def _packaged_day(date: str, league: str | None) -> dict[str, Any] | None:
    """A finished day that is already written down. Opening it does not rebuild the card."""
    path = Path(__file__).resolve().parent / "frozen" / f"{date}.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    wanted = league or "all"
    rows = list(payload.get("rows") or [])
    if wanted != "all":
        rows = [row for row in rows if row.get("league_id") == wanted]
    return {
        "date": date,
        "timezone": payload.get("timezone") or "America/Chicago",
        "league": wanted,
        "rows": rows,
        "dates": list(payload.get("dates") or []),
        "summary": _ledger_summary(rows),
    }


def ledger_day(db, date: str, league: str | None = None) -> dict[str, Any]:
    """Verified pregame records; historical display archives are explicitly separate."""
    props = [r for r in day_picks(db, league, date) if r.get("market_id") != "anytime_td"]
    ats = ats_history(db, league, limit=100000, date=date)["rows"]
    rows = _ledger_rows([], [], props + ats + _replay_scorers(db, date, league))
    if not rows:
        archive = _packaged_day(date, league)
        if archive:
            # Keep the old display available without inventing evidence of pregame publication.
            archive["record_status"] = "archived_unverified"
            archive["note"] = "Historical archive: pregame publication times have not been verified. Excluded from verified performance and returns."
            archive["rows"] = [{**r, "units": None} for r in archive["rows"]]
            archive["summary"] = _ledger_summary(archive["rows"])
            return archive
    return {"date": date, "timezone": "America/Chicago", "league": league or "all",
            "rows": rows, "summary": _ledger_summary(rows), "record_status": "saved_pregame"}


def ledger_calendar(db, month: str, league: str | None = None) -> dict[str, Any]:
    totals = {}
    dates = [d for d in slate_dates(db, month=month) if d.startswith(month + "-")]
    for day, rows in _ledger_days(db, dates, league).items():
        priced = [r for r in rows if r.get("units") is not None and r.get("result") in {"win", "loss", "push"}]
        if priced:
            totals[day] = round(sum(r["units"] for r in priced), 4)
    return {"month": month, "units": totals}


def ledger_year(db, year: int, league: str | None = None) -> dict[str, Any]:
    """Use the same corrected, verified records as the daily and monthly views."""
    rows, days = [], 0
    dates = [d for d in slate_dates(db) if d.startswith(f"{year}-")]
    for day_rows in _ledger_days(db, dates, league).values():
        if not day_rows:
            continue
        days += 1
        rows.extend(day_rows)
    summary = _ledger_summary(rows)
    labels = {"point_spread": "Spreads", "anytime_td": "Anytime TD", "receptions": "Receptions",
              "rushing_yards": "Rushing yards", "receiving_yards": "Receiving yards"}
    markets = []
    for market, label in labels.items():
        group = _ledger_summary([r for r in rows if r.get("market_id") == market])
        if group["win"] + group["loss"]:
            markets.append({"label": label, "win": group["win"], "loss": group["loss"],
                            "units": group["units"], "priced": group["priced_bets"]})
    return {"year": str(year), "days": days, **summary, "priced": summary["priced_bets"], "markets": markets}


def _ledger_days(db, dates, league):
    """Same verified records as ledger_day, with bounded intermediate card memory.

    Display-only packaged archives are deliberately absent from performance totals.
    There is no cross-request cache: official corrections apply on the next read.
    """
    dates = sorted(set(dates))
    result = {}
    # Detailed TD cards include substantial evidence. A whole year's raw cards
    # must not be resident for every simultaneous track-record request.
    for start in range(0, len(dates), 14):
        result.update(_ledger_range(db, dates[start:start + 14], league))
    return result


def _ledger_range(db, dates, league):
    from copy import deepcopy
    from sqlalchemy import select
    from spe.db.models import BoardSnapshot
    from spe.models.player_td.desk_replay import (
        day_card_key, _refresh_card_grades, _withhold_current_absences,
    )

    start, _ = _day_bounds(dates[0])
    _, end = _day_bounds(dates[-1])
    published = defaultdict(list)
    for row in _picks_in_range(db, league, start, end):
        if row.get("market_id") != "anytime_td":
            published[_slate_day(row.get("kickoff_utc"))].append(row)
    spreads = defaultdict(list)
    for row in ats_history(db, league, limit=None, start=start, end=end)["rows"]:
        spreads[_slate_day(row.get("kickoff_utc"))].append(row)

    keys = {day_card_key(day, league): day for day in dates}
    cards = {}
    for saved in db.scalars(select(BoardSnapshot).where(BoardSnapshot.key.in_(keys))):
        if isinstance(saved.payload, dict) and "rows" in saved.payload:
            cards[keys[saved.key]] = deepcopy(saved.payload)
    # Mutate the copied rows once so each day's record uses the same official data.
    combined = {"rows": [row for card in cards.values() for row in card["rows"]]}
    if combined["rows"]:
        _refresh_card_grades(db, combined)
    now = datetime.now(UTC)
    result = {}
    for day in dates:
        card = _withhold_current_absences(db, cards[day], now) if day in cards else {}
        result[day] = _ledger_rows([], [], published[day] + spreads[day] + _scorer_rows(card))
    return result


def _replay_scorers(db, date: str, league: str | None) -> list[dict[str, Any]]:
    """Every anytime touchdown the desk named for the day, including the ones that lost."""
    from spe.models.player_td.desk_replay import saved_day_card

    payload = saved_day_card(db, date, league or "all") or {}
    return _scorer_rows(payload)


def _scorer_rows(payload):
    rows = []
    for row in payload.get("rows") or []:
        if row.get("retrospective") is not False:
            continue
        result = {"scored": "win", "blank": "loss", "win": "win", "loss": "loss", "void": "void"}.get(str(row.get("result") or ""), "pending")
        if row.get("game_status") not in {"final", "post", "canceled", "cancelled"}:
            result = "pending"
        name = row.get("player_name") or "Anytime touchdown"
        rows.append(
            {
                "pick_id": row.get("outcome_id") or f"{row.get('game_id')}:{row.get('player_id')}",
                "game_id": row.get("game_id"),
                "market_id": "anytime_td",
                "league_id": row.get("league_id"),
                "player_name": name,
                "sentence": f"{name} anytime touchdown",
                "bookmaker": row.get("bookmaker"),
                "book_price": row.get("book_price"),
                "stake": bool(((row.get("decision_results") or {}).get("most_likely") or {}).get("bet_qualified")),
                "result": result,
                "kickoff_utc": row.get("kickoff_utc"),
            }
        )
        rows.extend(_secondary_scorers(row))
    return rows


def _secondary_scorers(row: dict[str, Any]) -> list[dict[str, Any]]:
    """The other scorer named on the same game, kept on the anytime record."""
    alt = ((row.get("decision") or {}).get("value_candidate")) or {}
    name = alt.get("name")
    if not name or alt.get("player_id") == row.get("player_id"):
        return []
    lane = ((row.get("decision_results") or {}).get("value_candidate")) or {}
    result = {"scored": "win", "blank": "loss", "win": "win", "loss": "loss", "void": "void"}.get(str(lane.get("result") or ""), "pending")
    if row.get("game_status") not in {"final", "post", "canceled", "cancelled"}:
        result = "pending"
    price = alt.get("american")
    return [
        {
            "pick_id": f"{row.get('game_id')}:anytime_td_secondary:{alt.get('player_id')}",
            "game_id": row.get("game_id"),
            "market_id": "anytime_td",
            "league_id": row.get("league_id"),
            "player_name": name,
            "sentence": f"{name} anytime touchdown",
            "bookmaker": alt.get("bookmaker"),
            "book_price": price,
            "stake": bool(lane.get("bet_qualified")),
            "result": result,
            "kickoff_utc": row.get("kickoff_utc"),
        }
    ]


def _column(item: Any) -> Any:
    if isinstance(item, (str, datetime)):
        return item
    try:
        return item[0]
    except (TypeError, KeyError, IndexError):
        return item


def _slate_day(value: Any) -> str:
    if isinstance(value, datetime):
        kick = as_utc(value)
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        if " " in text and "T" not in text:
            text = text.replace(" ", "T", 1)
        try:
            kick = as_utc(datetime.fromisoformat(text))
        except ValueError:
            return ""
    else:
        return ""
    return "" if kick is None else kick.astimezone(SLATE).date().isoformat()


def _ledger_mark(result: Any) -> str:
    if result in {"win", "correct", "scored"}:
        return "win"
    if result in {"loss", "miss", "blank"}:
        return "loss"
    if result in {"push", "void"}:
        return result
    return "pending"


def _ledger_rows(spreads: list[dict[str, Any]], prices: list[dict[str, Any]], published: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in spreads:
        final = ""
        if row.get("away_score") is not None and row.get("home_score") is not None and str(row.get("game_status") or "").lower() in {"final", "post"}:
            final = f"Final {row.get('away_score')}–{row.get('home_score')}"
        matchup = f"{row.get('away_team') or 'Away'} at {row.get('home_team') or 'Home'}"
        price = row.get("book_price")
        price_text = None if price is None else f"{'+' if int(price) > 0 else ''}{int(price)}"
        rows.append(
            {
                "pick_id": f"{row.get('game_id')}:spread",
                "game_id": row.get("game_id"),
                "market_id": "point_spread",
                "league_id": row.get("league_id") or "",
                "sentence": row.get("ats_pick_label") or "Spread",
                "detail": " · ".join(part for part in (matchup, price_text, final) if part),
                "result": _ledger_mark(row.get("ats_pick_result")),
                "kickoff_utc": row.get("kickoff_utc"),
                "units": _units(row.get("ats_pick_result"), price),
            }
        )
    for row in prices:
        price = row.get("book_price")
        price_text = None if price is None else f"{'+' if int(price) > 0 else ''}{int(price)}"
        actual = row.get("actual")
        actual_text = None if actual is None else f"Actual {actual:g}"
        rows.append(
            {
                "pick_id": str(row.get("outcome_id") or f"{row.get('game_id')}:{row.get('market_id')}:{row.get('player_id')}"),
                "game_id": row.get("game_id"),
                "market_id": row.get("market_id"),
                "league_id": row.get("league_id") or "",
                "sentence": row.get("pick") or row.get("player_name") or "Prop",
                "detail": " · ".join(part for part in (row.get("bookmaker"), price_text, actual_text) if part),
                "result": _ledger_mark(row.get("result")),
                "kickoff_utc": row.get("kickoff_utc"),
                "units": _units(row.get("result"), price),
            }
        )
    for row in published:
        price = row.get("book_price")
        price_text = None if price is None else f"{'+' if int(price) > 0 else ''}{int(price)}"
        actual = row.get("actual")
        actual_text = None if actual is None else f"Actual {actual:g}"
        staked = row.get("stake", True)
        result = _ledger_mark(row.get("result"))
        units = row.get("units") if row.get("units") is not None else (_units(row.get("result"), price) if staked else None)
        rows.append(
            {
                "pick_id": row.get("pick_id"),
                "game_id": row.get("game_id"),
                "market_id": row.get("market_id") or "anytime_td",
                "league_id": row.get("league_id") or "",
                "sentence": row.get("sentence") or row.get("player_name") or "Anytime touchdown",
                "detail": " · ".join(part for part in (row.get("bookmaker"), price_text, actual_text) if part),
                "result": result,
                "kickoff_utc": row.get("kickoff_utc"),
                "units": units,
            }
        )
    rows.sort(key=lambda row: (str(row.get("kickoff_utc") or ""), _MARKET_ORDER.get(str(row.get("market_id")), 9), str(row.get("sentence") or "")))
    return rows


def _units(result: Any, price: Any) -> float | None:
    import math
    mark = _ledger_mark(result)
    try:
        valid_price = price is not None and math.isfinite(float(price)) and abs(float(price)) >= 100
    except (TypeError, ValueError):
        valid_price = False
    if mark in {"pending", "void"} or not valid_price:
        return None
    if mark == "push":
        return 0.0
    if mark == "loss":
        return -1.0
    try:
        return american_to_decimal(int(price)) - 1
    except (TypeError, ValueError):
        return None


def _ledger_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {"win": 0, "loss": 0, "push": 0, "void": 0, "pending": 0, "units": 0.0}
    for row in rows:
        result = row.get("result") if row.get("result") in summary else "pending"
        summary[result] += 1
        if row.get("units") is not None and result in {"win", "loss", "push"}:
            summary["units"] += float(row["units"])
    staked = sum(r.get("units") is not None and r.get("result") in {"win", "loss", "push"} for r in rows)
    summary["priced_bets"] = staked
    summary["roi"] = summary["units"] / staked if staked else None
    summary["units"] = round(summary["units"], 4)
    return summary


def _day_bounds(date):
    start = datetime.fromisoformat(date).replace(tzinfo=SLATE)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def _result_query(db):
    ranked = db.query(PublishedCorrection, func.row_number().over(
        partition_by=PublishedCorrection.pick_id,
        order_by=(PublishedCorrection.settled_at.desc(), PublishedCorrection.correction_id.desc())
    ).label("rank")).subquery()
    correction = aliased(PublishedCorrection, ranked)
    return db.query(PublishedPick, PublishedResult, correction).outerjoin(
        PublishedResult, PublishedPick.pick_id == PublishedResult.pick_id
    ).outerjoin(correction, (PublishedPick.pick_id == correction.pick_id) & (ranked.c.rank == 1))


def day_picks(db, league=None, date=None):
    """Published props for one slate day, including ones whose price window has closed.

    The live card drops a pick when the game starts or the quote expires. This list
    keeps that same frozen pick up for the day and attaches the settled result.
    """
    if not date:
        return []
    start, end = _day_bounds(date)
    return _picks_in_range(db, league, start, end)


def _picks_in_range(db, league, start, end):
    query = _result_query(db)
    if league and league != "all":
        query = query.filter(PublishedPick.league_id == league)
    query = query.filter(PublishedPick.kickoff_utc >= start, PublishedPick.kickoff_utc < end)
    out = []
    for pick, original, correction in query.all():
        settled = correction or original
        kick = as_utc(pick.kickoff_utc)
        if kick is None:
            continue
        out.append(
            {
                **pick.snapshot,
                "pick_id": pick.pick_id,
                "published_at": as_utc(pick.published_at).isoformat(),
                "kickoff_utc": kick.isoformat(),
                "result": settled.result if settled else "pending",
                "actual": settled.actual if settled else None,
                "units": settled.units if settled and settled.result != "pending" else None,
            }
        )
    out.sort(key=lambda row: (str(row.get("kickoff_utc") or ""), str(row.get("market_id") or ""), str(row.get("player_name") or "")))
    return out


def current_picks(db, rows, now=None):
    """Only the originally published book, line and price can be a current call.

    Reobserving an unchanged price renews freshness, but never the frozen terms.
    New projections must still qualify; model revisions cannot rewrite the record.
    """
    if not rows:
        return []
    now = as_utc(now) or datetime.now(UTC)
    live = {(r.get("outcome_id"), r.get("bookmaker"), r.get("book_price")): r for r in rows}
    game_ids = {r.get("game_id") for r in rows}
    games = {g.game_id: g for g in db.query(Game).filter(Game.game_id.in_(game_ids or {"__none__"}))}
    out = []
    for record in (
        db.query(PublishedPick).filter(PublishedPick.game_id.in_(game_ids or {"__none__"})).all()
    ):
        old = record.snapshot if isinstance(record.snapshot, dict) else None
        published_at = as_utc(record.published_at)
        if old is None or published_at is None:
            continue
        row = live.get((old.get("outcome_id"), old.get("bookmaker"), old.get("book_price")))
        game = games.get(record.game_id)
        if (
            not game
            or game.status not in {"scheduled", "pre", "upcoming"}
            or as_utc(game.kickoff_utc) <= now
        ):
            continue
        if not row or not assess(row, now)["eligible"]:
            continue
        refreshed = {**old, "price_freshness": row.get("price_freshness")}
        qualification = assess(refreshed, now)
        if not qualification["eligible"]:
            continue
        out.append(
            {
                **old,
                "pick_id": record.pick_id,
                "published_at": published_at.isoformat(),
                "qualification": qualification,
            }
        )
    return out


def settle(db, now=None):
    now = as_utc(now) or datetime.now(UTC)
    rows = (
        db.query(PublishedPick)
        .filter((~db.query(PublishedResult.pick_id).filter(
                    PublishedResult.pick_id == PublishedPick.pick_id).exists()) |
                (PublishedPick.kickoff_utc >= now - timedelta(days=7)))
        .all()
    )
    written = pending = 0
    for pick in rows:
        game = db.get(Game, pick.game_id)
        if not game:
            pending += 1
            continue
        result, actual, evidence = None, None, {}
        if game.status in {"canceled", "cancelled"}:
            result, evidence = "void", {"source": "games.status", "reason": game.status}
        elif game.status in {"final", "post"}:
            snap = pick.snapshot
            stat = db.get(PublishedStat, f"{pick.game_id}|{snap.get('player_id')}|{pick.market_id}")
            if stat is None:
                pending += 1
                continue
            if stat.source in {"espn.summary.participation_unconfirmed", "espn.summary.absent"}:
                previous = resolved_result(db, pick.pick_id)
                if previous is not None and previous.result != "pending":
                    db.add(PublishedCorrection(pick_id=pick.pick_id, result="pending", actual=None, units=0,
                        settled_at=now, evidence={"source": stat.source, "reason": "participation_not_confirmed"}))
                    db.flush()
                    written += 1
                pending += 1
                continue
            actual = stat.actual
            result = "void" if stat.source == "espn.summary.confirmed_inactive" else _grade(snap["side"], snap.get("line"), actual, pick.market_id)
            if result == "void":
                actual = None
            evidence = {"source": stat.source, "observed_at": as_utc(stat.observed_at).isoformat()}
        if result not in {"win", "loss", "push", "void"}:
            pending += 1
            continue
        units = (
            american_to_decimal(pick.snapshot["book_price"]) - 1
            if result == "win"
            else (-1 if result == "loss" else 0)
        )
        previous = resolved_result(db, pick.pick_id)
        if previous is not None:
            if (previous.result, previous.actual, previous.units) != (result, actual, units):
                db.add(PublishedCorrection(pick_id=pick.pick_id, result=result, actual=actual,
                       units=units, settled_at=now, evidence={**evidence, "reason": "official_result_correction"}))
                db.flush()
                written += 1
            continue
        try:
            with db.begin_nested():
                db.add(
                    PublishedResult(
                        pick_id=pick.pick_id,
                        result=result,
                        actual=actual,
                        units=units,
                        settled_at=now,
                        evidence=evidence,
                    )
                )
                db.flush()
            written += 1
        except IntegrityError:
            if db.get(PublishedResult, pick.pick_id) is None:
                raise
    return {"status": "ok", "settled": written, "pending": pending}


def resolved_result(db, pick_id, original=None):
    correction = db.query(PublishedCorrection).filter_by(pick_id=pick_id).order_by(
        PublishedCorrection.settled_at.desc(), PublishedCorrection.correction_id.desc()).first()
    return correction or original or db.get(PublishedResult, pick_id)


def history(db, league=None, market=None, offset=0, limit=100):
    query = _result_query(db)
    if league and league != "all":
        query = query.filter(PublishedPick.league_id == league)
    if market and market != "all":
        query = query.filter(PublishedPick.market_id == market)
    # All matching records count toward totals, not just the visible page.
    groups = defaultdict(
        lambda: {"win": 0, "loss": 0, "push": 0, "void": 0, "pending": 0, "units": 0.0}
    )
    accuracy = defaultdict(list)
    daily = defaultdict(float)
    total = 0
    # Totals only need narrow scalar columns, not every historical JSON snapshot
    # and three ORM objects per selection. The visible page loads full cards below.
    correction = query.column_descriptions[2]["entity"]
    summary_rows = query.with_entities(PublishedPick.league_id, PublishedPick.market_id,
        PublishedPick.snapshot["p"].as_string(),
        func.coalesce(correction.result, PublishedResult.result, "pending"),
        func.coalesce(correction.units, PublishedResult.units, 0),
        func.coalesce(correction.settled_at, PublishedResult.settled_at))
    for lg, mk, probability, result, units, settled_at in summary_rows.yield_per(500):
        total += 1
        group = groups[(lg, mk)]
        group[result] += 1
        group["units"] += units
        if settled_at:
            daily[as_utc(settled_at).date().isoformat()] += units
            from spe.publishing.policy import finite
            probability = finite(probability)
            if result in {"win", "loss"} and probability is not None and 0 < probability < 1:
                accuracy[(lg, mk)].append((probability, int(result == "win")))
    summaries = []
    for (lg, mk), group in groups.items():
        staked = group["win"] + group["loss"] + group["push"]
        summaries.append(
            {
                "league": lg,
                "market": mk,
                **group,
                "roi": group["units"] / staked if staked else None,
                "accuracy": calibration_summary(accuracy[(lg, mk)]),
            }
        )
    records = []
    for pick, original, correction in (
        query.order_by(PublishedPick.published_at.desc(), PublishedPick.pick_id)
        .offset(offset)
        .limit(limit)
        .all()
    ):
        settled = correction or original
        records.append(
            {
                **pick.snapshot,
                "pick_id": pick.pick_id,
                "published_at": as_utc(pick.published_at).isoformat(),
                "result": settled.result if settled else "pending",
                "units": settled.units if settled and settled.result != "pending" else None,
                "settled_at": as_utc(settled.settled_at).isoformat() if settled else None,
            }
        )
    cumulative, series = 0.0, []
    for day, units in sorted(daily.items()):
        cumulative += units
        series.append({"date": day, "units": round(cumulative, 4)})
    return {
        "rows": records,
        "total": total,
        "offset": offset,
        "limit": limit,
        "summaries": summaries,
        "series": series,
        "methodology": "First qualifying prop per game, frozen before kickoff. One unit risked at the published price. ROI excludes voids and pending picks; pushes count as stakes. Missing stats stay pending. Research results, not proof of future returns.",
    }


def calibration_summary(pairs):
    """Score frozen probabilities only on binary, settled outcomes."""
    bins = []
    for low in range(0, 100, 10):
        values = [(p, y) for p, y in pairs if low / 100 <= p < (low + 10) / 100]
        if values:
            bins.append({"range": f"{low}–{low + 10}%", "count": len(values),
                         "predicted": sum(p for p, _ in values) / len(values),
                         "observed": sum(y for _, y in values) / len(values)})
    return {"graded": len(pairs),
            "brier_score": sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None,
            "predicted_hit_rate": sum(p for p, _ in pairs) / len(pairs) if pairs else None,
            "actual_hit_rate": sum(y for _, y in pairs) / len(pairs) if pairs else None,
            "bins": bins}


def ats_history(db, league=None, offset=0, limit=100, date=None, *, start=None, end=None):
    """Separate frozen ATS research history. Never reconstruct it from the board."""
    from spe.db.models import AtsPrediction, AtsSettlement
    from spe.models.ats.research_bundle import CLEAN_PROSPECTIVE_COHORT

    query = (
        db.query(AtsPrediction, AtsSettlement, Game)
        .join(Game, Game.game_id == AtsPrediction.game_id)
        .outerjoin(AtsSettlement, AtsSettlement.ats_id == AtsPrediction.ats_id)
        .filter(
            AtsPrediction.components["eligibility"].as_string() == "clean_prospective",
            AtsPrediction.components["cohort_version"].as_string() == CLEAN_PROSPECTIVE_COHORT,
            AtsPrediction.predicted_at < Game.kickoff_utc,
            (AtsPrediction.price_american >= 100) | (AtsPrediction.price_american <= -100),
        )
    )
    if league and league != "all":
        query = query.filter(AtsPrediction.league_id == league)
    if date:
        start, end = _day_bounds(date)
    if start is not None and end is not None:
        query = query.filter(Game.kickoff_utc >= start, Game.kickoff_utc < end)
    records, groups = (
        [],
        defaultdict(
            lambda: {"win": 0, "loss": 0, "push": 0, "void": 0, "pending": 0, "units": 0.0}
        ),
    )
    seen = set()
    for pred, settled, game in query.order_by(
        AtsPrediction.predicted_at, AtsPrediction.ats_id
    ).yield_per(500):
        meta = pred.components or {}
        if (
            meta.get("eligibility") != "clean_prospective"
            or meta.get("cohort_version") != CLEAN_PROSPECTIVE_COHORT
            or as_utc(pred.predicted_at) >= as_utc(game.kickoff_utc)
            or pred.price_american is None
            or abs(pred.price_american) < 100
            or pred.game_id in seen
        ):
            continue
        seen.add(pred.game_id)
        result = ("win" if settled.result == "cover" else settled.result) if settled else "pending"
        units = (
            (
                american_to_decimal(pred.price_american) - 1
                if result == "win"
                else -1
                if result == "loss"
                else 0
            )
            if settled
            else None
        )
        group = groups[pred.league_id]
        group[result] += 1
        group["units"] += units or 0
        records.append(
            {
                "pick_id": pred.ats_id,
                "game_id": pred.game_id,
                "market_id": "point_spread",
                "league_id": pred.league_id,
                "player_name": pred.home_team_id if pred.side == "home" else pred.away_team_id,
                "sentence": f"{pred.home_team_id if pred.side == 'home' else pred.away_team_id} {pred.spread_line:+g}",
                "book_price": pred.price_american,
                "bookmaker": pred.bookmaker_id,
                "p": pred.p_cover,
                "published_at": as_utc(pred.predicted_at).isoformat(),
                "kickoff_utc": as_utc(game.kickoff_utc).isoformat(),
                "result": result,
                "units": units,
                "forecast": {"model_version": pred.model_version_id},
            }
        )
    summaries = []
    for lg, group in groups.items():
        staked = group["win"] + group["loss"] + group["push"]
        summaries.append(
            {
                "league": lg,
                "market": "point_spread",
                **group,
                "roi": group["units"] / staked if staked else None,
            }
        )
    return {
        "rows": list(reversed(records))[offset : None if limit is None else offset + limit],
        "summaries": summaries,
        "total": len(records),
        "methodology": "Frozen ATS research predictions, separate from published prop opportunities. First eligible freeze per game in the current research cohort. Original line and price; one unit risked. Legacy and consensus-only records excluded. These are research results, not a verified betting edge.",
    }
