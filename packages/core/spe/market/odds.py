from __future__ import annotations

from dataclasses import dataclass


def american_to_implied(american: int) -> float:
    if american == 0:
        raise ValueError("American odds cannot be 0")
    if american > 0:
        return 100.0 / (american + 100.0)
    a = abs(american)
    return a / (a + 100.0)


def american_to_decimal(american: int) -> float:
    if american == 0:
        raise ValueError("American odds cannot be 0")
    if american > 0:
        return 1.0 + american / 100.0
    return 1.0 + 100.0 / abs(american)


def ev_per_dollar(p: float, american: int) -> float:
    """EV per $1 staked: p*(d-1) - (1-p). Not a guarantee of profit."""
    if not 0.0 <= p <= 1.0:
        raise ValueError("p must be in [0, 1]")
    d = american_to_decimal(american)
    return p * (d - 1.0) - (1.0 - p)


def fractional_kelly(p: float, american: int, fraction: float = 0.25) -> float:
    """Educational risk metric only. Does not establish safety of a bet."""
    d = american_to_decimal(american)
    b = d - 1.0
    if b <= 0:
        return 0.0
    full = (b * p - (1.0 - p)) / b
    return max(0.0, full * fraction)


def no_vig_two_way(implied_a: float, implied_b: float) -> tuple[float, float]:
    total = implied_a + implied_b
    if total <= 0:
        raise ValueError("Implied probabilities must be positive")
    return implied_a / total, implied_b / total


@dataclass(frozen=True)
class NoVigResult:
    identified: bool
    p_yes: float | None
    p_no: float | None
    overround: float | None
    reason: str


def no_vig_anytime_td(yes_american: int, no_american: int | None = None) -> NoVigResult:
    """A yes-only anytime-TD price does not identify a no-vig probability."""
    p_yes_raw = american_to_implied(yes_american)
    if no_american is None:
        return NoVigResult(
            identified=False,
            p_yes=None,
            p_no=None,
            overround=None,
            reason="YES_ONLY_NOVIG_UNIDENTIFIED",
        )
    p_no_raw = american_to_implied(no_american)
    p_yes, p_no = no_vig_two_way(p_yes_raw, p_no_raw)
    return NoVigResult(
        identified=True,
        p_yes=p_yes,
        p_no=p_no,
        overround=p_yes_raw + p_no_raw - 1.0,
        reason="TWO_WAY_DEVIGGED",
    )
