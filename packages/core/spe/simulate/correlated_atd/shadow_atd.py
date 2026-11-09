"""Prospective shadow capture for M6 + atd_sim_v1 Anytime TD.

Append-only into ShadowPrediction. Never rewrites existing M3/M4 shadow rows.
Recommendations remain research-only / no_bet.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from spe.db.base import as_utc, new_id
from spe.db.models import Game, ShadowPrediction
from spe.market.odds import american_to_implied, ev_per_dollar, no_vig_two_way
from spe.simulate.correlated_atd.examples import simulate_game_example
from spe.simulate.correlated_atd.phase5_isolation import PHASE_VERSION
from spe.simulate.correlated_atd.player_weights import load_m6, _m6_predict_simple
from spe.simulate.correlated_atd.team_adapter import TeamTdV1Provider, upcoming_games


def capture_atd_sim_shadow(
    session: Session,
    *,
    now: datetime | None = None,
    horizon_hours: int = 24 * 10,
    limit_per_league: int | None = None,
    draw_count: int = 4_000,
) -> dict[str, Any]:
    """Freeze sim + M6 probs beside live anytime_td odds before kickoff."""
    now = now or datetime.now(UTC)
    summary: dict[str, Any] = {
        "written": 0,
        "unchanged": 0,
        "skipped": 0,
        "m6": 0,
        "atd_sim_v1": 0,
        "games": 0,
        "play_enabled": False,
        "research_only": True,
        "validated_play": False,
        "recommendation_status": "no_bet",
        "phase_version": PHASE_VERSION,
    }

    priced = _priced_anytime_rows(session)
    if not priced:
        summary["status"] = "no_priced_anytime_td"
        return summary

    existing = {
        (r.outcome_id, r.model_version_id, r.odds_tick_id)
        for r in session.query(ShadowPrediction)
        .filter(ShadowPrediction.market_id == "anytime_td")
        .filter(
            or_(
                ShadowPrediction.model_version_id.like("m6-%"),
                ShadowPrediction.model_version_id.like("atd_sim_v1-%"),
            )
        )
        .all()
    }

    provider = TeamTdV1Provider(session)
    games = upcoming_games(session, horizon_hours=horizon_hours, limit_per_league=limit_per_league)
    # Also include any upcoming game that has priced anytime TD even if outside adapter limit.
    priced_game_ids = {r["game_id"] for r in priced.values()}
    have = {g.game_id for g in games}
    missing_ids = priced_game_ids - have
    if missing_ids:
        extra = session.query(Game).filter(Game.game_id.in_(list(missing_ids))).all()
        games.extend(extra)

    by_game: dict[str, list[dict[str, Any]]] = {}
    for row in priced.values():
        by_game.setdefault(row["game_id"], []).append(row)

    for game in games:
        rows = by_game.get(game.game_id) or []
        if not rows:
            continue
        kickoff = as_utc(game.kickoff_utc)
        if kickoff is not None and kickoff <= now:
            summary["skipped"] += len(rows)
            continue
        summary["games"] += 1
        example = simulate_game_example(session, game, provider, draw_count=draw_count, seed=42)
        sim_by_pid = {p["player_id"]: p for p in example.get("players") or []}
        m6_art = load_m6(game.league_id)

        for row in rows:
            pid = row["player_id"]
            tick_id = row["tick_id"]
            pos = row.get("position_group") or "WR"

            # M6
            m6_version = f"m6-{game.league_id}-anytime_td"
            key_m6 = (row["outcome_id"], m6_version, tick_id)
            if key_m6 in existing:
                summary["unchanged"] += 1
            else:
                p_m6 = None
                mean_m6 = None
                if m6_art:
                    p_m6, mean_m6 = _m6_predict_simple(m6_art, pid, pos)
                if p_m6 is None and pid in sim_by_pid:
                    p_m6 = sim_by_pid[pid].get("m6_p")
                if p_m6 is not None:
                    _append_shadow(
                        session,
                        row=row,
                        model_version_id=m6_version,
                        p=float(p_m6),
                        family="m6",
                        now=now,
                        components_extra={"mean": mean_m6, "phase": 5, "research_only": True},
                    )
                    summary["written"] += 1
                    summary["m6"] += 1
                    existing.add(key_m6)
                else:
                    summary["skipped"] += 1

            # Simulator
            sim_version = f"atd_sim_v1-{game.league_id}-anytime_td"
            key_sim = (row["outcome_id"], sim_version, tick_id)
            if key_sim in existing:
                summary["unchanged"] += 1
                continue
            sim_row = sim_by_pid.get(pid)
            if not sim_row:
                summary["skipped"] += 1
                continue
            _append_shadow(
                session,
                row=row,
                model_version_id=sim_version,
                p=float(sim_row["p_anytime_td"]),
                family="atd_sim_v1",
                now=now,
                components_extra={
                    "mean_tds": sim_row.get("mean_tds"),
                    "p_rush_td": sim_row.get("p_rush_td"),
                    "p_rec_td": sim_row.get("p_rec_td"),
                    "m6_p": sim_row.get("m6_p"),
                    "delta_vs_m6": sim_row.get("delta_vs_m6"),
                    "phase": 5,
                    "research_only": True,
                    "sim_seed": 42,
                    "draw_count": draw_count,
                },
            )
            summary["written"] += 1
            summary["atd_sim_v1"] += 1
            existing.add(key_sim)

    session.flush()
    summary["status"] = "ok"
    return summary


def _priced_anytime_rows(session: Session) -> dict[str, dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT o.outcome_id, o.game_id, o.player_id, o.market_id, o.side, o.line,
                   g.league_id, g.status, g.kickoff_utc,
                   t.tick_id, t.bookmaker_id, t.price_american, t.captured_at,
                   pl.position_group, pl.name
            FROM outcomes o
            JOIN games g ON g.game_id = o.game_id
            JOIN odds_ticks t ON t.outcome_id = o.outcome_id
            LEFT JOIN players pl ON pl.player_id = o.player_id
            WHERE g.data_tier = 'upcoming'
              AND g.status IN ('scheduled', 'preview', 'pregame')
              AND g.league_id IN ('nfl', 'fbs')
              AND o.market_id = 'anytime_td'
              AND o.side IN ('yes', 'over')
              AND o.player_id IS NOT NULL
            """
        )
    ).mappings().all()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        oid = row["outcome_id"]
        prev = latest.get(oid)
        if prev is None or _as_dt(row["captured_at"]) >= _as_dt(prev["captured_at"]):
            latest[oid] = dict(row)
    return latest


def _append_shadow(
    session: Session,
    *,
    row: dict[str, Any],
    model_version_id: str,
    p: float,
    family: str,
    now: datetime,
    components_extra: dict[str, Any] | None = None,
) -> None:
    price = int(row["price_american"])
    implied_raw = american_to_implied(price)
    opposing = _opposing_price(session, row["outcome_id"], row["bookmaker_id"], row["side"])
    novig = None
    if opposing is not None:
        try:
            a, _b = no_vig_two_way(implied_raw, american_to_implied(opposing))
            novig = float(a)
        except ValueError:
            novig = None
    ev = ev_per_dollar(p, price)
    reasons = [
        "RESEARCH_ONLY",
        "PLAY_DISABLED",
        "SHADOW_TRACK",
        "PHASE5_ATD_SIM",
        "VALIDATED_PLAY_DISABLED",
    ]
    if ev <= 0:
        reasons.append("NEGATIVE_EXPECTED_VALUE")
    if novig is None:
        reasons.append("YES_ONLY_NOVIG_UNIDENTIFIED")
    session.add(
        ShadowPrediction(
            shadow_id=new_id(),
            player_id=row["player_id"],
            game_id=row["game_id"],
            league_id=row["league_id"],
            market_id="anytime_td",
            outcome_id=row["outcome_id"],
            bookmaker_id=row["bookmaker_id"],
            line=None if row["line"] is None else float(row["line"]),
            side=row["side"],
            price_american=price,
            odds_tick_id=row["tick_id"],
            forecast_probability=float(p),
            market_implied_raw=float(implied_raw),
            market_implied_novig=novig,
            expected_value=float(ev),
            model_version_id=model_version_id,
            forecast_as_of=now,
            data_quality_status="research",
            abstain_reasons=reasons,
            recommendation_status="no_bet",
            components={
                "family": family,
                "position_group": row.get("position_group"),
                "player_name": row.get("name"),
                "market_implied_raw": implied_raw,
                "market_implied_novig": novig,
                "novig_identified": novig is not None,
                "play_enabled": False,
                "line_source": "sportsbook",
                **(components_extra or {}),
            },
            research_only=True,
            play_enabled=False,
            created_at=now,
        )
    )


def _opposing_price(session: Session, outcome_id: str, bookmaker_id: str, side: str) -> int | None:
    want = {"over": "under", "under": "over", "yes": "no", "no": "yes"}.get(side)
    if not want:
        return None
    row = session.execute(
        text(
            """
            SELECT t.price_american
            FROM outcomes o
            JOIN odds_ticks t ON t.outcome_id = o.outcome_id
            WHERE o.game_id = (SELECT game_id FROM outcomes WHERE outcome_id = :oid)
              AND o.player_id = (SELECT player_id FROM outcomes WHERE outcome_id = :oid)
              AND o.market_id = (SELECT market_id FROM outcomes WHERE outcome_id = :oid)
              AND o.side = :side
              AND t.bookmaker_id = :book
            ORDER BY t.captured_at DESC
            LIMIT 1
            """
        ),
        {"oid": outcome_id, "side": want, "book": bookmaker_id},
    ).first()
    return int(row[0]) if row else None


def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return as_utc(value) or value
    if isinstance(value, str):
        return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00"))) or datetime.now(UTC)
    return datetime.now(UTC)
