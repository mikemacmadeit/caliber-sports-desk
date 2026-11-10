"""Game-week eligible players. Never treat multi-season ``rosters`` as the active set."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.db.models import Game, Outcome, Player, PlayerGameStat, Roster, RosterWeek, Season, Team
from spe.integrity.assignments import linked_team_ids, participating_ids

# nflverse status codes that may appear in a game-week population.
_ELIGIBLE_NFL = {"ACT", "ACTIVE", "active"}
# Explicitly excluded from simulation / current-game participation.
_INELIGIBLE_NFL = {"RET", "CUT", "INA", "DEV", "PUP", "RES", "SUS", "NON", "EXE", "UDF", "RFA"}


@dataclass(frozen=True)
class EligiblePlayer:
    player_id: str
    team_id: str
    name: str
    position_group: str
    status: str
    source: str
    eligibility: str = "eligible"  # eligible | uncertain | confirmed_ineligible (excluded before return)
    role: str = "unspecified"


def season_year_for_game(session: Session, game: Game) -> int | None:
    season = session.get(Season, game.season_id)
    if season is not None:
        return int(season.year)
    if game.season_id and "-" in game.season_id:
        try:
            return int(game.season_id.rsplit("-", 1)[-1])
        except ValueError:
            return None
    return None


def eligible_players_for_game(
    session: Session,
    game: Game,
    *,
    as_of: datetime | None = None,
    include_inactive_for_props: bool = False,
    apply_eligibility_audit: bool = True,
) -> list[EligiblePlayer]:
    """Players who belong to either club at kickoff.

    NFL: ``roster_weeks`` for the game's season/week with ACT status (plus linked
    team ids for WAS/WSH and LA/LAR). FBS: open ``rosters`` rows at kickoff with
    status active; never dumps the entire historical roster table.

    Set ``apply_eligibility_audit=False`` for latency-sensitive paths (simulations)
    that already filter inactive/RET statuses from roster_weeks.
    """
    teams = {
        t.team_id: t
        for t in session.query(Team).filter(Team.team_id.in_([game.home_team_id, game.away_team_id])).all()
    }
    # Load alias twins so linked_team_ids can expand.
    extras: set[str] = set()
    for tid in list(teams):
        extras |= linked_team_ids(teams[tid])
    if extras - set(teams):
        for t in session.query(Team).filter(Team.team_id.in_(extras - set(teams))).all():
            teams[t.team_id] = t
    participant_ids = participating_ids(game, teams)
    kickoff = as_utc(as_of or game.kickoff_utc) or game.kickoff_utc

    if game.league_id == "nfl":
        return _nfl_week_roster(
            session,
            game,
            participant_ids,
            include_inactive_for_props=include_inactive_for_props,
            apply_eligibility_audit=apply_eligibility_audit,
        )
    year = season_year_for_game(session, game)
    base = _open_roster_at_kickoff(session, participant_ids, kickoff, season_year=year)
    supplemented = _supplement_fbs_eligible_players(
        session,
        game,
        participant_ids=participant_ids,
        kickoff=kickoff,
        season_year=year,
        base=base,
        # Props only needs priced names recovered from outcomes; season box scan is for
        # research scoring of unpriced skill players and is too heavy for /v1/props.
        include_season_box=not include_inactive_for_props,
    )
    from spe.identity.roles import annotate_fbs_roles

    return annotate_fbs_roles(session, game, supplemented)


def _nfl_week_roster(
    session: Session,
    game: Game,
    participant_ids: set[str],
    *,
    include_inactive_for_props: bool,
    apply_eligibility_audit: bool = True,
) -> list[EligiblePlayer]:
    year = season_year_for_game(session, game)
    week = game.week
    if year is None or week is None:
        # Fall back to latest roster_week for the clubs in that season year.
        return _latest_nfl_roster(session, participant_ids, year, include_inactive_for_props=include_inactive_for_props)

    rows = (
        session.query(RosterWeek, Player)
        .join(Player, Player.player_id == RosterWeek.player_id)
        .filter(
            RosterWeek.season_year == year,
            RosterWeek.week == week,
            RosterWeek.team_id.in_(participant_ids),
        )
        .all()
    )
    # Upcoming kickoffs often land before nflverse publishes that week's file.
    # Prefer an exact week when present; otherwise use the latest published week
    # for these clubs so live props can still resolve names.
    if not rows:
        return _latest_nfl_roster(
            session,
            participant_ids,
            year,
            include_inactive_for_props=include_inactive_for_props,
        )
    labels = _nfl_eligibility_labels(session, year, week) if apply_eligibility_audit else {}
    seen: set[str] = set()
    out: list[EligiblePlayer] = []
    for rw, player in rows:
        status = (rw.status or "").upper()
        if not include_inactive_for_props:
            if status and status not in _ELIGIBLE_NFL:
                continue
        elif status in {"RET"}:
            continue
        label = labels.get(player.player_id, "eligible")
        # Never surface confirmed ineligible players as active participants.
        if label == "confirmed_ineligible":
            continue
        if player.player_id in seen:
            continue
        seen.add(player.player_id)
        out.append(
            EligiblePlayer(
                player_id=player.player_id,
                team_id=rw.team_id,
                name=player.name,
                position_group=player.position_group,
                status=status or "ACT",
                source="roster_weeks",
                eligibility=label,
            )
        )
    return out


def _nfl_eligibility_labels(session: Session, season_year: int, week: int) -> dict[str, str]:
    """Map player_id → eligible | uncertain | confirmed_ineligible for a week."""
    from spe.models.eligibility_audit import audit_nfl_week

    audit = audit_nfl_week(session, season_year=season_year, week=week)
    labels: dict[str, str] = {}
    for row in audit["confirmed_ineligible"]:
        labels[row["player_id"]] = "confirmed_ineligible"
    for row in audit["uncertain"]:
        labels[row["player_id"]] = "uncertain"
    return labels


def _latest_nfl_roster(
    session: Session,
    participant_ids: set[str],
    year: int | None,
    *,
    include_inactive_for_props: bool,
) -> list[EligiblePlayer]:
    q = session.query(RosterWeek).filter(RosterWeek.team_id.in_(participant_ids))
    if year is not None:
        q = q.filter(RosterWeek.season_year == year)
    rows = q.all()
    if not rows:
        return []
    max_week = max(r.week for r in rows)
    max_year = max(r.season_year for r in rows)
    latest = [r for r in rows if r.season_year == max_year and r.week == max_week]
    labels = _nfl_eligibility_labels(session, max_year, max_week)
    players = {
        p.player_id: p
        for p in session.query(Player).filter(Player.player_id.in_({r.player_id for r in latest})).all()
    }
    out: list[EligiblePlayer] = []
    seen: set[str] = set()
    for rw in latest:
        status = (rw.status or "").upper()
        if not include_inactive_for_props and status and status not in _ELIGIBLE_NFL:
            continue
        if status == "RET":
            continue
        if labels.get(rw.player_id) == "confirmed_ineligible":
            continue
        player = players.get(rw.player_id)
        if player is None or rw.player_id in seen:
            continue
        seen.add(rw.player_id)
        out.append(
            EligiblePlayer(
                player_id=rw.player_id,
                team_id=rw.team_id,
                name=player.name,
                position_group=player.position_group,
                status=status or "ACT",
                source="roster_weeks_latest",
                eligibility=labels.get(rw.player_id, "eligible"),
            )
        )
    return out


def _open_roster_at_kickoff(
    session: Session,
    participant_ids: set[str],
    kickoff: datetime,
    *,
    season_year: int | None = None,
) -> list[EligiblePlayer]:
    """FBS / fallback: only roster rows whose validity window covers kickoff.

    Prior-season open rows are excluded when ``season_year`` is known. When the
    current season has no rows yet, the latest available season year for the
    clubs is used — never the full multi-year dump.
    """
    rows = (
        session.query(Roster, Player)
        .join(Player, Player.player_id == Roster.player_id)
        .filter(Roster.team_id.in_(participant_ids))
        .all()
    )
    kick = as_utc(kickoff)
    candidates: list[tuple[Roster, Player]] = []
    years_by_team: dict[str, set[int]] = {}
    for roster, player in rows:
        start = as_utc(roster.valid_from)
        end = as_utc(roster.valid_to)
        if start is None or kick is None or start > kick:
            continue
        if end is not None and end <= kick:
            continue
        status = (roster.status or "active").lower()
        if status in {"retired", "inactive", "cut", "released"}:
            continue
        candidates.append((roster, player))
        years_by_team.setdefault(roster.team_id, set()).add(start.year)

    # Latest season year available at or before the game season. Incomplete
    # mid-season refreshes (a handful of 2026 rows) must not hide the prior
    # full roster when that is all CFBD has delivered.
    keep_year_by_team: dict[str, int] = {}
    for team_id, years in years_by_team.items():
        usable = {y for y in years if season_year is None or y <= season_year}
        if not usable:
            usable = set(years)
        # Prefer a year with a real roster (≥20) over a thin refresh year.
        ranked = sorted(usable, reverse=True)
        chosen = ranked[0]
        counts = {
            y: sum(
                1
                for roster, _player in candidates
                if roster.team_id == team_id and (as_utc(roster.valid_from) or roster.valid_from).year == y
            )
            for y in ranked
        }
        for y in ranked:
            if counts.get(y, 0) >= 20:
                chosen = y
                break
        keep_year_by_team[team_id] = chosen

    # Prefer keep-year rows, but never drop still-open earlier rows for the same
    # club. CFBD often stamps transfers in the new season year while leaving
    # continuing starters on last year's valid_from with valid_to=NULL; exclusive
    # year filtering then erases starters (and their odds) from the board.
    preferred: dict[str, tuple[Roster, Player]] = {}
    continuing: dict[str, tuple[Roster, Player]] = {}
    for roster, player in candidates:
        start = as_utc(roster.valid_from)
        keep = keep_year_by_team.get(roster.team_id)
        if keep is not None and start is not None and start.year > keep:
            continue
        if keep is not None and start is not None and start.year < keep:
            end = as_utc(roster.valid_to)
            if end is not None:
                # Closed prior-year membership — not current.
                continue
            continuing.setdefault(player.player_id, (roster, player))
            continue
        preferred[player.player_id] = (roster, player)

    merged = {**continuing, **preferred}
    # Drop players whose latest open membership is on another club (transfer still
    # open on the prior team). Avoids backups/starters following a player to the
    # wrong scoreboard when multi-open rows were never closed.
    if merged:
        open_elsewhere = (
            session.query(Roster)
            .filter(
                Roster.player_id.in_(list(merged.keys())),
                Roster.valid_to.is_(None),
            )
            .all()
        )
        latest_open: dict[str, Roster] = {}
        for row in open_elsewhere:
            kick_ok = True
            start = as_utc(row.valid_from)
            if kick is not None and start is not None and start > kick:
                kick_ok = False
            if not kick_ok:
                continue
            prev = latest_open.get(row.player_id)
            prev_start = as_utc(prev.valid_from) if prev is not None else None
            if prev is None or (start is not None and (prev_start is None or start >= prev_start)):
                latest_open[row.player_id] = row
        merged = {
            pid: pair
            for pid, pair in merged.items()
            if latest_open.get(pid) is None or latest_open[pid].team_id == pair[0].team_id
        }

    out: list[EligiblePlayer] = []
    for _player_id, (roster, player) in merged.items():
        out.append(
            EligiblePlayer(
                player_id=player.player_id,
                team_id=roster.team_id,
                name=player.name,
                position_group=player.position_group,
                status=roster.status or "active",
                source="rosters_point_in_time",
            )
        )
    return out


def _infer_participant_team_id(
    session: Session,
    player_id: str,
    participant_ids: set[str],
    *,
    kickoff: datetime,
    season_year: int | None,
) -> str | None:
    """Best team id among participants for a player missing from open roster."""
    rows = (
        session.query(Roster)
        .filter(Roster.player_id == player_id, Roster.team_id.in_(participant_ids))
        .all()
    )
    kick = as_utc(kickoff)
    ranked: list[tuple[datetime, str]] = []
    for row in rows:
        start = as_utc(row.valid_from)
        if start is None:
            continue
        if kick is not None and start > kick:
            continue
        ranked.append((start, row.team_id))
    if ranked:
        ranked.sort(key=lambda x: x[0])
        return ranked[-1][1]

    # Fall back to current-season box scores on participating clubs.
    if season_year is None:
        return None
    from spe.integrity.assignments import linked_team_ids as _linked

    participant_group: set[str] = set(participant_ids)
    for tid in list(participant_ids):
        t = session.get(Team, tid)
        if t is not None:
            participant_group |= _linked(t)

    stats = (
        session.query(PlayerGameStat, Game)
        .join(Game, Game.game_id == PlayerGameStat.game_id)
        .filter(
            PlayerGameStat.player_id == player_id,
            Game.status == "final",
        )
        .all()
    )
    for pgs, g in stats:
        g_year = season_year_for_game(session, g)
        if g_year != season_year:
            continue
        tid = pgs.team_id
        if tid and tid in participant_group:
            # Map alias hist team onto the scoreboard participant id when possible.
            if tid in participant_ids:
                return tid
            for side in participant_ids:
                if tid in (_linked(session.get(Team, side)) | {side}):
                    return side
            return tid
        if tid:
            continue
        # Null team_id: only accept a participant side that matches this player's roster.
        roster_ids = {
            str(r.team_id)
            for r in session.query(Roster).filter(Roster.player_id == player_id).all()
        }
        for side in (g.home_team_id, g.away_team_id):
            group = _linked(session.get(Team, side)) | {side}
            if roster_ids & group and side in participant_group:
                if side in participant_ids:
                    return side
                for board in participant_ids:
                    if side in (_linked(session.get(Team, board)) | {board}):
                        return board
    return None


def _supplement_fbs_eligible_players(
    session: Session,
    game: Game,
    *,
    participant_ids: set[str],
    kickoff: datetime,
    season_year: int | None,
    base: list[EligiblePlayer],
    include_season_box: bool = True,
) -> list[EligiblePlayer]:
    """Add priced / season-active players erased by false prior-year roster closures.

    A bad FBS roster sync closed continuing starters when a partial new-year
    refresh appeared. Open-roster eligibility alone then drops those players —
    and their sportsbook markets — from research boards. Supplement from
    (1) anytime_td outcomes already stored for this game and (2) involved
    current-season box scores on participating clubs.
    """
    by_id: dict[str, EligiblePlayer] = {e.player_id: e for e in base}

    from sqlalchemy import or_

    outcome_pids = {
        str(pid)
        for (pid,) in session.query(Outcome.player_id)
        .filter(
            Outcome.game_id == game.game_id,
            Outcome.market_id == "anytime_td",
            Outcome.player_id.isnot(None),
        )
        .all()
        if pid
    }

    season_involved: set[str] = set()
    if include_season_box and season_year is not None and participant_ids:
        from spe.integrity.assignments import linked_team_ids as _linked

        season_start = datetime(season_year, 7, 1, tzinfo=UTC)
        season_end = datetime(season_year + 1, 2, 1, tzinfo=UTC)
        # Expand participants with aliases (uga ↔ fbs:61) for hist team_id matches.
        participant_group: set[str] = set(participant_ids)
        for tid in list(participant_ids):
            t = session.get(Team, tid)
            if t is not None:
                participant_group |= _linked(t)

        # Roster teams for players we may recover — used when PGS.team_id is null.
        roster_by_player: dict[str, set[str]] = {}
        for row in (
            session.query(Roster.player_id, Roster.team_id)
            .filter(Roster.team_id.in_(participant_group))
            .all()
        ):
            roster_by_player.setdefault(str(row[0]), set()).add(str(row[1]))

        qrows = (
            session.query(PlayerGameStat, Game)
            .join(Game, Game.game_id == PlayerGameStat.game_id)
            .filter(
                Game.status == "final",
                Game.kickoff_utc >= season_start,
                Game.kickoff_utc < season_end,
                or_(
                    PlayerGameStat.team_id.in_(participant_group),
                    # Null team_id rows still need a join path; filter ownership below.
                    PlayerGameStat.team_id.is_(None),
                ),
            )
            .all()
        )
        for pgs, g in qrows:
            touches = float(pgs.carries or 0) + float(pgs.attempts or 0) + float(pgs.receptions or 0)
            if touches <= 0:
                continue
            pid = str(pgs.player_id)
            if pgs.team_id:
                if str(pgs.team_id) not in participant_group:
                    continue
            else:
                # Null team_id: only if this player's roster is on a CURRENT participant.
                # Do NOT treat "participant was the opponent in a past game" as membership
                # (that wrongly put Gunner Stockton on Arkansas after UGA@Auburn).
                roster_teams = roster_by_player.get(pid) or set()
                if not (roster_teams & participant_group):
                    continue
                # And the historical game must involve that roster club (not random null rows).
                hist_sides = {g.home_team_id, g.away_team_id}
                hist_group: set[str] = set()
                for side in hist_sides:
                    hist_group.add(side)
                    st = session.get(Team, side)
                    if st is not None:
                        hist_group |= _linked(st)
                if not (roster_teams & hist_group):
                    continue
            season_involved.add(pid)

    need = (outcome_pids | season_involved) - set(by_id)
    if not need:
        return base

    players = {
        p.player_id: p
        for p in session.query(Player).filter(Player.player_id.in_(list(need))).all()
    }
    for pid in need:
        player = players.get(pid)
        if player is None:
            continue
        team_id = _infer_participant_team_id(
            session,
            pid,
            participant_ids,
            kickoff=kickoff,
            season_year=season_year,
        )
        if team_id is None:
            continue
        source = "game_market_outcome" if pid in outcome_pids else "season_box_score"
        by_id[pid] = EligiblePlayer(
            player_id=pid,
            team_id=team_id,
            name=player.name,
            position_group=player.position_group,
            status="active",
            source=source,
        )
    return list(by_id.values())


def roster_summary(session: Session, game: Game) -> dict[str, Any]:
    eligible = eligible_players_for_game(session, game)
    by_team: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for row in eligible:
        by_team[row.team_id] = by_team.get(row.team_id, 0) + 1
        by_status[row.status] = by_status.get(row.status, 0) + 1
    historical = (
        session.query(Roster)
        .filter(Roster.team_id.in_([game.home_team_id, game.away_team_id]))
        .count()
    )
    return {
        "game_id": game.game_id,
        "eligible_count": len(eligible),
        "by_team": by_team,
        "by_status": by_status,
        "historical_roster_table_rows": historical,
        "sample": [
            {"player_id": p.player_id, "name": p.name, "team_id": p.team_id, "pos": p.position_group, "status": p.status}
            for p in eligible[:20]
        ],
    }


def sync_current_rosters(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Close contaminated open ``rosters`` rows and open the current NFL week ACT set.

    FBS: close multi-open memberships, keeping the latest ``valid_from`` only.
    Historical ``roster_weeks`` rows are preserved; this only rewrites the open
    ``rosters`` table used by odds matching and FBS eligibility.
    """
    now = now or datetime.now(UTC)
    nfl = _sync_nfl_open_rosters(session, now=now)
    fbs = _sync_fbs_open_rosters(session, now=now)
    return {"nfl": nfl, "fbs": fbs, "as_of": now.isoformat()}


def _sync_fbs_open_rosters(session: Session, *, now: datetime) -> dict[str, Any]:
    """Close contaminated FBS open memberships without erasing continuing starters.

    Transfers often keep ``valid_from`` on the prior season year with
    ``valid_to=NULL``. Mass-closing every non-keep-year row deletes those
    starters from eligibility (and hides their sportsbook markets). Only close
    a prior-year open row when the same player already has a keep-year row on
    that club, then resolve multi-team open memberships.
    """
    open_rows = (
        session.query(Roster)
        .filter(Roster.valid_to.is_(None), Roster.team_id.like("fbs:%"))
        .all()
    )
    if not open_rows:
        return {"closed_prior_season": 0, "closed_extra_memberships": 0, "open_remaining": 0}
    by_team: dict[str, list[Roster]] = {}
    for row in open_rows:
        by_team.setdefault(row.team_id, []).append(row)
    closed_prior = 0
    for _team_id, rows in by_team.items():
        counts: dict[int, int] = {}
        for row in rows:
            start = as_utc(row.valid_from) or row.valid_from
            if start is None:
                continue
            counts[start.year] = counts.get(start.year, 0) + 1
        if not counts:
            continue
        ranked = sorted(counts, reverse=True)
        keep_year = ranked[0]
        for year in ranked:
            if counts[year] >= 20:
                keep_year = year
                break
        keep_players = {
            row.player_id
            for row in rows
            if (as_utc(row.valid_from) or row.valid_from) is not None
            and (as_utc(row.valid_from) or row.valid_from).year == keep_year
        }
        for row in rows:
            start = as_utc(row.valid_from) or row.valid_from
            if start is None or start.year == keep_year:
                continue
            # Replaced on this club in keep_year — prior open stamp is stale.
            if row.player_id in keep_players:
                row.valid_to = now
                closed_prior += 1
    session.flush()
    extras = _close_multi_open_memberships(session, league_prefix="fbs:", now=now)
    remaining = (
        session.query(Roster)
        .filter(Roster.valid_to.is_(None), Roster.team_id.like("fbs:%"))
        .count()
    )
    return {
        "closed_prior_season": closed_prior,
        "closed_extra_memberships": extras.get("closed_extra_memberships", 0),
        "open_remaining": remaining,
    }


def _sync_nfl_open_rosters(session: Session, *, now: datetime) -> dict[str, Any]:
    latest = (
        session.query(RosterWeek.season_year, RosterWeek.week)
        .order_by(RosterWeek.season_year.desc(), RosterWeek.week.desc())
        .first()
    )
    if latest is None:
        closed = _close_open_prefix(session, "nfl:", now=now)
        return {"status": "no_roster_weeks", "closed": closed, "opened": 0}

    year, week = int(latest[0]), int(latest[1])
    closed = _close_open_prefix(session, "nfl:", now=now)
    rows = (
        session.query(RosterWeek)
        .filter(RosterWeek.season_year == year, RosterWeek.week == week)
        .all()
    )
    opened = 0
    seen: set[tuple[str, str]] = set()
    for rw in rows:
        status = (rw.status or "").upper()
        if status and status not in _ELIGIBLE_NFL:
            continue
        if session.get(Player, rw.player_id) is None:
            continue
        key = (rw.team_id, rw.player_id)
        if key in seen:
            continue
        seen.add(key)
        session.add(
            Roster(
                team_id=rw.team_id,
                player_id=rw.player_id,
                valid_from=now,
                valid_to=None,
                status="active",
                jersey=rw.jersey,
            )
        )
        opened += 1
        if opened % 2000 == 0:
            session.flush()
    session.flush()
    return {
        "status": "ok",
        "season_year": year,
        "week": week,
        "closed": closed,
        "opened": opened,
    }


def _close_open_prefix(session: Session, prefix: str, *, now: datetime) -> int:
    rows = (
        session.query(Roster)
        .filter(Roster.valid_to.is_(None), Roster.team_id.like(f"{prefix}%"))
        .all()
    )
    for row in rows:
        row.valid_to = now
    session.flush()
    return len(rows)


def _close_multi_open_memberships(session: Session, *, league_prefix: str, now: datetime) -> dict[str, Any]:
    open_rows = (
        session.query(Roster)
        .filter(Roster.valid_to.is_(None), Roster.team_id.like(f"{league_prefix}%"))
        .all()
    )
    by_player: dict[str, list[Roster]] = {}
    for row in open_rows:
        by_player.setdefault(row.player_id, []).append(row)
    closed = 0
    for rows in by_player.values():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: as_utc(r.valid_from) or r.valid_from)
        for stale in rows[:-1]:
            stale.valid_to = now
            closed += 1
    session.flush()
    return {"closed_extra_memberships": closed, "open_players": len(by_player)}
