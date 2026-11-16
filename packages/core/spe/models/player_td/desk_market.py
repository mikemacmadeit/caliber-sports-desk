"""Normalize sportsbook handicaps and totals, retaining source and timing."""
import math
from datetime import UTC, datetime
from statistics import median

from sqlalchemy import select

from spe.db.base import as_utc
from spe.db.models import ClosingLine, Game, OddsTick, Outcome, RawSnapshot

MARKET_FEATURES = ["market_team_spread", "market_game_total", "market_spread_known", "market_total_known"]
MIN_TEAM_IMPLIED_POINTS = 21.0
PREGAME_MARKET_ENDPOINT = "td_pregame_market"


def store_pregame_quote(db, game_id, context, *, captured_at=None):
    """Append an observed pregame line without overwriting historical prices."""
    captured = as_utc(captured_at) or datetime.now(UTC)
    observed = as_utc(context.get("observed_at"))
    game = db.get(Game, game_id)
    if game is None or observed is None or not observed <= captured < as_utc(game.kickoff_utc):
        return False
    spread, total = context.get("home_handicap"), context.get("total")
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (spread, total)):
        return False
    if not 0 < total < 150 or abs(spread) >= total:
        return False
    row = RawSnapshot(provider="espn", endpoint=PREGAME_MARKET_ENDPOINT,
        provider_record_id=game_id, payload=context, observed_at=observed,
        ingested_at=captured, license_name="ESPN public endpoint; terms apply", credit_cost=0)
    db.add(row)
    db.flush()
    return True


def summary_market(payload, event_id, observed):
    for quote in (payload or {}).get("pickcenter") or []:
        try:
            total, spread = float(quote["overUnder"]), abs(float(quote["spread"]))
        except (KeyError, TypeError, ValueError):
            continue
        home, away = quote.get("homeTeamOdds") or {}, quote.get("awayTeamOdds") or {}
        if spread and not (home.get("favorite") or away.get("favorite")):
            continue
        handicap = -spread if home.get("favorite") else spread
        if 0 < total < 150 and abs(handicap) < total:
            return {"home_handicap": handicap, "total": total, "source": "espn_summary_market",
                    "observed_at": observed.isoformat(), "event_id": event_id}
    return None


def store_archive(db, archive):
    """Never backdate observation time when importing retrospective markets."""
    written = 0
    for gid, context in archive.items():
        game = db.get(Game, gid)
        observed = as_utc(context.get("observed_at"))
        if game is None or observed is None:
            continue
        kick = as_utc(game.kickoff_utc)
        for market, field in (("point_spread", "home_handicap"), ("total", "total")):
            value = context.get(field)
            if value is None or not math.isfinite(value):
                continue
            existing = db.scalars(select(ClosingLine).where(ClosingLine.game_id == gid,
                ClosingLine.source == "espn_summary_market", ClosingLine.market_id == market,
                ClosingLine.provider == "espn")).first()
            if existing:
                # ClosingLine is unique per game/market/source/provider, not a
                # quote-history table. Preserve the first archived observation;
                # do not relabel an old price as newly observed or insert a duplicate.
                continue
            db.add(ClosingLine(game_id=gid, market_id=market, home_line=value,
                away_line=-value if market == "point_spread" else None,
                source="espn_summary_market", captured_at=min(observed, kick), provider="espn",
                provider_record_id=f"{context.get('event_id', gid)}:{market}", observed_at=observed,
                effective_at=min(observed,kick), quality_status="retrospective" if observed > kick else "ok"))
            written += 1
    db.flush()
    return written


def market_pick_eligible(row):
    context = row.get("market_context") or {}
    points = row.get("team_implied_points")
    return bool(context.get("usable") and isinstance(points, (int, float)) and math.isfinite(points)
                and points >= MIN_TEAM_IMPLIED_POINTS)


def market_features(context, is_home, include_total=True):
    spread, total = context.get("home_handicap"), context.get("total")
    return {"market_team_spread": (spread if is_home else -spread) if spread is not None else 0.0,
            "market_game_total": total if total is not None and include_total else 0.0,
            "market_spread_known": float(spread is not None), "market_total_known": float(total is not None and include_total),
            "context_game_total": total}


def context_for_game(db, game_id, cutoff, *, replay=False):
    values = {"point_spread": [], "total": []}
    quote = db.scalars(select(RawSnapshot).where(RawSnapshot.provider == "espn",
        RawSnapshot.endpoint == PREGAME_MARKET_ENDPOINT, RawSnapshot.provider_record_id == game_id,
        RawSnapshot.observed_at <= cutoff, RawSnapshot.ingested_at <= cutoff)
        .order_by(RawSnapshot.observed_at.desc()).limit(1)).first()
    if quote:
        for market, field in (("point_spread", "home_handicap"), ("total", "total")):
            value = quote.payload.get(field)
            if isinstance(value, (int, float)) and math.isfinite(value):
                values[market].append((as_utc(quote.observed_at), value, "espn_summary_market", False))
    for row in db.scalars(select(ClosingLine).where(ClosingLine.game_id == game_id,
            ClosingLine.market_id.in_(("point_spread", "total")),
            ClosingLine.quality_status.notin_(("invalid", "quarantined")))):
        observed, captured = as_utc(row.observed_at), as_utc(row.captured_at)
        archive = row.source in {"cfbd", "nflverse_consensus", "espn_summary_market"}
        on_time = observed and captured and observed <= cutoff and captured <= cutoff
        if not on_time and not (replay and archive):
            continue
        number = row.home_line
        if number is None or not math.isfinite(number):
            continue
        # nflverse stores expected home margin; CFBD stores home handicap.
        if row.market_id == "point_spread" and row.source == "nflverse_consensus":
            number = -number
        values[row.market_id].append((observed, float(number), row.source, not on_time))
    # Current tick handicaps use the sportsbook sign convention.
    ticks = db.execute(select(Outcome, OddsTick).join(OddsTick, OddsTick.outcome_id == Outcome.outcome_id).where(
        Outcome.game_id == game_id, Outcome.market_id.in_(("point_spread", "total")),
        OddsTick.is_live.is_(False), OddsTick.observed_at <= cutoff, OddsTick.captured_at <= cutoff,
        OddsTick.quality_status.notin_(("invalid", "quarantined"))).order_by(OddsTick.observed_at)).all()
    latest = {}
    for outcome, tick in ticks:
        if outcome.line is None or not math.isfinite(outcome.line) or (tick.source_at and as_utc(tick.source_at) > cutoff):
            continue
        if outcome.market_id == "point_spread" and outcome.side not in {"home", "away"}:
            continue
        number = -outcome.line if outcome.market_id == "point_spread" and outcome.side == "away" else outcome.line
        latest[(outcome.market_id, tick.bookmaker_id)] = (as_utc(tick.observed_at), number, tick.provider, False)
    for (market, _), entry in latest.items():
        values[market].append(entry)
    result = {"home_handicap": None, "total": None, "sources": [], "retrospective_market_data": False}
    for market, dest in (("point_spread", "home_handicap"), ("total", "total")):
        available = values[market]
        if not replay:
            available = [v for v in available if 0 <= (cutoff-v[0]).total_seconds() <= 24*3600]
        if not available:
            continue
        newest = max(v[0] for v in available)
        current = [v for v in available if (newest-v[0]).total_seconds() <= 3600]
        result[dest] = float(median(v[1] for v in current))
        result["sources"].extend(sorted({v[2] for v in current}))
        result["retrospective_market_data"] |= any(v[3] for v in current)
    spread, total = result["home_handicap"], result["total"]
    result["usable"] = bool(spread is not None and total is not None and 0 < total < 150 and abs(spread) < total)
    if result["usable"]:
        result["home_implied_points"] = (total - spread) / 2
        result["away_implied_points"] = (total + spread) / 2
    return result
