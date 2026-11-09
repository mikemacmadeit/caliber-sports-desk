from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from spe.config import settings
from spe.db.base import as_utc
from spe.market.odds import ev_per_dollar, fractional_kelly


@dataclass
class RecommendInput:
    p: float
    p_low: float
    p_high: float
    american: int | None
    price_captured_at: datetime | None
    game_status: str
    availability_status: str
    availability_prob: float
    injury_confidence: float | None
    source_fresh: bool
    grading_supported: bool
    calibration_n: int
    late_news_conflict: bool = False
    min_ev: float | None = None
    max_price_age_minutes: float | None = None


@dataclass
class RecommendResult:
    status: str  # play | no_bet
    ev: float | None
    ev_after_haircut: float | None
    kelly_fractional: float | None
    threshold: float
    reason_codes: list[str] = field(default_factory=list)


def recommend(inp: RecommendInput, now: datetime | None = None) -> RecommendResult:
    """Abstain aggressively. A recommendation is tied to an offered price, not a name."""
    now = now or datetime.now(UTC)
    threshold = inp.min_ev if inp.min_ev is not None else settings.min_ev_after_haircut
    reasons: list[str] = []

    if inp.game_status not in {"scheduled", "preview", "pregame"}:
        return RecommendResult("no_bet", None, None, None, threshold, ["GAME_NOT_PREGAME"])
    if inp.american is None or inp.price_captured_at is None:
        return RecommendResult("no_bet", None, None, None, threshold, ["NO_CURRENT_PRICE"])
    if not inp.grading_supported:
        return RecommendResult("no_bet", None, None, None, threshold, ["UNSUPPORTED_GRADING_RULE"])
    if not inp.source_fresh:
        reasons.append("SOURCE_STALE")
    max_age = inp.max_price_age_minutes or settings.max_price_age_minutes
    age_min = (now - as_utc(inp.price_captured_at)).total_seconds() / 60.0
    if age_min > max_age:
        reasons.append("PRICE_STALE")
        return RecommendResult("no_bet", None, None, None, threshold, reasons)
    ev, ev_hc, kelly = _priced(inp)
    if inp.availability_status in {"unknown", "missing_report"}:
        reasons.append("INJURY_COVERAGE_INCOMPLETE")
        return RecommendResult("no_bet", ev, ev_hc, kelly, threshold, reasons + ["NEVER_ASSUME_HEALTHY"])
    if inp.availability_prob < settings.min_availability_prob:
        reasons.append("AVAILABILITY_TOO_LOW")
        return RecommendResult("no_bet", ev, ev_hc, kelly, threshold, reasons)
    if inp.injury_confidence is not None and inp.injury_confidence < 0.6:
        reasons.append("INJURY_LOW_CONFIDENCE")
        return RecommendResult("no_bet", ev, ev_hc, kelly, threshold, reasons)
    if inp.late_news_conflict:
        return RecommendResult("no_bet", ev, ev_hc, kelly, threshold, ["LATE_NEWS_CONFLICT"])
    if inp.calibration_n < 80:
        reasons.append("CALIBRATION_HISTORY_THIN")
        # Thin history does not auto-play even with a pretty EV.

    reasons.append("PRICE_TIED")
    if inp.calibration_n < 80:
        return RecommendResult("no_bet", ev, ev_hc, kelly, threshold, reasons + ["ABSTAIN_THIN_CALIBRATION"])
    if ev_hc < threshold:
        reasons.append("EDGE_AFTER_HAIRCUT_INSUFFICIENT")
        return RecommendResult("no_bet", ev, ev_hc, kelly, threshold, reasons)
    reasons.append("EDGE_AFTER_HAIRCUT_OK")
    return RecommendResult("play", ev, ev_hc, kelly, threshold, reasons)


def _priced(inp: RecommendInput) -> tuple[float, float, float]:
    ev = ev_per_dollar(inp.p, inp.american or 0)
    width = inp.p_high - inp.p_low
    ev_hc = ev - 0.5 * width * american_hold_factor(inp.american or 0)
    kelly = fractional_kelly(inp.p, inp.american or 0, 0.25)
    return ev, ev_hc, kelly


def american_hold_factor(american: int) -> float:
    # Convert interval on probability into an EV haircut scale.
    return abs(american_to_decimal_minus_one(american))


def american_to_decimal_minus_one(american: int) -> float:
    if american > 0:
        return american / 100.0
    return 100.0 / abs(american)
