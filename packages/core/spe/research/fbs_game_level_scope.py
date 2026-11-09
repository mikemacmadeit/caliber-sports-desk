"""FBS game-level projection scope — presentation guardrails only.

Locked C_recent / M5 score involved-conditional histories (touches > 0). They do
**not** model P(player participates | upcoming game). Presenting that score as a
normal ranked game-level Anytime TD projection is unsupported when current-team
evidence shows situational / backup usage.

This module does **not**:
- retrain or alter locked C_recent coefficients
- invent participation multipliers
- hard-code player exclusions
- manufacture zero-touch outcomes for missing box rows

It demotes unsupported estimates from ``primary_research`` while preserving the
raw research score for inspection under an explicit scope label.

A separately versioned role-aware model would need, at minimum:
- pregame depth / snap or participation probability (or a well-specified proxy)
- separation of P(ATD | participates) from P(participates)
- training labels aligned to the game-level target (not involved-only only)
- explicit handling of confirmed DNP vs missing box vs zero-touch appearances
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.db.base import as_utc

SCOPE_VERSION = "fbs_game_level_scope_v1"
INVOLVED_CONDITIONAL = "involved_conditional_not_game_level"
ROLE_UNKNOWN = "role_unknown_not_game_level"
GAME_LEVEL = "game_level_ok"

# Current-team season: need enough completed games to evidence regular vs situational use.
MIN_TEAM_GAMES_FOR_ROLE = 4
# Appeared with touches in at most this fraction of team finals → situational on current team.
MAX_INVOLVEMENT_RATE_FOR_SITUATIONAL = 0.5
# Teammate QB must show a real starter workload before we treat another QB as backup.
MIN_STARTER_QB_ATTEMPTS = 80


@dataclass(frozen=True)
class PlayerTeamSeasonUsage:
    player_id: str
    team_id: str
    season_year: int
    team_final_games: int
    involved_games: int
    total_attempts: int
    total_carries: int
    position_group: str | None


def _season_year_as_of(kickoff: datetime | None, now: datetime) -> int:
    moment = kickoff or now
    # CFB season year ≈ calendar year of August–December games; Jan bowls still prior year.
    if moment.month < 2:
        return moment.year - 1
    return moment.year


def load_fbs_team_season_usage(
    session: Session,
    *,
    season_year: int,
    as_of: datetime | None = None,
) -> tuple[dict[tuple[str, str], PlayerTeamSeasonUsage], dict[str, int], dict[str, set[str]]]:
    """Batch current-season involved counts and pass attempts by (player_id, team_id)."""
    moment = as_utc(as_of) or datetime.now(UTC)
    team_games = session.execute(
        text(
            """
            SELECT g.home_team_id AS team_id, COUNT(*) AS n
            FROM games g
            JOIN seasons s ON s.season_id = g.season_id
            WHERE g.league_id = 'fbs' AND s.year = :yr AND g.status = 'final'
              AND g.kickoff_utc < :as_of
            GROUP BY g.home_team_id
            UNION ALL
            SELECT g.away_team_id AS team_id, COUNT(*) AS n
            FROM games g
            JOIN seasons s ON s.season_id = g.season_id
            WHERE g.league_id = 'fbs' AND s.year = :yr AND g.status = 'final'
              AND g.kickoff_utc < :as_of
            GROUP BY g.away_team_id
            """
        ),
        {"yr": int(season_year), "as_of": moment.isoformat()},
    ).mappings().all()
    team_final: dict[str, int] = {}
    for row in team_games:
        tid = str(row["team_id"])
        team_final[tid] = team_final.get(tid, 0) + int(row["n"] or 0)

    # Per-game rows so null team_id can be resolved onto scoreboard/roster aliases.
    rows = session.execute(
        text(
            """
            SELECT p.player_id, p.team_id, pl.position_group,
                   g.home_team_id, g.away_team_id,
                   COALESCE(p.attempts, 0) AS attempts,
                   COALESCE(p.carries, 0) AS carries
            FROM player_game_stats p
            JOIN games g ON g.game_id = p.game_id
            JOIN seasons s ON s.season_id = g.season_id
            LEFT JOIN players pl ON pl.player_id = p.player_id
            WHERE g.league_id = 'fbs' AND s.year = :yr AND g.status = 'final'
              AND g.kickoff_utc < :as_of
              AND (
                    COALESCE(p.targets, 0) + COALESCE(p.receptions, 0) + COALESCE(p.carries, 0)
                    + ABS(COALESCE(p.rec_yards, 0)) + ABS(COALESCE(p.rush_yards, 0))
                  ) > 0
            """
        ),
        {"yr": int(season_year), "as_of": moment.isoformat()},
    ).mappings().all()

    from spe.db.models import Team
    from spe.integrity.assignments import linked_team_ids

    teams = {t.team_id: t for t in session.query(Team).filter(Team.league_id == "fbs").all()}
    alias_of: dict[str, set[str]] = {}
    for t in teams.values():
        group = linked_team_ids(t) | {t.team_id}
        for tid in group:
            alias_of[tid] = group | alias_of.get(tid, set())

    # Latest roster team per player (for null PGS.team_id resolution).
    roster_teams = session.execute(
        text(
            """
            SELECT player_id, team_id FROM rosters
            WHERE team_id LIKE 'fbs:%' OR team_id IN ('uga', 'ala', 'tex')
            ORDER BY valid_from DESC
            """
        )
    ).mappings().all()
    latest_roster: dict[str, list[str]] = {}
    for row in roster_teams:
        latest_roster.setdefault(str(row["player_id"]), []).append(str(row["team_id"]))

    def resolve_team(pid: str, raw_tid: Any, home: str, away: str) -> str | None:
        if raw_tid:
            return str(raw_tid)
        roster = latest_roster.get(pid) or []
        for side in (home, away):
            group = alias_of.get(side, {side})
            for rtid in roster:
                if rtid in group or side in alias_of.get(rtid, {rtid}):
                    # Prefer the scoreboard id so lookups by game.away_team_id hit.
                    return side
        return None

    # Aggregate (player, resolved_team).
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        pid = str(row["player_id"])
        tid = resolve_team(pid, row["team_id"], str(row["home_team_id"]), str(row["away_team_id"]))
        if not tid:
            continue
        key = (pid, tid)
        cur = agg.get(key)
        if cur is None:
            agg[key] = {
                "involved_games": 1,
                "total_attempts": int(row["attempts"] or 0),
                "total_carries": int(row["carries"] or 0),
                "position_group": row["position_group"],
            }
        else:
            cur["involved_games"] += 1
            cur["total_attempts"] += int(row["attempts"] or 0)
            cur["total_carries"] += int(row["carries"] or 0)

    out: dict[tuple[str, str], PlayerTeamSeasonUsage] = {}
    for (pid, tid), cur in agg.items():
        # Team finals: max across alias group (uga vs fbs:61 double-count risk if summed).
        group = alias_of.get(tid, {tid})
        finals = max((int(team_final.get(x, 0)) for x in group), default=int(team_final.get(tid, 0)))
        out[(pid, tid)] = PlayerTeamSeasonUsage(
            player_id=pid,
            team_id=tid,
            season_year=int(season_year),
            team_final_games=finals,
            involved_games=int(cur["involved_games"]),
            total_attempts=int(cur["total_attempts"]),
            total_carries=int(cur["total_carries"]),
            position_group=str(cur["position_group"]) if cur["position_group"] else None,
        )
    return out, team_final, alias_of


def load_fbs_usage_context(session: Session, *, season_year: int, as_of: datetime | None = None) -> dict[str, Any]:
    usage, team_final, alias_of = load_fbs_team_season_usage(session, season_year=season_year, as_of=as_of)
    # Max QB attempts per alias group (starter evidence across uga ↔ fbs:61).
    qb_leader: dict[str, dict[str, Any]] = {}
    for (pid, tid), u in usage.items():
        if (u.position_group or "").upper() != "QB":
            continue
        group = alias_of.get(tid, {tid})
        for key in group:
            cur = qb_leader.get(key)
            if cur is None or u.total_attempts > int(cur["attempts"]):
                qb_leader[key] = {"player_id": pid, "attempts": u.total_attempts}
    return {
        "usage": usage,
        "team_final": team_final,
        "qb_leader": qb_leader,
        "season_year": season_year,
        "alias_of": alias_of,
    }


def _usage_for_player_team(
    usage_map: dict[tuple[str, str], PlayerTeamSeasonUsage],
    *,
    player_id: str,
    team_id: str,
    alias_of: dict[str, set[str]],
) -> PlayerTeamSeasonUsage | None:
    group = alias_of.get(team_id, {team_id})
    best: PlayerTeamSeasonUsage | None = None
    for cand in group:
        u = usage_map.get((player_id, cand))
        if u is None:
            continue
        if best is None or u.involved_games > best.involved_games:
            best = u
    # Also accept usage stored under any alias key for this player.
    if best is None:
        for (pid, tid), u in usage_map.items():
            if pid != player_id:
                continue
            if tid in group or team_id in alias_of.get(tid, {tid}):
                if best is None or u.involved_games > best.involved_games:
                    best = u
    return best


def classify_fbs_projection_scope(
    *,
    player_id: str,
    team_id: str | None,
    position_group: str | None,
    ctx: dict[str, Any],
) -> dict[str, Any]:
    """Return scope decision for presenting a research score as game-level."""
    tid = str(team_id) if team_id else ""
    pid = str(player_id)
    alias_of: dict[str, set[str]] = ctx.get("alias_of") or {}
    group = alias_of.get(tid, {tid} if tid else set())
    team_final_map: dict[str, int] = ctx.get("team_final") or {}
    team_final = max((int(team_final_map.get(x, 0)) for x in group), default=int(team_final_map.get(tid, 0)))
    usage_map: dict[tuple[str, str], PlayerTeamSeasonUsage] = ctx.get("usage") or {}
    u = _usage_for_player_team(usage_map, player_id=pid, team_id=tid, alias_of=alias_of) if tid else None
    involved = int(u.involved_games) if u else 0
    attempts = int(u.total_attempts) if u else 0
    rate = (involved / team_final) if team_final > 0 else None
    pos = (position_group or (u.position_group if u else None) or "").upper()

    evidence: dict[str, Any] = {
        "scope_version": SCOPE_VERSION,
        "season_year": ctx.get("season_year"),
        "team_id": tid or None,
        "team_final_games": team_final,
        "current_team_involved_games": involved,
        "current_team_involvement_rate": rate,
        "current_team_pass_attempts": attempts,
        "position_group": pos or None,
        "usage_team_id": u.team_id if u else None,
    }

    if not tid or team_final < MIN_TEAM_GAMES_FOR_ROLE:
        return {
            "projection_scope": ROLE_UNKNOWN,
            "game_level_supported": False,
            "reason_codes": ["insufficient_current_team_season_evidence"],
            "evidence": evidence,
        }

    reason_codes: list[str] = []

    # Backup QB: teammate has a clear starter attempt lead. Not a hard exclusion of all QBs.
    leader = (ctx.get("qb_leader") or {}).get(tid)
    if pos == "QB" and leader and leader.get("player_id") != pid:
        lead_att = int(leader.get("attempts") or 0)
        evidence["teammate_qb_attempt_leader_id"] = leader.get("player_id")
        evidence["teammate_qb_attempt_leader_attempts"] = lead_att
        if lead_att >= MIN_STARTER_QB_ATTEMPTS and attempts < lead_att:
            reason_codes.append("documented_backup_qb_attempt_share")

    if rate is not None and rate <= MAX_INVOLVEMENT_RATE_FOR_SITUATIONAL:
        reason_codes.append("situational_current_team_involvement")

    if reason_codes:
        return {
            "projection_scope": INVOLVED_CONDITIONAL,
            "game_level_supported": False,
            "reason_codes": reason_codes,
            "evidence": evidence,
        }

    return {
        "projection_scope": GAME_LEVEL,
        "game_level_supported": True,
        "reason_codes": [],
        "evidence": evidence,
    }


def apply_scope_to_research_player(player: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    """Demote unsupported game-level primary; preserve raw C_recent / M5 for inspection."""
    out = dict(player)
    out["projection_scope"] = scope.get("projection_scope")
    out["game_level_supported"] = bool(scope.get("game_level_supported"))
    out["projection_scope_evidence"] = scope.get("evidence")
    out["projection_scope_reasons"] = list(scope.get("reason_codes") or [])
    out["scope_version"] = SCOPE_VERSION

    if scope.get("game_level_supported"):
        return out

    # Preserve raw research outputs; clear only the ranked game-level primary.
    raw_c = out.get("probabilities", {}).get("c_recent") if isinstance(out.get("probabilities"), dict) else out.get("p_c_recent")
    if isinstance(out.get("probabilities"), dict):
        probs = dict(out["probabilities"])
        if raw_c is not None:
            probs["c_recent_raw_involved_conditional"] = raw_c
        # Keep c_recent visible for inspection, but primary_research must not rank it as game-level.
        probs["primary_research"] = None
        probs["primary_suppressed_reason"] = scope.get("projection_scope")
        out["probabilities"] = probs
    else:
        out["p_c_recent_raw_involved_conditional"] = out.get("p_c_recent")
        # enrich_player_for_finder reads p_c_recent then sets primary — mark suppression flag.
        out["suppress_game_level_primary"] = True

    reasons = ", ".join(scope.get("reason_codes") or []) or "model_lacks_participation_term"
    out["primary_model_label"] = (
        "C_recent — involved-conditional (not a game-level projection)"
    )
    warnings = list(out.get("research_warnings") or [])
    warnings.append(
        {
            "code": "involved_conditional_not_game_level",
            "message": (
                "Locked C_recent estimates P(ATD | involved history features); it does not "
                f"model current-role participation ({reasons}). Raw score retained for inspection "
                "only — not ranked as a game-level projection. A role-aware model would need an "
                "explicit P(participates) term and game-level training labels."
            ),
        }
    )
    out["research_warnings"] = warnings
    missing = list(out.get("missing_information") or [])
    if "fbs_no_game_level_participation_model" not in missing:
        missing.append("fbs_no_game_level_participation_model")
    out["missing_information"] = missing
    # Version the presentation layer separately from locked model weights.
    pipe = str(out.get("pipeline_version") or "")
    if SCOPE_VERSION not in pipe:
        out["pipeline_version"] = (pipe + f"+{SCOPE_VERSION}") if pipe else SCOPE_VERSION
    return out
