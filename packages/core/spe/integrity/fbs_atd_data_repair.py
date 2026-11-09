"""Repair FBS Anytime TD input data using only rows already in the DB.

- Backfill null ``player_game_stats.team_id`` from roster + scoreboard aliases.
- Reopen continuing roster rows that a prior-year sync falsely closed.

Does not call paid APIs, does not touch freezes/settlements, does not enable Play.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.db.models import Team
from spe.integrity.assignments import linked_team_ids

# Exact timestamp used by the contaminated prior-year roster sync.
FALSE_CLOSE_TS = datetime(2026, 11, 7, 1, 28, 25, 493860)


def repair_null_pgs_team_ids(session: Session, *, season_year: int = 2026) -> dict[str, Any]:
    """Set team_id on null PGS rows when roster uniquely maps to a game side."""
    teams = {t.team_id: t for t in session.query(Team).filter(Team.league_id == "fbs").all()}
    alias_of: dict[str, set[str]] = {}
    for t in teams.values():
        group = linked_team_ids(t) | {t.team_id}
        for tid in group:
            alias_of[tid] = alias_of.get(tid, set()) | group

    rows = session.execute(
        text(
            """
            SELECT pgs.id, pgs.player_id, g.home_team_id, g.away_team_id
            FROM player_game_stats pgs
            JOIN games g ON g.game_id = pgs.game_id
            WHERE pgs.team_id IS NULL
              AND g.league_id = 'fbs'
              AND g.status = 'final'
              AND g.kickoff_utc >= :start
              AND g.kickoff_utc < :end
            """
        ),
        {"start": f"{season_year}-07-01", "end": f"{season_year + 1}-02-01"},
    ).mappings().all()

    roster = session.execute(
        text(
            """
            SELECT player_id, team_id, valid_from, valid_to
            FROM rosters
            WHERE team_id LIKE 'fbs:%' OR team_id IN ('uga','ala','tex','ou','osu')
            ORDER BY valid_from DESC
            """
        )
    ).mappings().all()
    by_player: dict[str, list[dict[str, Any]]] = {}
    for r in roster:
        by_player.setdefault(str(r["player_id"]), []).append(dict(r))

    updated = 0
    ambiguous = 0
    unresolved = 0
    for row in rows:
        pid = str(row["player_id"])
        home = str(row["home_team_id"])
        away = str(row["away_team_id"])
        home_g = alias_of.get(home, {home})
        away_g = alias_of.get(away, {away})
        hits: set[str] = set()
        for rr in by_player.get(pid, []):
            tid = str(rr["team_id"])
            if tid in home_g or home in alias_of.get(tid, {tid}):
                hits.add(home)
            if tid in away_g or away in alias_of.get(tid, {tid}):
                hits.add(away)
        if len(hits) == 1:
            team_id = next(iter(hits))
            session.execute(
                text("UPDATE player_game_stats SET team_id = :tid WHERE id = :id AND team_id IS NULL"),
                {"tid": team_id, "id": row["id"]},
            )
            updated += 1
        elif len(hits) > 1:
            ambiguous += 1
        else:
            unresolved += 1
    session.flush()
    return {
        "status": "ok",
        "season_year": season_year,
        "candidates": len(rows),
        "updated": updated,
        "ambiguous": ambiguous,
        "unresolved": unresolved,
    }


def repair_false_roster_closures(session: Session, *, closed_at: datetime | None = None) -> dict[str, Any]:
    """Reopen roster rows closed by the bad prior-year sync for still-active players."""
    stamp = closed_at or FALSE_CLOSE_TS
    # Players with upcoming anytime_td markets or 2026 involved box scores.
    active = session.execute(
        text(
            """
            SELECT DISTINCT player_id FROM (
              SELECT o.player_id AS player_id
              FROM outcomes o
              JOIN games g ON g.game_id = o.game_id
              WHERE o.market_id = 'anytime_td'
                AND o.player_id IS NOT NULL
                AND g.league_id = 'fbs'
                AND g.status IN ('scheduled','in_progress')
                AND g.data_tier = 'upcoming'
              UNION
              SELECT pgs.player_id
              FROM player_game_stats pgs
              JOIN games g ON g.game_id = pgs.game_id
              WHERE g.league_id = 'fbs'
                AND g.status = 'final'
                AND g.kickoff_utc >= '2026-07-01'
                AND (
                  COALESCE(pgs.carries,0)+COALESCE(pgs.attempts,0)+COALESCE(pgs.receptions,0)
                ) > 0
            )
            """
        )
    ).scalars().all()
    active_ids = {str(pid) for pid in active if pid}
    if not active_ids:
        return {"status": "ok", "reopened": 0, "active_players": 0}

    closed = session.execute(
        text(
            """
            SELECT roster_id, player_id, team_id, valid_from, valid_to
            FROM rosters
            WHERE valid_to = :ts
            """
        ),
        {"ts": stamp.replace(tzinfo=None) if stamp.tzinfo else stamp},
    ).mappings().all()

    # Latest open membership per player (any team).
    open_rows = session.execute(
        text(
            """
            SELECT player_id, team_id, valid_from
            FROM rosters
            WHERE valid_to IS NULL
            """
        )
    ).mappings().all()
    latest_open: dict[str, tuple[str, datetime]] = {}
    for r in open_rows:
        pid = str(r["player_id"])
        start = r["valid_from"]
        prev = latest_open.get(pid)
        if prev is None or (start is not None and (prev[1] is None or start >= prev[1])):
            latest_open[pid] = (str(r["team_id"]), start)

    reopened = 0
    skipped_transfer = 0
    for row in closed:
        pid = str(row["player_id"])
        if pid not in active_ids:
            continue
        tid = str(row["team_id"])
        start = row["valid_from"]
        open_hit = latest_open.get(pid)
        if open_hit is not None:
            open_team, open_start = open_hit
            # Player already has a later open membership elsewhere — transfer, leave closed.
            if open_team != tid and open_start is not None and start is not None and open_start > start:
                skipped_transfer += 1
                continue
            if open_team == tid:
                continue
        session.execute(
            text("UPDATE rosters SET valid_to = NULL WHERE roster_id = :rid AND valid_to = :ts"),
            {"rid": row["roster_id"], "ts": stamp.replace(tzinfo=None) if stamp.tzinfo else stamp},
        )
        reopened += 1
        latest_open[pid] = (tid, start)
    session.flush()
    return {
        "status": "ok",
        "closed_at": stamp.isoformat(),
        "active_players": len(active_ids),
        "reopened": reopened,
        "skipped_transfer": skipped_transfer,
        "closed_candidates": len(closed),
    }


def run_fbs_atd_data_repair(session: Session) -> dict[str, Any]:
    pgs = repair_null_pgs_team_ids(session, season_year=2026)
    roster = repair_false_roster_closures(session)
    session.commit()
    return {
        "status": "ok",
        "as_of": datetime.now(UTC).isoformat(),
        "null_pgs_team_id": pgs,
        "false_roster_closures": roster,
        "note": "Existing-data repair only. Freezes/settlements untouched. Validated Play remains off.",
    }
