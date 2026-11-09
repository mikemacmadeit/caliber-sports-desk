"""Current-roster eligibility audit.

Do not trust a stale ACT designation. Prior RET plus no post-retirement offensive
involvement is confirmed ineligible. Missing history for a listed ACT player is
uncertain, not automatically active.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.db.base import as_utc

EligibilityLabel = Literal["eligible", "confirmed_ineligible", "uncertain"]

SKILL = {"QB", "RB", "WR", "TE"}


def _involved(targets: float, carries: float, receptions: float, rec_yards: float, rush_yards: float) -> bool:
    return (targets + carries + receptions + abs(rec_yards) + abs(rush_yards)) > 0


def audit_nfl_week(
    session: Session,
    *,
    season_year: int,
    week: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Classify ACT players for a roster week without inventing availability."""
    del now  # classification is roster/stats evidence, not wall-clock dependent
    rows = session.execute(
        text(
            """
            SELECT rw.player_id, rw.team_id, rw.status, p.name, p.position_group,
                   (
                     SELECT MAX(r2.season_year * 100 + r2.week)
                     FROM roster_weeks r2
                     WHERE r2.player_id = rw.player_id AND UPPER(COALESCE(r2.status, '')) = 'RET'
                   ) AS last_ret_key,
                   (
                     SELECT MAX(g.kickoff_utc)
                     FROM player_game_stats pgs
                     JOIN games g ON g.game_id = pgs.game_id
                     WHERE pgs.player_id = rw.player_id
                       AND g.status = 'final'
                       AND (
                         COALESCE(pgs.targets, 0) + COALESCE(pgs.carries, 0) + COALESCE(pgs.receptions, 0)
                         + ABS(COALESCE(pgs.rec_yards, 0)) + ABS(COALESCE(pgs.rush_yards, 0))
                       ) > 0
                   ) AS last_involved_kickoff,
                   (
                     SELECT MAX(g.kickoff_utc)
                     FROM player_game_stats pgs
                     JOIN games g ON g.game_id = pgs.game_id
                     WHERE pgs.player_id = rw.player_id AND g.status = 'final'
                   ) AS last_any_kickoff,
                   (
                     SELECT COUNT(*)
                     FROM player_game_stats pgs
                     JOIN games g ON g.game_id = pgs.game_id
                     WHERE pgs.player_id = rw.player_id
                       AND g.status = 'final'
                       AND g.kickoff_utc >= :season_start
                       AND (
                         COALESCE(pgs.targets, 0) + COALESCE(pgs.carries, 0) + COALESCE(pgs.receptions, 0)
                         + ABS(COALESCE(pgs.rec_yards, 0)) + ABS(COALESCE(pgs.rush_yards, 0))
                       ) > 0
                   ) AS season_involved_games
            FROM roster_weeks rw
            JOIN players p ON p.player_id = rw.player_id
            WHERE rw.season_year = :year AND rw.week = :week
              AND UPPER(COALESCE(rw.status, '')) = 'ACT'
              AND rw.team_id LIKE 'nfl:%'
            """
        ),
        {"year": season_year, "week": week, "season_start": f"{season_year}-08-01"},
    ).mappings().all()

    confirmed: list[dict[str, Any]] = []
    uncertain: list[dict[str, Any]] = []
    eligible = 0
    for row in rows:
        label, reason = classify_act_row(dict(row), season_year=season_year)
        def _iso(value: object) -> str | None:
            if value is None:
                return None
            if hasattr(value, "isoformat"):
                return value.isoformat()  # type: ignore[no-any-return]
            return str(value)

        payload = {
            "player_id": row["player_id"],
            "name": row["name"],
            "team_id": row["team_id"],
            "position_group": row["position_group"],
            "label": label,
            "reason": reason,
            "last_involved_kickoff": _iso(row["last_involved_kickoff"]),
            "last_any_kickoff": _iso(row["last_any_kickoff"]),
            "season_involved_games": int(row["season_involved_games"] or 0),
        }
        if label == "confirmed_ineligible":
            confirmed.append(payload)
        elif label == "uncertain":
            uncertain.append(payload)
        else:
            eligible += 1

    return {
        "season_year": season_year,
        "week": week,
        "act_rows": len(rows),
        "eligible": eligible,
        "confirmed_ineligible": confirmed,
        "uncertain": uncertain,
        "confirmed_ineligible_n": len(confirmed),
        "uncertain_n": len(uncertain),
        "skill_confirmed_ineligible": [r for r in confirmed if str(r["position_group"] or "").upper() in SKILL],
        "skill_uncertain": [r for r in uncertain if str(r["position_group"] or "").upper() in SKILL],
    }


def classify_act_row(row: dict[str, Any], *, season_year: int) -> tuple[EligibilityLabel, str]:
    """Evidence-based label for a single ACT roster_weeks row."""
    pos = str(row.get("position_group") or "").upper()
    last_ret_key = row.get("last_ret_key")
    last_involved = row.get("last_involved_kickoff")
    last_any = row.get("last_any_kickoff")
    season_involved = int(row.get("season_involved_games") or 0)

    # Prior RET and no offensive involvement after retirement evidence → exclude.
    if last_ret_key is not None and season_involved == 0:
        # If they somehow have only zero-stat rows after RET, still exclude.
        if last_involved is None:
            return "confirmed_ineligible", "prior_ret_no_offensive_involvement"
        ret_year = int(last_ret_key) // 100
        involved = as_utc(last_involved)
        if involved is not None and involved.year < ret_year:
            return "confirmed_ineligible", "prior_ret_last_involvement_before_retirement"
        if involved is not None and involved.year < season_year - 1 and season_involved == 0:
            return "confirmed_ineligible", "prior_ret_stale_involvement"

    # Never appeared in any final box score — rookies / practice-squad noise.
    if last_any is None:
        return "uncertain", "act_no_final_box_score_history"

    # Skill player with long inactivity and no current-season involvement.
    if pos in SKILL and season_involved == 0 and last_involved is not None:
        involved = as_utc(last_involved)
        if involved is not None and involved.year < season_year - 1:
            return "uncertain", "skill_stale_history_no_current_season_involvement"

    if pos in SKILL and season_involved == 0 and last_involved is None:
        return "uncertain", "skill_act_never_involved"

    return "eligible", "act_ok"


def confirmed_ineligible_player_ids(
    session: Session,
    *,
    season_year: int,
    week: int,
) -> set[str]:
    audit = audit_nfl_week(session, season_year=season_year, week=week)
    return {row["player_id"] for row in audit["confirmed_ineligible"]}


def audit_upcoming_games(session: Session, game_ids: list[str] | None = None) -> dict[str, Any]:
    """Run eligibility classification for teams on the upcoming slate."""
    from spe.db.models import Game
    from spe.identity.roster import eligible_players_for_game, season_year_for_game

    q = session.query(Game).filter(Game.data_tier == "upcoming", Game.league_id == "nfl")
    if game_ids:
        q = q.filter(Game.game_id.in_(game_ids))
    games = q.all()
    by_game: list[dict[str, Any]] = []
    all_excluded: list[dict[str, Any]] = []
    all_uncertain: list[dict[str, Any]] = []
    for game in games:
        year = season_year_for_game(session, game)
        week = game.week
        if year is None or week is None:
            by_game.append({"game_id": game.game_id, "status": "no_season_week"})
            continue
        audit = audit_nfl_week(session, season_year=year, week=week)
        excluded_ids = {r["player_id"] for r in audit["confirmed_ineligible"]}
        uncertain_ids = {r["player_id"] for r in audit["uncertain"]}
        eligible = eligible_players_for_game(session, game)
        # eligible_players_for_game already applies exclusions once roster.py is wired.
        leaked = [p for p in eligible if p.player_id in excluded_ids]
        by_game.append(
            {
                "game_id": game.game_id,
                "season_year": year,
                "week": week,
                "eligible_after_filter": len(eligible),
                "confirmed_ineligible_on_week": len(audit["confirmed_ineligible"]),
                "uncertain_on_week": len(audit["uncertain"]),
                "leaked_confirmed_ineligible": [
                    {"player_id": p.player_id, "name": p.name, "team_id": p.team_id} for p in leaked
                ],
            }
        )
        all_excluded.extend(audit["confirmed_ineligible"])
        all_uncertain.extend(audit["uncertain"])

    # De-dupe players across games for the report.
    def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for row in rows:
            pid = row["player_id"]
            if pid in seen:
                continue
            seen.add(pid)
            out.append(row)
        return out

    return {
        "games": by_game,
        "confirmed_ineligible": _dedupe(all_excluded),
        "uncertain": _dedupe(all_uncertain),
        "as_of": datetime.now(UTC).isoformat(),
    }
