"""One fail-closed eligibility policy. Research qualification never enables Play."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from spe.config import settings
from spe.db.base import as_utc
from spe.market.odds import american_to_decimal
from spe.market.price_age import max_fresh_minutes

VERSION = "published-v1"
MARKETS = {"anytime_td", "receptions", "rushing_yards", "receiving_yards"}


def finite(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def assess(row: dict, now: datetime | None = None) -> dict:
    now = as_utc(now) or datetime.now(UTC)
    forecast = row.get("forecast") or {}
    freshness = row.get("price_freshness") or forecast.get("price_freshness") or {}
    kick = as_utc(row.get("kickoff_utc"))
    observed = as_utc(freshness.get("observed_at") or freshness.get("captured_at"))
    prediction = as_utc(forecast.get("prediction_as_of"))
    cutoff = as_utc(forecast.get("data_cutoff"))
    price = finite(row.get("book_price"))
    p = finite(forecast.get("p_estimate"))
    edge = finite(forecast.get("ev_after_haircut"))
    reasons = []
    if (
        not kick
        or kick <= now
        or row.get("game_status") in {"final", "in_progress", "canceled", "cancelled", "postponed"}
    ):
        reasons.append("game_not_pregame")
    age = (now - observed).total_seconds() / 60 if observed else None
    fresh = bool(
        kick
        and kick > now
        and age is not None
        and 0 <= age <= max_fresh_minutes(kick, now)
        and freshness.get("verification")
        not in {"failed", "budget_blocked", "skipped", "unverified"}
    )
    if not fresh:
        reasons.append("price_not_verified_recently")
    if price is None or abs(price) < 100 or price != int(price):
        reasons.append("invalid_price")
    if not row.get("bookmaker") or not row.get("outcome_id"):
        reasons.append("missing_book_or_selection")
    if (
        not forecast.get("model_version")
        or not prediction
        or not cutoff
        or not (cutoff <= prediction <= now)
    ):
        reasons.append("missing_or_future_model_provenance")
    if row.get("data_tier") == "demo" or forecast.get("data_tier") == "demo":
        reasons.append("demo_data")
    if str(forecast.get("availability_status", "")).lower() not in {
        "active",
        "healthy",
        "probable",
        "available",
    }:
        reasons.append("availability_unconfirmed")
    blocked = {
        "IDENTITY_CONFLICT",
        "SOURCE_STALE",
        "INJURY_COVERAGE_INCOMPLETE",
        "NEVER_ASSUME_HEALTHY",
        "INJURY_LOW_CONFIDENCE",
        "LATE_NEWS_CONFLICT",
        "AVAILABILITY_TOO_LOW",
        "ABSTAIN_THIN_CALIBRATION",
    }
    if blocked.intersection(forecast.get("reason_codes") or []):
        reasons.append("insufficient_reliable_inputs")
    market = row.get("market_id")
    line = finite(row.get("line"))
    side = row.get("side")
    if (
        market not in MARKETS
        or (market != "anytime_td" and (line is None or side not in {"over", "under"}))
        or (market == "anytime_td" and side not in {"yes", "over"})
    ):
        reasons.append("unsupported_market_or_side")
    if p is None or not 0 < p < 1:
        reasons.append("invalid_probability")
    if edge is None or edge < settings.min_ev_after_haircut:
        reasons.append("insufficient_value")
    threshold = None
    if p is not None and 0 < p < 1 and price is not None and abs(price) >= 100 and edge is not None:
        decimal = american_to_decimal(int(price))
        # Recover the conservative probability encoded by EV after uncertainty haircut.
        conservative_p = (edge + 1) / decimal
        if not 0 < conservative_p < 1 or p * decimal - 1 < settings.min_ev_after_haircut:
            reasons.append("inconsistent_value")
        else:
            minimum_decimal = (1 + settings.min_ev_after_haircut) / conservative_p
            threshold = (
                math.ceil((minimum_decimal - 1) * 100)
                if minimum_decimal >= 2
                else math.ceil(-100 / (minimum_decimal - 1))
            )
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "policy_version": VERSION,
        "verified_at": observed.isoformat() if observed else None,
        "age_minutes": age,
        "valid_until": min(
            kick, observed + timedelta(minutes=max_fresh_minutes(kick, now))
        ).isoformat()
        if kick and observed
        else None,
        "minimum_american_odds": threshold,
        "research_only": True,
        "play_enabled": False,
    }


def annotate(row: dict, now: datetime | None = None) -> dict:
    result = dict(row)
    result["qualification"] = assess(row, now)
    freshness = dict(
        row.get("price_freshness") or (row.get("forecast") or {}).get("price_freshness") or {}
    )
    freshness.update(
        age_minutes=result["qualification"]["age_minutes"],
        is_current_opportunity=result["qualification"]["eligible"],
    )
    if "price_not_verified_recently" in result["qualification"]["reasons"]:
        freshness["status"] = "stale"
    result["price_freshness"] = freshness
    result["forecast"] = {**(row.get("forecast") or {}), "price_freshness": freshness}
    return result
