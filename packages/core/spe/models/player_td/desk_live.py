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
from spe.models.player_td import desk_hybrid


HISTORY_SQL = """
WITH ranked AS (
 SELECT g.game_id,s.player_id,coalesce(s.team_id,r.team_id) AS team_id,g.kickoff_utc,
 g.home_team_id,g.away_team_id,p.position_group AS position,s.provider,
 s.carries,s.targets,s.receptions,s.rush_td,s.rec_td,p.name AS player_name,
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
 SELECT s.game_id,s.player_id,s.gl_carries,s.rz_targets,
 ROW_NUMBER() OVER (PARTITION BY s.game_id,s.player_id ORDER BY s.revision DESC,s.ingested_at DESC) AS rn
 FROM player_game_stats s JOIN games g ON s.game_id=g.game_id
 WHERE s.provider='nflverse' AND s.quality_status='pbp_opportunities'
 AND g.kickoff_utc>=:start AND g.kickoff_utc<:cutoff
)
SELECT q.*,c.gl_carries,c.rz_targets
FROM ranked q LEFT JOIN charted c ON c.game_id=q.game_id AND c.player_id=q.player_id AND c.rn=1
WHERE q.rn=1
"""


def load_history(db, league, cutoff, *, start=None):
    season_start = cutoff.replace(month=7, day=1, hour=0, minute=0, second=0, microsecond=0)
    if cutoff.month < 7:
        season_start = season_start.replace(year=season_start.year - 1)
    start = start or season_start
    rows = [dict(r) for r in db.execute(text(HISTORY_SQL), {
        "league": league, "start": start, "cutoff": cutoff - timedelta(hours=6),
    }).mappings()]
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
        hybrid = desk_hybrid.load_hybrid(league, now)
        hybrid_games = {}
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
            if hybrid and kick and now < kick:
                key = (row["game_id"], team)
                if key not in hybrid_games:
                    hybrid_games[key] = _hybrid_game(db, row, history, hybrid, aliases, cutoff)
                alternative = hybrid_games[key]
                pid = row.get("player_id")
                if alternative and pid in alternative["players"] and f:
                    allocation = alternative["players"][pid]
                    player_p = desk_hybrid.predict(hybrid["player_model"], f)
                    row["desk_analysis"]["hybrid"] = {
                        "version": desk_hybrid.VERSION, "artifact_sha256": hybrid["artifact_sha256"],
                        "probability": desk_hybrid.blended_probability(hybrid, player_p, allocation["team_probability"]),
                        "player_probability": player_p, "team_probability": allocation["team_probability"],
                        "team_weight": hybrid["team_weight"], "calibration_end": hybrid["calibration_end"],
                        "team_expected_tds": alternative["team_expected_tds"],
                        "unallocated_expected_tds": alternative["unallocated_expected_tds"],
                        "pool_player_ids": alternative["pool_player_ids"],
                        "pool_snapshot": alternative["pool_snapshot"],
                        "unsupported_roster_ids": alternative["unsupported_roster_ids"],
                        "roster_statuses": alternative["roster_statuses"],
                        "context": alternative["context"],
                        "status": "research_comparison_only", "play_enabled": False,
                        "limitation": "Injury redistribution and replacement roles remain unvalidated.",
                    }
            if chance is not None:
                row["usage"] = {"games": f["games"], "carries": f["carries"],
                    "rush_td": f["rush_td"], "rec_td": f["rec_td"],
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
            from spe.shadow.atd_desk_takes import desk_candidate_blockers
            selection_blocks, valid_until = desk_candidate_blockers(row, now=now)
            row["desk_analysis"]["blockers"] = list(dict.fromkeys([*blockers, *selection_blocks]))
            row["desk_analysis"]["eligible"] = not row["desk_analysis"]["blockers"]
            row["desk_analysis"]["valid_until"] = valid_until
            if chance is not None and row.get("book_price") and abs(row["book_price"]) >= 100:
                from spe.market.odds import american_to_implied
                implied = american_to_implied(int(row["book_price"]))
                reasons.append(f"Research estimate {chance:.0%}; posted price implies {implied:.0%}. This estimated gap is not a proven edge.")
    # One decision for the whole slate, before any UI filtering or sorting.
    from spe.shadow.atd_desk_takes import select_desk_takes
    selected = {r["outcome_id"] for r in select_desk_takes(atd, now=now)}
    for row in atd:
        row["desk_selected"] = row["outcome_id"] in selected


def _hybrid_game(db, row, history, model, aliases, cutoff):
    """Use the whole roster, never just the names with a sportsbook price."""
    from spe.db.models import Game, InjuryEvent
    from spe.identity.roster import eligible_players_for_game
    game = db.get(Game, row["game_id"])
    if game is None:
        return None
    roster = eligible_players_for_game(db, game, as_of=cutoff, apply_eligibility_audit=False)
    team = canonical_team(row["team_id"], aliases)
    opponent = canonical_team(row["opponent_id"], aliases)
    ids = {p.player_id for p in roster if canonical_team(p.team_id, aliases) == team
           and p.eligibility != "confirmed_ineligible"}
    if not ids:
        return None
    events = db.query(InjuryEvent).filter(InjuryEvent.game_id == game.game_id,
        InjuryEvent.player_id.in_(ids), InjuryEvent.reported_at <= cutoff,
        InjuryEvent.observed_at <= cutoff, InjuryEvent.effective_at <= cutoff,
        InjuryEvent.ingested_at <= cutoff,
        InjuryEvent.quality_status.notin_(["quarantined", "invalid"])
        ).order_by(InjuryEvent.reported_at, InjuryEvent.ingested_at).all()
    statuses = {e.player_id: e.designation for e in events}
    home = canonical_team(game.home_team_id, aliases) == team
    context = desk_hybrid.team_context(history, team, opponent, cutoff, home)
    if context is None:
        return None
    pool = desk_hybrid.player_pool(history, team, opponent, cutoff, home, roster_ids=ids, statuses=statuses)
    allocation, totals, residuals = desk_hybrid.allocate(model, context, pool)
    return {"players": allocation, "pool_player_ids": sorted(pool), "team_expected_tds": totals,
            "unallocated_expected_tds": residuals, "context": context,
            "roster_statuses": {p.player_id: {"roster": p.status,
                "injury": statuses.get(p.player_id, "unknown")} for p in roster if p.player_id in ids},
            "unsupported_roster_ids": sorted(ids - pool.keys()),
            "pool_snapshot": {pid: {"features": v["features"], **allocation[pid]} for pid, v in pool.items()}}
