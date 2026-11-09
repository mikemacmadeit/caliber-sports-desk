"""A relocated club keeps the seasons played under its old abbreviation."""

import sqlite3
from pathlib import Path

from spe.integrity.assignments import NFL_CONTINUITY
from spe.models.ats.guards import HISTORY_GAP_LIMIT, withhold_for_history_gap
from spe.models.ats.ratings import team_id_canon


def test_moved_clubs_fold_into_the_current_id():
    canon = team_id_canon(None, "nfl")  # type: ignore[arg-type]
    assert canon["nfl:OAK"] == "nfl:LV"
    assert canon["nfl:SD"] == "nfl:LAC"
    assert canon["nfl:STL"] == "nfl:LA"
    assert NFL_CONTINUITY == canon


def test_raiders_seasons_match_the_49ers_before_their_kickoff():
    db = Path("data/caliber.db")
    if not db.exists():
        return
    canon = team_id_canon(None, "nfl")  # type: ignore[arg-type]
    con = sqlite3.connect(db)
    counts = _prior_games(con, canon, kickoff="2026-11-08 21:05:00")
    con.close()
    raiders = counts.get("nfl:LV", 0)
    niners = counts.get("nfl:SF", 0)
    assert raiders > 0 and niners > 0
    assert not withhold_for_history_gap(niners, raiders)
    assert abs(niners - raiders) < HISTORY_GAP_LIMIT


def _prior_games(con: sqlite3.Connection, canon: dict[str, str], *, kickoff: str) -> dict[str, int]:
    def mapped(column: str) -> str:
        parts = [f"WHEN '{old}' THEN '{new}'" for old, new in canon.items()]
        return f"CASE {column} {' '.join(parts)} ELSE {column} END"

    sql = f"""
        WITH games_mapped AS (
            SELECT game_id,
                   {mapped("home_team_id")} AS home_team_id,
                   {mapped("away_team_id")} AS away_team_id
            FROM games
            WHERE league_id = 'nfl'
              AND status = 'final'
              AND data_tier = 'historical'
              AND kickoff_utc < ?
        ),
        sides AS (
            SELECT game_id, home_team_id AS team_id FROM games_mapped
            UNION ALL
            SELECT game_id, away_team_id FROM games_mapped
        ),
        offenses AS (
            SELECT DISTINCT p.game_id, {mapped("p.offense_id")} AS team_id
            FROM plays p
            JOIN games_mapped g ON g.game_id = p.game_id
        )
        SELECT s.team_id, COUNT(*) AS n
        FROM sides s
        JOIN offenses o ON o.game_id = s.game_id AND o.team_id = s.team_id
        WHERE s.team_id IN ('nfl:LV', 'nfl:SF')
        GROUP BY s.team_id
    """
    return {team_id: n for team_id, n in con.execute(sql, (kickoff,))}
