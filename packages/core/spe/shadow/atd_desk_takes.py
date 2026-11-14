"""Anytime TD desk takes — one scorer per game.

The Desk requires a supported scoring role and a current price at least four
probability points below the estimate, then ranks by modeled scoring chance.

Validated Play stays off. These are research takes for scoring only.
"""

from __future__ import annotations

import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, attributes

from spe.db.base import as_utc, new_id
from spe.db.models import Game, ShadowPrediction, ShadowSettlement
from spe.market.odds import american_to_implied, ev_per_dollar

DESK_TAKE_KIND = "game_scorer"
VALUE_HAIRCUT = 0.04  # probability-point margin, not an EV haircut
SCORE_CHANCE_FLOOR = 0.22

# Weights for the composite desk score (must sum ~1).
W_CHANCE = 0.35
W_EDGE = 0.40
W_FRESH = 0.15
W_AVAIL = 0.10


def _ensure_api_path() -> None:
    # .../packages/core/spe/shadow/atd_desk_takes.py → repo root is parents[4]
    root = Path(__file__).resolve().parents[4]
    api = root / "services" / "api"
    api_s = str(api)
    if api.exists() and api_s not in sys.path:
        sys.path.insert(0, api_s)


def list_anytime_prop_rows(session: Session, *, league: str = "all") -> list[dict[str, Any]]:
    _ensure_api_path()
    from app.queries import list_props

    return list_props(session, market="anytime_td", min_odds=None, min_ev=None, league=league, date=None)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _edge_component(ev_hc: float | None) -> float:
    """Map haircut EV into 0..1. 0 EV → 0.5; +10% → ~0.88; -10% → ~0.12."""
    if ev_hc is None:
        return 0.35
    return _clamp01(1.0 / (1.0 + math.exp(-12.0 * float(ev_hc))))


def _fresh_component(row: dict[str, Any]) -> float:
    forecast = row.get("forecast") or {}
    freshness = row.get("price_freshness") or forecast.get("price_freshness") or {}
    status = str(freshness.get("status") or "")
    current = bool(freshness.get("is_current_opportunity"))
    if status == "fresh" and current:
        return 1.0
    if status == "fresh":
        return 0.7
    if status == "stale":
        return 0.25
    return 0.15


def _avail_component(forecast: dict[str, Any]) -> float:
    status = str(forecast.get("availability_status") or "unknown").lower()
    if status in {"out", "doubtful"}:
        return 0.0
    if status in {"questionable", "uncertain"}:
        return 0.45
    if status in {"probable", "active", "healthy"}:
        return 1.0
    if status in {"missing_report", "unknown"}:
        return 0.55
    return 0.5


def desk_pick_score(row: dict[str, Any]) -> dict[str, float]:
    """Composite score parts + total for one prop row."""
    forecast = row.get("forecast") or {}
    p = float(forecast.get("p_estimate") or 0.0)
    ev_hc = forecast.get("ev_after_haircut")
    if ev_hc is None and forecast.get("ev") is not None:
        ev_hc = float(forecast["ev"])
    chance = _clamp01(p)
    edge = _edge_component(None if ev_hc is None else float(ev_hc))
    fresh = _fresh_component(row)
    avail = _avail_component(forecast)
    total = W_CHANCE * chance + W_EDGE * edge + W_FRESH * fresh + W_AVAIL * avail
    # Soft bonus when model is meaningfully above market (not just high raw %).
    price = row.get("book_price")
    if price is not None and p > 0:
        try:
            implied = american_to_implied(int(price))
            gap = p - float(implied)
            if gap > 0:
                total += min(0.06, gap * 0.35)
        except Exception:  # noqa: BLE001
            pass
    return {
        "chance": chance,
        "edge": edge,
        "fresh": fresh,
        "avail": avail,
        "total": float(total),
        "p": p,
        "ev_after_haircut": float(ev_hc) if ev_hc is not None else None,
    }


def desk_candidate_blockers(row: dict[str, Any], *, haircut: float = VALUE_HAIRCUT,
                            now: datetime | None = None) -> tuple[list[str], str | None]:
    """Selection policy. Price freshness is independent of published-Play eligibility."""
    from spe.publishing.policy import assess, finite

    forecast = row.get("forecast") or {}
    checked = assess(row, now)
    reasons = [r for r in checked["reasons"] if r in {
        "game_not_pregame", "price_not_verified_recently", "invalid_price", "missing_book_or_selection"}]
    status = str(forecast.get("availability_status") or "").lower()
    if status in {"out", "doubtful", "confirmed_inactive", "inactive", "suspended", "questionable", "uncertain"}:
        reasons.append("availability_requires_review")
    p = finite(forecast.get("p_estimate"))
    price = finite(row.get("book_price"))
    if p is None or not SCORE_CHANCE_FLOOR <= p <= 1:
        reasons.append("insufficient_scoring_chance")
    elif price is not None and abs(price) >= 100:
        if p - american_to_implied(int(price)) + 1e-12 < haircut:
            reasons.append("insufficient_price_advantage")
    usage = row.get("usage") or {}
    work = lambda key: finite(usage.get(key)) or 0.0
    # College has no trustworthy goal-line charting: meaningful carries or catches
    # can support a role without requiring a recent touchdown streak.
    role = (work("gl_carries") >= .4 or work("rz_targets") >= .5
            or work("carries") >= 5 or work("receptions") >= 3.5)
    if not role:
        reasons.append("insufficient_scoring_role")
    return reasons, checked["valid_until"]


def _eligible(row: dict[str, Any], *, haircut: float, now: datetime | None = None) -> bool:
    analysis = row.get("desk_analysis")
    if analysis is not None and not analysis.get("eligible"):
        return False
    return not desk_candidate_blockers(row, haircut=haircut, now=now)[0]


def beats_the_price(row: dict[str, Any]) -> bool:
    """Our chance is higher than the chance in the price. Used by the value list, not the pick."""
    forecast = row.get("forecast") or {}
    chance = forecast.get("p_estimate")
    price = row.get("book_price")
    if chance is None or price is None:
        return False
    try:
        p = float(chance)
        implied = american_to_implied(int(price))
    except (TypeError, ValueError):
        return False
    return p >= SCORE_CHANCE_FLOOR and p > implied


def select_desk_takes(
    rows: list[dict[str, Any]],
    *,
    haircut: float = VALUE_HAIRCUT,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Highest scoring chance among supported, currently priced research candidates."""
    best: dict[str, tuple[float, dict[str, Any], dict[str, float]]] = {}
    for row in rows:
        if not _eligible(row, haircut=haircut, now=now):
            continue
        gid = str(row.get("game_id") or "")
        if not gid:
            continue
        chance = float((row.get("forecast") or {}).get("p_estimate") or 0.0)
        if chance < 0.05:
            continue
        parts = desk_pick_score(row)
        prior = best.get(gid)
        if prior is None or (chance, str(row.get("player_id"))) > (prior[0], str(prior[1].get("player_id"))):
            best[gid] = (chance, row, parts)

    out: list[dict[str, Any]] = []
    for _score, row, parts in best.values():
        enriched = dict(row)
        enriched["desk_pick_score"] = parts["total"]
        enriched["desk_pick_parts"] = parts
        out.append(enriched)
    out.sort(key=lambda r: float((r.get("forecast") or {}).get("p_estimate") or 0), reverse=True)
    return out


def capture_atd_desk_takes(
    session: Session,
    *,
    league: str = "all",
    now: datetime | None = None,
    haircut: float | None = None,
) -> dict[str, Any]:
    """Capture the current Desk decision without rewriting previous calls."""
    from spe.shadow.desk_ledger import freeze_desk_rows

    moment = as_utc(now) or datetime.now(UTC)
    props = list_anytime_prop_rows(session, league=league)
    takes = select_desk_takes(props, now=moment)
    saved = freeze_desk_rows(session, props, now=moment)
    return {"status": "ok", "research_only": True, "play_enabled": False,
            "validated_play": False, "selection": "opportunity_probability_with_role_and_price_checks",
            "prop_rows_scanned": len(props), "takes_selected": len(takes),
            "decisions_appended": saved, "captured_at": moment.isoformat()}


def frozen_desk_row(pred: ShadowPrediction) -> dict[str, Any] | None:
    """One pre-kickoff shadow row, scored from the chance and price that were frozen.

    Freshness and availability were not stored on the row, so every player gets
    the same neutral values. They cannot change who is picked. The result of
    the game is not an input.
    """
    p = pred.forecast_probability
    if p is None or float(p) <= 0:
        return None
    comps = pred.components or {}
    price = pred.price_american
    ev_hc = None
    if price is not None:
        try:
            ev_hc = ev_per_dollar(float(p), int(price)) - VALUE_HAIRCUT
        except (TypeError, ValueError):
            ev_hc = None
    freshness = {"status": "stale", "is_current_opportunity": False}
    return {
        "game_id": pred.game_id,
        "player_id": pred.player_id,
        "player_name": comps.get("player_name") or pred.player_id,
        "book_price": None if price is None else int(price),
        "shadow_id": pred.shadow_id,
        "forecast_as_of": pred.forecast_as_of,
        "price_freshness": freshness,
        "forecast": {
            "p_estimate": float(p),
            "ev_after_haircut": ev_hc,
            "availability_status": "unknown",
            "price_freshness": freshness,
        },
    }


def lock_finished_desk_picks(
    session: Session,
    *,
    league: str = "nfl",
    model_prefix: str = "player_td_v1-",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Name the one pre-kickoff scorer for each finished game that has no desk pick yet.

    The choice uses only rows frozen before kickoff. It does not look at the box score.
    """
    moment = as_utc(now) or datetime.now(UTC)
    preds = (
        session.query(ShadowPrediction)
        .filter(ShadowPrediction.market_id == "anytime_td", ShadowPrediction.league_id == league)
        .filter(ShadowPrediction.model_version_id.startswith(model_prefix))
        .all()
    )
    game_ids = {pred.game_id for pred in preds}
    games = {
        game.game_id: game
        for game in session.query(Game).filter(Game.game_id.in_(game_ids or ["__none__"])).all()
    }
    by_game: dict[str, dict[str, ShadowPrediction]] = {}
    already: set[str] = set()
    for pred in preds:
        game = games.get(pred.game_id)
        if game is None or str(game.status or "").lower() != "final":
            continue
        kick = as_utc(game.kickoff_utc)
        frozen_at = as_utc(pred.forecast_as_of)
        if kick is None or frozen_at is None or frozen_at >= kick:
            continue
        if is_desk_take(pred):
            already.add(pred.game_id)
            continue
        players = by_game.setdefault(pred.game_id, {})
        prior = players.get(pred.player_id)
        if prior is None or frozen_at > (as_utc(prior.forecast_as_of) or frozen_at):
            players[pred.player_id] = pred

    marked = 0
    picks: list[dict[str, Any]] = []
    for game_id, players in by_game.items():
        if game_id in already:
            continue
        rows = [row for pred in players.values() if (row := frozen_desk_row(pred)) is not None]
        takes = select_desk_takes(rows)
        if not takes:
            continue
        chosen = takes[0]
        pred = players.get(str(chosen.get("player_id")))
        if pred is None:
            continue
        parts = chosen.get("desk_pick_parts") or {}
        frozen_at = as_utc(pred.forecast_as_of) or moment
        comps = dict(pred.components or {})
        comps["desk_take"] = DESK_TAKE_KIND
        comps["desk_take_kind"] = DESK_TAKE_KIND
        comps["desk_take_at"] = frozen_at.isoformat()
        comps["desk_take_source"] = "prekick_frozen"
        edge = (chosen.get("forecast") or {}).get("ev_after_haircut")
        comps["desk_take_ev_after_haircut"] = None if edge is None else float(edge)
        comps["desk_pick_score"] = float(parts.get("total") or 0)
        comps["desk_pick_parts"] = {
            key: (None if isinstance(value, float) and math.isnan(value) else value)
            for key, value in parts.items()
        }
        comps["desk_take_id"] = comps.get("desk_take_id") or new_id()
        pred.components = comps
        attributes.flag_modified(pred, "components")
        marked += 1
        picks.append(
            {
                "game_id": game_id,
                "player_id": pred.player_id,
                "player_name": comps.get("player_name"),
                "shadow_id": pred.shadow_id,
                "p": pred.forecast_probability,
            }
        )
    return {
        "status": "ok",
        "play_enabled": False,
        "league": league,
        "model_prefix": model_prefix,
        "games_marked": marked,
        "games_already_picked": len(already),
        "picks": picks,
    }


_DESK_KEYS = (
    "desk_take",
    "desk_take_at",
    "desk_take_kind",
    "desk_take_ev_after_haircut",
    "desk_pick_score",
    "desk_pick_parts",
    "desk_take_source",
    "desk_take_id",
    "board_p",
)


def assign_unsettled_board_call(
    session: Session,
    *,
    game_id: str,
    player_id: str,
    called_at: datetime,
    board_p: float | None = None,
    model_prefix: str = "player_td_v1-",
) -> dict[str, Any]:
    """Move an unsettled desk flag onto the player the board called before kickoff.

    A settled take stays put. The call time has to be before kickoff.
    """
    game = session.get(Game, game_id)
    from spe.shadow.desk_ledger import latest_decisions
    if game_id in latest_decisions(session, [game_id]):
        return {"status": "recorded_desk_decision_is_immutable"}
    if game is None:
        return {"status": "missing_game"}
    kick = as_utc(game.kickoff_utc)
    called = as_utc(called_at)
    if kick is None or called is None or called >= kick:
        return {"status": "call_not_before_kickoff"}
    if str(game.status or "").lower() not in {"final", "post"}:
        return {"status": "game_not_final"}

    preds = (
        session.query(ShadowPrediction)
        .filter(
            ShadowPrediction.game_id == game_id,
            ShadowPrediction.market_id == "anytime_td",
        )
        .all()
    )
    settled = {
        row.shadow_id: row.result
        for row in session.query(ShadowSettlement).filter(
            ShadowSettlement.shadow_id.in_([pred.shadow_id for pred in preds] or ["__none__"])
        )
    }
    desk = [pred for pred in preds if is_desk_take(pred)]
    graded = {"win", "loss", "push", "void"}
    if any(settled.get(pred.shadow_id) in graded for pred in desk):
        return {"status": "already_graded"}

    chosen: ShadowPrediction | None = None
    for pred in preds:
        if pred.player_id != player_id:
            continue
        if not (pred.model_version_id or "").startswith(model_prefix):
            continue
        frozen = as_utc(pred.forecast_as_of)
        if frozen is None or frozen >= kick:
            continue
        if chosen is None or frozen > (as_utc(chosen.forecast_as_of) or frozen):
            chosen = pred
    if chosen is None:
        return {"status": "no_pregame_row"}

    cleared: list[str] = []
    for pred in desk:
        if pred.shadow_id == chosen.shadow_id:
            continue
        comps = dict(pred.components or {})
        for key in _DESK_KEYS:
            comps.pop(key, None)
        pred.components = comps
        attributes.flag_modified(pred, "components")
        cleared.append(pred.shadow_id)

    comps = dict(chosen.components or {})
    comps["desk_take"] = DESK_TAKE_KIND
    comps["desk_take_kind"] = DESK_TAKE_KIND
    comps["desk_take_at"] = called.isoformat()
    comps["desk_take_source"] = "tonight_board"
    if board_p is not None:
        comps["board_p"] = float(board_p)
    comps["desk_take_id"] = comps.get("desk_take_id") or new_id()
    chosen.components = comps
    attributes.flag_modified(chosen, "components")
    return {
        "status": "ok",
        "shadow_id": chosen.shadow_id,
        "player_id": player_id,
        "cleared": cleared,
    }


def is_desk_take(pred: ShadowPrediction) -> bool:
    comps = pred.components or {}
    kind = comps.get("desk_take") or comps.get("desk_take_kind")
    return kind in {DESK_TAKE_KIND, "at_this_price"}
