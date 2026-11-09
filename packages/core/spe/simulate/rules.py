from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LeagueRules:
    league_id: str
    regulation_minutes: int
    play_clock: int
    overtime: str
    notes: str


NFL_2026 = LeagueRules(
    league_id="nfl",
    regulation_minutes=60,
    play_clock=40,
    overtime="regular_2025plus_both_possess_unless_td_and_safety_exceptions",
    notes=(
        "Never apply NCAA overtime to NFL. Regular-season OT: both teams possess "
        "except documented scoring exceptions. Super Bowl/postseason may differ."
    ),
)

FBS_2026 = LeagueRules(
    league_id="fbs",
    regulation_minutes=60,
    play_clock=40,
    overtime="ncaa_2019plus_25yd_two_point_from_2nd_ot",
    notes=(
        "FBS OT starts at the 25. Two-point attempts from the 2nd overtime. "
        "Clock, penalty, and targeting rules are college-specific."
    ),
)


def rules_for(league_id: str) -> LeagueRules:
    if league_id == "nfl":
        return NFL_2026
    if league_id == "fbs":
        return FBS_2026
    raise KeyError(f"No rules registered for {league_id}")
