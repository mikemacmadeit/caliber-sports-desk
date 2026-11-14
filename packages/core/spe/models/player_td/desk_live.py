"""Database-backed Desk research scoring and grounded explanations."""
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from spe.db.base import as_utc
from spe.db.models import Team
from spe.market.odds import ev_per_dollar
from spe.integrity.assignments import canonical_team_map
from spe.models.player_td.desk_opportunity import (
    VERSION, OpportunityHistory, canonical_team, explain, load_model, predict, prepare_history,
)


HISTORY_SQL = """
WITH ranked AS (
 SELECT g.game_id,s.player_id,coalesce(s.team_id,r.team_id) AS team_id,g.kickoff_utc,
 g.home_team_id,g.away_team_id,p.position_group AS position,s.provider,
 s.carries,s.targets,s.receptions,s.rush_td,s.rec_td,s.gl_carries,s.rz_targets,p.name AS player_name,
 ROW_NUMBER() OVER (PARTITION BY g.game_id,s.player_id ORDER BY
 CASE WHEN s.provider IN ('nflverse','cfbd') THEN 0 ELSE 1 END,
 s.revision DESC,s.ingested_at DESC,r.revision DESC) AS rn
 FROM player_game_stats s JOIN games g ON g.game_id=s.game_id
 JOIN players p ON p.player_id=s.player_id JOIN seasons se ON se.season_id=g.season_id
 LEFT JOIN roster_weeks r ON g.league_id='nfl' AND s.team_id IS NULL
 AND r.player_id=s.player_id AND r.season_year=se.year AND r.week=g.week
 AND r.team_id IN (g.home_team_id,g.away_team_id)
 WHERE g.league_id=:league AND g.status='final' AND g.data_tier NOT IN ('demo','alias')
 AND g.kickoff_utc>=:start AND g.kickoff_utc<:cutoff
 AND p.position_group IN ('QB','RB','WR','TE','FB')
 AND coalesce(s.team_id,r.team_id) IS NOT NULL
 AND s.quality_status NOT IN ('quarantined','invalid')
), charted AS (
 SELECT DISTINCT s.game_id FROM player_game_stats s JOIN games g ON s.game_id=g.game_id
 WHERE s.provider='nflverse' AND s.quality_status='pbp_opportunities'
 AND g.kickoff_utc>=:start AND g.kickoff_utc<:cutoff
)
SELECT q.*,CASE WHEN c.game_id IS NOT NULL THEN 1 ELSE 0 END AS charted
FROM ranked q LEFT JOIN charted c ON c.game_id=q.game_id WHERE q.rn=1
"""


def load_history(db, league, cutoff):
    start = cutoff.replace(month=7, day=1, hour=0, minute=0, second=0, microsecond=0)
    if cutoff.month < 7:
        start = start.replace(year=start.year - 1)
    rows = [dict(r) for r in db.execute(text(HISTORY_SQL), {
        "league": league, "start": start, "cutoff": cutoff - timedelta(hours=6),
    }).mappings()]
    for row in rows:
        if not row.pop("charted"):
            row["gl_carries"] = row["rz_targets"] = None
    return rows


def enrich_desk_rows(db, rows, now=None):
    """Score the research Desk from measured opportunities; official models stay untouched."""
    now = as_utc(now) or datetime.now(UTC)
    atd = [r for r in rows if r.get("market_id") == "anytime_td"]
    if not atd:
        return
    aliases = canonical_team_map(db.query(Team).all())
    for league in {r["league_id"] for r in atd}:
        model = load_model(league)
        history = OpportunityHistory(prepare_history(load_history(db, league, now), aliases))
        for row in [r for r in atd if r["league_id"] == league]:
            kick = as_utc(row.get("kickoff_utc"))
            team = canonical_team(row.get("team_id"), aliases)
            opp = canonical_team(row.get("opponent_id"), aliases)
            cutoff = min(now, kick) if kick else now
            f = history.features(row.get("player_id"), team, opp, row.get("position"), cutoff,
                                 row.get("team") == row.get("home_abbr"))
            reasons, risks = explain(f, league) if f else ([], ["Fewer than two usable prior games for the player, team, or opponent."])
            status = str((row.get("forecast") or {}).get("availability_status") or "unknown").lower()
            blockers = []
            if model is None:
                blockers.append("opportunity_model_unavailable")
            if f is None:
                blockers.append("insufficient_current_role_history")
            elif f["carries"] + f["receptions"] < 1:
                blockers.append("minimal_recent_workload")
            if f and (cutoff - as_utc(f["last_played_at"])).days > 21:
                blockers.append("role_history_outdated")
            if status in {"out", "doubtful", "confirmed_inactive", "inactive", "suspended", "questionable", "uncertain"}:
                blockers.append("availability_requires_review")
            if status not in {"active", "healthy", "probable", "available"}:
                risks.append("Availability is not confirmed; check the final inactive report.")
            chance = predict(model, f) if model and f else None
            row["desk_analysis"] = {
                "version": VERSION, "as_of": now.isoformat(), "features": f,
                "candidate_probability": chance,
                "candidate_status": "experimental_research", "reasons": reasons, "risks": risks,
                "eligible": not blockers, "blockers": blockers,
                "selection_probability": chance,
            }
            if chance is not None:
                row["usage"] = {"games": f["games"], "carries": f["carries"],
                    "targets": f["targets"] if league == "nfl" else None,
                    "receptions": f["receptions"],
                    "gl_carries": f["gl_carries"] if f["charting_known"] else None,
                    "rz_targets": f["rz_targets"] if f["charting_known"] else None}
                prior = dict(row.get("forecast") or {})
                row["forecast"] = {**prior, "base_probability": prior.get("p_estimate"),
                    "p_estimate": chance, "confidence_interval": (0.0, 1.0),
                    "uncertainty_status": "unvalidated", "model_version": VERSION + "-" + league + "-" + model.get("artifact_sha256", "test")[:10],
                    "prediction_as_of": now.isoformat(), "data_cutoff": now.isoformat(),
                    "tonight": None, "matchup": None, "research_only": True,
                    "validated_play": False, "play_enabled": False, "data_tier": "research",
                    "recommendation_status": "no_bet", "ev_after_haircut": None,
                    "ev": ev_per_dollar(chance, int(row["book_price"])) if row.get("book_price") and abs(row["book_price"]) >= 100 else None,
                    "reason_codes": list(dict.fromkeys([*prior.get("reason_codes", []), "DESK_EXPERIMENTAL", "PLAY_DISABLED"]))}
                row["data_tier"] = "research"
    # One decision for the whole slate, before any UI filtering or sorting.
    from spe.shadow.atd_desk_takes import select_desk_takes
    selected = {r["outcome_id"] for r in select_desk_takes(atd)}
    for row in atd:
        row["desk_selected"] = row["outcome_id"] in selected
