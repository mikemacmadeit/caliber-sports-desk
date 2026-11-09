"""Settle shadow predictions against official finals. Missing stats are not zeros."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.db.base import as_utc, new_id
from spe.db.models import ShadowPrediction, ShadowSettlement
from spe.market.odds import american_to_decimal, american_to_implied


def settle_shadow_predictions(
    session: Session,
    now: datetime | None = None,
    shadow_ids: set[str] | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    summary: dict[str, Any] = {
        "settled": 0,
        "already_settled": 0,
        "blocked": 0,
        "pending": 0,
        "by_result": {},
        "blockers": {},
    }
    # Terminal results stay immutable. Blocked rows may be retried when box scores appear.
    terminal = {
        r.shadow_id
        for r in session.query(ShadowSettlement).filter(ShadowSettlement.result.in_(("win", "loss", "void", "push"))).all()
    }
    blocked_ids = {
        r.shadow_id
        for r in session.query(ShadowSettlement).filter(ShadowSettlement.result == "blocked").all()
    }
    query = session.query(ShadowPrediction)
    if shadow_ids is not None:
        query = query.filter(ShadowPrediction.shadow_id.in_(shadow_ids or ["__none__"]))
    open_rows = [row for row in query.all() if row.shadow_id not in terminal]
    summary["blocked_retry_candidates"] = len(blocked_ids)
    if not open_rows:
        summary["status"] = "nothing_open"
        return summary

    game_ids = {row.game_id for row in open_rows}
    from sqlalchemy import bindparam

    games = {
        r["game_id"]: r
        for r in session.execute(
            text("SELECT game_id, status, kickoff_utc, league_id FROM games WHERE game_id IN :games").bindparams(
                bindparam("games", expanding=True)
            ),
            {"games": list(game_ids)},
        ).mappings().all()
    }
    finals = {gid for gid, game in games.items() if str(game["status"]).lower() in {"final", "post"}}
    stats = _player_stats(session, finals)
    injuries = _inactive_before_kickoff(session, finals)

    for row in open_rows:
        game = games.get(row.game_id)
        if game is None or row.game_id not in finals:
            summary["pending"] += 1
            continue
        key = (row.game_id, row.player_id)
        if key in injuries:
            _store(
                session,
                row,
                result="void",
                actual=None,
                now=now,
                source="injury_report_pregame",
                void_reason="confirmed_inactive_pregame",
                evidence={"designation": injuries[key]},
                summary=summary,
            )
            continue
        if key not in stats:
            was_new = session.query(ShadowSettlement).filter(ShadowSettlement.shadow_id == row.shadow_id).one_or_none() is None
            _store(
                session,
                row,
                result="blocked",
                actual=None,
                now=now,
                source="player_game_stats",
                void_reason="missing_box_score_not_assumed_zero",
                evidence={"blocker": "MISSING_PLAYER_STAT_ROW"},
                summary=summary,
            )
            if was_new:
                summary["blocked"] += 1
                summary["blockers"]["MISSING_PLAYER_STAT_ROW"] = int(summary["blockers"].get("MISSING_PLAYER_STAT_ROW", 0)) + 1
            continue
        actual = _actual(stats[key], row.market_id)
        if actual is None:
            was_new = session.query(ShadowSettlement).filter(ShadowSettlement.shadow_id == row.shadow_id).one_or_none() is None
            _store(
                session,
                row,
                result="blocked",
                actual=None,
                now=now,
                source="player_game_stats",
                void_reason="market_stat_unavailable",
                evidence={"blocker": "MARKET_STAT_UNAVAILABLE"},
                summary=summary,
            )
            if was_new:
                summary["blocked"] += 1
            continue
        result = _grade(row.side, row.line, actual, row.market_id)
        close = _closing_price(session, row)
        clv = None
        if close is not None:
            clv = american_to_implied(close) - american_to_implied(row.price_american)
        stake = _flat_return(result, row.price_american)
        _store(
            session,
            row,
            result=result,
            actual=actual,
            now=now,
            source="player_game_stats",
            void_reason=None,
            evidence={"stat_row": True, "market": row.market_id},
            summary=summary,
            flat=stake,
            close=close,
            clv=clv,
        )
    session.flush()
    summary["status"] = "ok"
    return summary


def _player_stats(session: Session, game_ids: set[str]) -> dict[tuple[str, str], dict[str, float]]:
    if not game_ids:
        return {}
    from sqlalchemy import bindparam

    rows = session.execute(
        text(
            """
            SELECT game_id, player_id, targets, receptions, rec_yards, rush_yards, carries, rush_td, rec_td
            FROM player_game_stats
            WHERE game_id IN :games
            """
        ).bindparams(bindparam("games", expanding=True)),
        {"games": list(game_ids)},
    ).mappings().all()
    return {
        (row["game_id"], row["player_id"]): {
            "targets": float(row["targets"] or 0),
            "receptions": float(row["receptions"] or 0),
            "rec_yards": float(row["rec_yards"] or 0),
            "rush_yards": float(row["rush_yards"] or 0),
            "carries": float(row["carries"] or 0),
            "anytime_tds": float((row["rush_td"] or 0) + (row["rec_td"] or 0)),
        }
        for row in rows
    }


def _inactive_before_kickoff(session: Session, game_ids: set[str]) -> dict[tuple[str, str], str]:
    if not game_ids:
        return {}
    from sqlalchemy import bindparam

    rows = session.execute(
        text(
            """
            SELECT i.game_id, i.player_id, i.designation, i.reported_at, g.kickoff_utc
            FROM injury_events i
            JOIN games g ON g.game_id = i.game_id
            WHERE i.game_id IN :games AND i.reported_at < g.kickoff_utc
            """
        ).bindparams(bindparam("games", expanding=True)),
        {"games": list(game_ids)},
    ).mappings().all()
    latest: dict[tuple[str, str], tuple[datetime, str]] = {}
    for row in rows:
        label = str(row["designation"] or "").strip().lower()
        if label not in {"out", "ir", "injured reserve", "pup"}:
            continue
        key = (row["game_id"], row["player_id"])
        reported = as_utc(row["reported_at"]) or row["reported_at"]
        current = latest.get(key)
        if current is None or reported >= current[0]:
            latest[key] = (reported, str(row["designation"]))
    return {key: value[1] for key, value in latest.items()}


def _actual(stats: dict[str, float], market_id: str) -> float | None:
    mapping = {
        "anytime_td": "anytime_tds",
        "receptions": "receptions",
        "opportunity": "targets",
        "receiving_yards": "rec_yards",
        "rushing_yards": "rush_yards",
    }
    key = mapping.get(market_id)
    if key is None:
        return None
    return float(stats[key])


def _grade(side: str, line: float | None, actual: float, market_id: str) -> str:
    if market_id == "anytime_td":
        hit = actual > 0
        if side in {"yes", "over"}:
            return "win" if hit else "loss"
        return "win" if not hit else "loss"
    if line is None:
        return "blocked"
    if abs(actual - float(line)) < 1e-9:
        return "push"
    over = actual > float(line)
    if side == "over":
        return "win" if over else "loss"
    if side == "under":
        return "win" if not over else "loss"
    return "blocked"


def _flat_return(result: str, american: int) -> float | None:
    if result == "push":
        return 0.0
    if result == "void" or result == "blocked":
        return None
    if result == "loss":
        return -1.0
    return american_to_decimal(american) - 1.0


def _closing_price(session: Session, row: ShadowPrediction) -> int | None:
    tick = session.execute(
        text(
            """
            SELECT price_american
            FROM odds_ticks
            WHERE outcome_id = :oid
            ORDER BY captured_at DESC
            LIMIT 1
            """
        ),
        {"oid": row.outcome_id},
    ).first()
    return int(tick[0]) if tick else None


def _store(
    session: Session,
    row: ShadowPrediction,
    *,
    result: str,
    actual: float | None,
    now: datetime,
    source: str,
    void_reason: str | None,
    evidence: dict[str, Any],
    summary: dict[str, Any],
    flat: float | None = None,
    close: int | None = None,
    clv: float | None = None,
) -> None:
    existing = session.query(ShadowSettlement).filter(ShadowSettlement.shadow_id == row.shadow_id).one_or_none()
    flat_val = flat if flat is not None else _flat_return(result, row.price_american)
    if existing is not None:
        # Only blocked → terminal upgrades are allowed (idempotent retry).
        if existing.result != "blocked":
            summary["already_settled"] += 1
            return
        if result == "blocked":
            summary["already_settled"] += 1
            return
        prior = existing.result
        existing.result = result
        existing.actual_value = actual
        existing.settled_at = now
        existing.official_source = source
        existing.void_reason = void_reason
        existing.evidence = {**(existing.evidence or {}), **evidence, "retried_from": prior}
        existing.flat_stake_return = flat_val
        existing.closing_price_american = close
        existing.closing_line_value = clv
        summary["settled"] += 1
        summary["retried_from_blocked"] = int(summary.get("retried_from_blocked", 0)) + 1
        summary["by_result"][result] = int(summary["by_result"].get(result, 0)) + 1
        return
    session.add(
        ShadowSettlement(
            settlement_id=new_id(),
            shadow_id=row.shadow_id,
            result=result,
            actual_value=actual,
            settled_at=now,
            official_source=source,
            void_reason=void_reason,
            evidence=evidence,
            flat_stake_return=flat_val,
            closing_price_american=close,
            closing_line_value=clv,
        )
    )
    summary["settled"] += 1
    summary["by_result"][result] = int(summary["by_result"].get(result, 0)) + 1
