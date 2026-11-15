"""A second look for close spread calls.

The card's side stays unless two checks both want the other team: the last
four games, and this season adjusted for who they played. One check is not
enough to move the pick. Big gaps are left alone.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.models.ats.labels import normal_cover_probs

LAST_GAMES = 4
MIN_GAMES = 2
HFA = {"fbs": 3.0, "nfl": 2.5}
SHRINK = {"fbs": 14.0, "nfl": 8.0}


class CloseLook:
    def __init__(
        self,
        league: str,
        recent: dict[str, list[tuple[datetime, float]]],
        ratings: dict[str, float],
    ):
        self.league = league
        self.recent = recent
        self.ratings = ratings
        self.hfa = HFA.get(league, 3.0)


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        stamp = value
    else:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def load_close_look(session: Session, league: str, before: datetime) -> CloseLook:
    """Season results strictly before ``before``. Later games cannot move the pick."""
    rows = session.execute(
        text(
            """
            SELECT g.kickoff_utc, g.neutral_site, g.away_score, g.home_score, a.name, h.name
            FROM games g
            JOIN teams a ON a.team_id = g.away_team_id
            JOIN teams h ON h.team_id = g.home_team_id
            WHERE g.league_id = :lg
              AND g.status = 'final'
              AND g.season_id LIKE :season
              AND g.home_score IS NOT NULL
              AND g.kickoff_utc < :before
            """
        ),
        {
            "lg": league,
            "season": f"{league}-2026%",
            "before": _utc(before).strftime("%Y-%m-%d %H:%M:%S"),
        },
    ).fetchall()
    recent: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    games: list[tuple[str, str, float, float]] = []
    for kickoff, neutral, away_score, home_score, away, home in rows:
        margin = float(home_score) - float(away_score)
        when = _utc(kickoff)
        hfa = 0.0 if neutral else HFA.get(league, 3.0)
        games.append((home, away, margin, hfa))
        recent[home].append((when, margin))
        recent[away].append((when, -margin))
    for team in recent:
        recent[team].sort(key=lambda item: item[0])
    ratings = _adjusted_ratings(games, SHRINK.get(league, 14.0))
    return CloseLook(league, recent, ratings)


def _adjusted_ratings(
    games: list[tuple[str, str, float, float]],
    shrink: float,
) -> dict[str, float]:
    teams = {name for home, away, *_ in games for name in (home, away)}
    rating = {team: 0.0 for team in teams}
    for _ in range(30):
        sums: dict[str, float] = defaultdict(float)
        counts: dict[str, int] = defaultdict(int)
        for home, away, margin, hfa in games:
            sums[home] += margin - hfa + rating[away]
            counts[home] += 1
            sums[away] += -margin + hfa + rating[home]
            counts[away] += 1
        for team in teams:
            n = counts[team]
            raw = sums[team] / n if n else 0.0
            rating[team] = (n / (n + shrink)) * raw if n else 0.0
    if rating:
        center = sum(rating.values()) / len(rating)
        for team in rating:
            rating[team] -= center
    return rating


def _average_before(
    history: list[tuple[datetime, float]] | None,
    kickoff: datetime,
) -> float | None:
    if not history:
        return None
    prior = [margin for when, margin in history if when < kickoff]
    if len(prior) < MIN_GAMES:
        return None
    sample = prior[-LAST_GAMES:]
    return sum(sample) / len(sample)


def _side_against_number(home_margin: float, market_home_margin: float) -> str | None:
    """The side that covers if the game lands on home_margin. Quiet under a point."""
    gap = home_margin - market_home_margin
    if abs(gap) < 1:
        return None
    return "home" if gap > 0 else "away"


def close_swap_side(row: dict[str, Any], look: CloseLook) -> str | None:
    """The other side when both checks want it. None means keep the card."""
    if row.get("ats_basis") != "score" or row.get("ats_side") not in {"home", "away"}:
        return None
    if row.get("home_handicap") is None:
        return None
    home = str(row.get("home_team") or "")
    away = str(row.get("away_team") or "")
    if home not in look.ratings or away not in look.ratings:
        return None
    kickoff = _utc(row.get("kickoff_utc"))
    home_recent = _average_before(look.recent.get(home), kickoff)
    away_recent = _average_before(look.recent.get(away), kickoff)
    if home_recent is None or away_recent is None:
        return None
    market = -float(row["home_handicap"])
    hfa = 0.0 if row.get("neutral_site") else look.hfa
    recent_side = _side_against_number(home_recent - away_recent + hfa, market)
    schedule_side = _side_against_number(look.ratings[home] - look.ratings[away] + hfa, market)
    other = "away" if row["ats_side"] == "home" else "home"
    if recent_side == other and schedule_side == other:
        return other
    return None


def apply_close_look(row: dict[str, Any], look: CloseLook) -> dict[str, Any]:
    """Swap a close pick when both checks agree. Started cards are left to the lock."""
    from spe.models.ats.predict import format_spread_line, grade_displayed_pick

    other = close_swap_side(row, look)
    if other is None:
        return row
    handicap = float(row["home_handicap"])
    line = handicap if other == "home" else -handicap
    team = str(row.get("home_team") if other == "home" else row.get("away_team"))
    row["ats_side"] = other
    row["ats_team"] = team
    row["ats_line"] = line
    row["ats_pick_label"] = f"{team} {format_spread_line(line)}"
    row["ats_close_swapped"] = True
    sigma = row.get("margin_sigma")
    gap = row.get("model_vs_market")
    if sigma is not None and gap is not None:
        raw_margin = float(gap) - handicap
        probs = normal_cover_probs(raw_margin, handicap, float(sigma))
        row["ats_probability"] = probs["p_home_cover"] if other == "home" else probs["p_away_cover"]
    row["ats_pick_result"] = grade_displayed_pick(
        status=row.get("game_status"),
        home_score=row.get("home_score"),
        away_score=row.get("away_score"),
        pick_market="spread",
        favored_side=other,
        home_handicap=handicap,
    )
    return row
