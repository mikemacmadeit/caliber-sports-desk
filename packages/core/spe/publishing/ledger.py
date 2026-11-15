"""Publication is an insert, never a recalculation of history."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from collections import defaultdict
from typing import Any
from zoneinfo import ZoneInfo
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from spe.db.base import as_utc, new_id
from spe.db.models import PublishedPick, PublishedResult, Game, PublishedStat
from spe.market.odds import american_to_decimal
from spe.props.game_picks import build_game_prop_picks
from spe.publishing.policy import assess
from spe.shadow.settle import _grade


@event.listens_for(PublishedPick, "before_update")
@event.listens_for(PublishedPick, "before_delete")
@event.listens_for(PublishedResult, "before_update")
@event.listens_for(PublishedResult, "before_delete")
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
        if (
            db.query(PublishedPick)
            .filter_by(game_id=game.game_id, market_id="anytime_td")
            .first()
        ):
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
            if (
                not db.query(PublishedPick)
                .filter_by(game_id=game.game_id, market_id="anytime_td")
                .first()
            ):
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
        if (
            db.query(PublishedPick)
            .filter_by(game_id=pick["game_id"], market_id=pick["market_id"])
            .first()
        ):
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
            # Another worker already published this game/market. Never replace it.
            if (
                not db.query(PublishedPick)
                .filter_by(game_id=game.game_id, market_id=pick["market_id"])
                .first()
            ):
                raise
    return {"status": "ok", "published": written, "eligible": len(selected)}


SLATE = ZoneInfo("America/Chicago")
_MARKET_ORDER = {"point_spread": 0, "anytime_td": 1, "receptions": 2, "rushing_yards": 3, "receiving_yards": 4}


def slate_dates(db) -> list[str]:
    """Days that already have a saved card, a frozen pick, or a spread on the board."""
    from spe.db.models import BoardSnapshot

    found: set[str] = set()
    for item in db.query(BoardSnapshot.key):
        parts = str(_column(item)).split("|")
        if len(parts) == 3 and len(parts[2]) == 10 and parts[2][4] == "-" and parts[2][7] == "-":
            found.add(parts[2])
    for item in db.query(PublishedPick.kickoff_utc):
        day = _slate_day(_column(item))
        if day:
            found.add(day)
    try:
        from spe.models.ats.board_cache import read_board

        board = read_board() or {}
        for row in board.get("rows") or []:
            if isinstance(row, dict):
                day = _slate_day(row.get("kickoff_utc"))
                if day:
                    found.add(day)
    except Exception:
        pass
    return sorted(found)


def ledger_day(db, date: str, league: str | None = None) -> dict[str, Any]:
    """Desk spreads, prop calls, and every anytime touchdown named for the day."""
    from spe.desk.best_bets import build_best_bets
    from spe.desk.day_results import apply_recorded_results
    from spe.props.game_picks import load_desk_props

    record_frozen_desk_picks(db)
    from spe.publishing.boxes import ingest as ingest_boxes

    ingest_boxes(db)
    settle(db)
    card = build_best_bets(league=league or "all", date=date)
    props = load_desk_props(db, league or "all", date)
    spreads = card.get("spreads") or []
    apply_recorded_results(db, spreads, props)
    rows = _ledger_rows(spreads, props, _replay_scorers(db, date, league))
    return {
        "date": date,
        "timezone": "America/Chicago",
        "league": league or "all",
        "rows": rows,
        "summary": _ledger_summary(rows),
    }


def _replay_scorers(db, date: str, league: str | None) -> list[dict[str, Any]]:
    """Every anytime touchdown the desk named for the day, including the ones that lost."""
    from spe.models.player_td.desk_replay import replay_day

    payload = replay_day(db, date, league or "all", cached_only=True)
    rows = []
    for row in payload.get("rows") or []:
        result = {"scored": "win", "blank": "loss"}.get(str(row.get("result") or ""), "pending")
        name = row.get("player_name") or "Anytime touchdown"
        rows.append(
            {
                "pick_id": row.get("outcome_id") or f"{row.get('game_id')}:{row.get('player_id')}",
                "game_id": row.get("game_id"),
                "league_id": row.get("league_id"),
                "player_name": name,
                "sentence": f"{name} anytime touchdown",
                "bookmaker": row.get("bookmaker"),
                "book_price": row.get("book_price"),
                "result": result,
                "kickoff_utc": row.get("kickoff_utc"),
            }
        )
    return rows


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
    if result == "push":
        return "push"
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
        rows.append(
            {
                "pick_id": row.get("pick_id"),
                "game_id": row.get("game_id"),
                "market_id": "anytime_td",
                "league_id": row.get("league_id") or "",
                "sentence": row.get("sentence") or row.get("player_name") or "Anytime touchdown",
                "detail": " · ".join(part for part in (row.get("bookmaker"), price_text, actual_text) if part),
                "result": _ledger_mark(row.get("result")),
                "kickoff_utc": row.get("kickoff_utc"),
                "units": row.get("units") if row.get("units") is not None else _units(row.get("result"), price),
            }
        )
    rows.sort(key=lambda row: (str(row.get("kickoff_utc") or ""), _MARKET_ORDER.get(str(row.get("market_id")), 9), str(row.get("sentence") or "")))
    return rows


def _units(result: Any, price: Any) -> float | None:
    mark = _ledger_mark(result)
    if mark == "pending" or price is None:
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
    summary = {"win": 0, "loss": 0, "push": 0, "pending": 0, "units": 0.0}
    for row in rows:
        result = row.get("result") if row.get("result") in summary else "pending"
        summary[result] += 1
        if row.get("units") is not None and result in {"win", "loss", "push"}:
            summary["units"] += float(row["units"])
    staked = summary["win"] + summary["loss"] + summary["push"]
    summary["roi"] = summary["units"] / staked if staked else None
    summary["units"] = round(summary["units"], 4)
    return summary


def day_picks(db, league=None, date=None):
    """Published props for one slate day, including ones whose price window has closed.

    The live card drops a pick when the game starts or the quote expires. This list
    keeps that same frozen pick up for the day and attaches the settled result.
    """
    if not date:
        return []
    query = db.query(PublishedPick, PublishedResult).outerjoin(
        PublishedResult, PublishedPick.pick_id == PublishedResult.pick_id
    )
    if league and league != "all":
        query = query.filter(PublishedPick.league_id == league)
    out = []
    for pick, settled in query.all():
        kick = as_utc(pick.kickoff_utc)
        if kick is None or kick.astimezone(SLATE).date().isoformat() != date:
            continue
        out.append(
            {
                **pick.snapshot,
                "pick_id": pick.pick_id,
                "published_at": as_utc(pick.published_at).isoformat(),
                "kickoff_utc": kick.isoformat(),
                "result": settled.result if settled else "pending",
                "actual": settled.actual if settled else None,
                "units": settled.units if settled else None,
            }
        )
    out.sort(key=lambda row: (str(row.get("kickoff_utc") or ""), str(row.get("market_id") or ""), str(row.get("player_name") or "")))
    return out


def current_picks(db, rows, now=None):
    """Only the originally published book, line and price can be a current call.

    Reobserving an unchanged price renews freshness, but never the frozen terms.
    New projections must still qualify; model revisions cannot rewrite the record.
    """
    now = as_utc(now) or datetime.now(UTC)
    live = {(r.get("outcome_id"), r.get("bookmaker"), r.get("book_price")): r for r in rows}
    game_ids = {r.get("game_id") for r in rows}
    out = []
    for record in (
        db.query(PublishedPick).filter(PublishedPick.game_id.in_(game_ids or {"__none__"})).all()
    ):
        old = record.snapshot
        row = live.get((old.get("outcome_id"), old.get("bookmaker"), old.get("book_price")))
        game = db.get(Game, record.game_id)
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
                "published_at": as_utc(record.published_at).isoformat(),
                "qualification": qualification,
            }
        )
    return out


def settle(db, now=None):
    now = as_utc(now) or datetime.now(UTC)
    rows = (
        db.query(PublishedPick)
        .filter(~PublishedPick.pick_id.in_(db.query(PublishedResult.pick_id)))
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
            actual = stat.actual
            result = _grade(snap["side"], snap.get("line"), actual, pick.market_id)
            evidence = {"source": stat.source, "observed_at": as_utc(stat.observed_at).isoformat()}
        if result not in {"win", "loss", "push", "void"}:
            pending += 1
            continue
        units = (
            american_to_decimal(pick.snapshot["book_price"]) - 1
            if result == "win"
            else (-1 if result == "loss" else 0)
        )
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


def history(db, league=None, market=None, offset=0, limit=100):
    query = db.query(PublishedPick, PublishedResult).outerjoin(
        PublishedResult, PublishedPick.pick_id == PublishedResult.pick_id
    )
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
    for pick, settled in query.yield_per(500):
        total += 1
        group = groups[(pick.league_id, pick.market_id)]
        group[settled.result if settled else "pending"] += 1
        group["units"] += settled.units if settled else 0
        if settled:
            daily[as_utc(settled.settled_at).date().isoformat()] += settled.units
            from spe.publishing.policy import finite
            probability = finite(pick.snapshot.get("p"))
            if settled.result in {"win", "loss"} and probability is not None and 0 < probability < 1:
                accuracy[(pick.league_id, pick.market_id)].append((probability, int(settled.result == "win")))
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
    for pick, settled in (
        query.order_by(PublishedPick.published_at.desc(), PublishedPick.pick_id)
        .offset(offset)
        .limit(limit)
        .all()
    ):
        records.append(
            {
                **pick.snapshot,
                "pick_id": pick.pick_id,
                "published_at": as_utc(pick.published_at).isoformat(),
                "result": settled.result if settled else "pending",
                "units": settled.units if settled else None,
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
        "methodology": "First qualifying prop per game and market, frozen before kickoff. One unit risked at the published price. ROI excludes voids and pending picks; pushes count as stakes. Missing stats stay pending. Research results, not proof of future returns.",
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


def ats_history(db, league=None, offset=0, limit=100):
    """Separate frozen ATS research history. Never reconstruct it from the board."""
    from spe.db.models import AtsPrediction, AtsSettlement
    from spe.models.ats.research_bundle import CLEAN_PROSPECTIVE_COHORT

    query = (
        db.query(AtsPrediction, AtsSettlement, Game)
        .join(Game, Game.game_id == AtsPrediction.game_id)
        .outerjoin(AtsSettlement, AtsSettlement.ats_id == AtsPrediction.ats_id)
    )
    if league and league != "all":
        query = query.filter(AtsPrediction.league_id == league)
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
        "rows": list(reversed(records))[offset : offset + limit],
        "summaries": summaries,
        "total": len(records),
        "methodology": "Frozen ATS research predictions, separate from published prop opportunities. First eligible freeze per game in the current research cohort. Original line and price; one unit risked. Legacy and consensus-only records excluded. These are research results, not a verified betting edge.",
    }
