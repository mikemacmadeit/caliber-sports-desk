"""Database-backed Desk research scoring and grounded explanations."""
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from spe.db.base import as_utc
from spe.db.models import Team, InjuryEvent
from spe.market.odds import ev_per_dollar
from spe.integrity.assignments import canonical_team_map
from spe.models.player_td.desk_opportunity import (
    VERSION, NFL_VERSION, OpportunityHistory, canonical_team, explain, load_model, predict, prepare_history,
)
from spe.models.player_td.desk_combined import (
    VERSION as COMBINED_VERSION, INACTIVE, AllocationHistory, combined_prediction, load_combined_model,
)


HISTORY_SQL = """
WITH ranked AS (
 SELECT g.game_id,s.player_id,coalesce(s.team_id,r.team_id) AS team_id,g.kickoff_utc,
 g.home_team_id,g.away_team_id,p.position_group AS position,s.provider,
 s.carries,s.targets,s.receptions,s.rush_td,s.rec_td,s.gl_carries,s.rz_targets,p.name AS player_name,
 s.rush_yards,s.rec_yards,
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


def load_history(db, league, cutoff, *, reconstructed=False):
    start = cutoff.replace(month=7, day=1, hour=0, minute=0, second=0, microsecond=0)
    if cutoff.month < 7:
        start = start.replace(year=start.year - 1)
    rows = [dict(r) for r in db.execute(text(HISTORY_SQL), {
        "league": league, "start": start, "cutoff": cutoff - timedelta(hours=6),
    }).mappings()]
    for row in rows:
        if not row.pop("charted"):
            row["gl_carries"] = row["rz_targets"] = None
    if league == "nfl":
        from spe.models.player_td.nflverse_context import load_context
        rows = load_context(db, rows, cutoff)
    if league == 'fbs':
        from spe.ingest.recent_td_boxes import overlay_history
        rows = overlay_history(rows, cutoff, reconstructed=reconstructed)
    return rows


def enrich_desk_rows(db, rows, now=None):
    """Score the research Desk from measured opportunities; official models stay untouched."""
    now = as_utc(now) or datetime.now(UTC)
    atd = [r for r in rows if r.get("market_id") == "anytime_td"]
    if not atd:
        return
    latest_availability = {}
    game_cutoffs = {r["game_id"]: min(now, as_utc(r.get("kickoff_utc")) or now) for r in atd}
    reports = db.query(InjuryEvent).filter(InjuryEvent.game_id.in_({r["game_id"] for r in atd}),
                                          InjuryEvent.reported_at <= now,
                                          InjuryEvent.observed_at <= now,
                                          InjuryEvent.quality_status.notin_(("invalid", "quarantined"))).order_by(InjuryEvent.reported_at).all()
    from spe.models.player_td.availability_status import resolved_reports
    for gid,cutoff in game_cutoffs.items():
        current_reports = [r for r in reports if r.game_id == gid and
            as_utc(r.reported_at) <= cutoff and as_utc(r.observed_at) <= cutoff]
        latest_availability.update({(gid,pid):r['status'] for pid,r in resolved_reports(current_reports).items()})
    aliases = canonical_team_map(db.query(Team).all())
    for league in {r["league_id"] for r in atd}:
        preferred_model = load_model(league)
        candidate_model = load_model(league, research_candidate=True) if league == "nfl" else None
        history = OpportunityHistory(prepare_history(load_history(db, league, now), aliases))
        from spe.models.player_td.desk_evidence import load_evidence, current_context, explanations
        history.evidence = load_evidence(league)
        combined_model = load_combined_model(league)
        rich_candidate = (load_combined_model(league, research_candidate=True, version="desk_combined_v5")
                          or load_combined_model(league, research_candidate=True))
        rich_allocations = {}
        allocator = AllocationHistory(history)
        allocations = {}
        market_contexts = {}
        from spe.models.player_td.scouting_report import capture_matchup, model_fields, build_report
        scouting_inputs = {}
        supplements = {}
        for row in [r for r in atd if r["league_id"] == league]:
            model = preferred_model
            kick = as_utc(row.get("kickoff_utc"))
            team = canonical_team(row.get("team_id"), aliases)
            opp = canonical_team(row.get("opponent_id"), aliases)
            cutoff = min(now, kick) if kick else now
            from spe.models.player_td.desk_market import context_for_game, market_features, market_pick_eligible
            market_key = (row["game_id"], cutoff)
            if market_key not in market_contexts:
                market_contexts[market_key] = context_for_game(db, row["game_id"], cutoff)
            market = market_contexts[market_key]
            home = row.get("team") == row.get("home_abbr")
            row["market_context"] = market
            row["team_implied_points"] = market.get("home_implied_points" if home else "away_implied_points")
            f = history.features(row.get("player_id"), team, opp, row.get("position"), cutoff,
                                 row.get("team") == row.get("home_abbr"))
            if f:
                f.update(market_features(market, home, (combined_model or {}).get("market_total_trained", True)))
            reasons, risks = explain(f, league) if f else ([], ["Fewer than two usable prior games for the player, team, or opponent."])
            if model and model.get("version") == NFL_VERSION and (not f or not f.get("snap_known") or not f.get("pbp_known") or not f.get("free_data_current")):
                model = load_model(league, prefer_enriched=False)
                risks.append("Detailed NFL inputs are incomplete; using the prior research model.")
            version = (model or {}).get("version", VERSION)
            supplement_key = (team, cutoff)
            if supplement_key not in supplements:
                from spe.models.player_td.nflverse_context import supplemental_context
                ids = [r['player_id'] for r in atd if canonical_team(r.get('team_id'), aliases) == team]
                supplements[supplement_key] = supplemental_context(db, ids, cutoff, team_id=team) if league == 'nfl' else {}
            current = current_context(db, row['game_id'], row['player_id'], cutoff, team_id=team,
                nfl_supplement=supplements[supplement_key].get(row['player_id'], {}))
            supplement = current.get('nflverse_context') or {}
            receiving = supplement.get("receiving", {}).get("metrics", {})
            rushing = supplement.get("rushing", {}).get("metrics", {})
            depth = supplement.get("depth", {})
            if depth.get("rank") is not None:
                reasons.append(f"Latest available depth chart: listed at {depth['position']} #{depth['rank']}; scoring opportunity is assessed from actual workload.")
            if receiving.get("avg_separation") is not None:
                reasons.append(f"Latest available Next Gen Stats: {receiving['avg_separation']:.1f} yards of average separation at the catch/incompletion point.")
            if rushing.get("rush_yards_over_expected_per_att") is not None:
                reasons.append(f"Latest available Next Gen Stats: {rushing['rush_yards_over_expected_per_att']:+.1f} rushing yards over expected per attempt.")
            status = str(latest_availability.get((row["game_id"], row["player_id"])) or (row.get("forecast") or {}).get("availability_status") or "unknown").lower()
            row.setdefault("forecast", {})["availability_status"] = status
            blockers = []
            if combined_model is None:
                blockers.append("combined_matchup_model_unavailable")
            if not market_pick_eligible(row):
                blockers.append("market_scoring_outlook_unverified_or_below_21_points")
            if model is None:
                blockers.append("opportunity_model_unavailable")
            if f is None:
                blockers.append("insufficient_current_role_history")
            elif f["carries"] + f["receptions"] < 1:
                blockers.append("minimal_recent_workload")
            if f and (cutoff - as_utc(f["last_played_at"])).days > 21:
                blockers.append("role_history_outdated")
            if status in INACTIVE | {"doubtful", "questionable", "uncertain"}:
                blockers.append("availability_requires_review")
            if status not in {"active", "healthy", "probable", "available"}:
                risks.append("Availability is not confirmed; check the final inactive report.")
            chance = predict(model, f) if model and f else None
            combination = None
            if combined_model and f and cutoff >= as_utc(combined_model.get("validation_end", "1900-01-01")):
                key = (row["game_id"], team, cutoff, row.get("team") == row.get("home_abbr"))
                if key not in allocations:
                    unavailable = {pid for (gid, pid), status_value in latest_availability.items()
                                   if gid == row["game_id"] and str(status_value).lower() in INACTIVE}
                    unavailable.update(r["player_id"] for r in atd if r["game_id"] == row["game_id"]
                                       and (r["game_id"], r["player_id"]) not in latest_availability
                                       and str((r.get("forecast") or {}).get("availability_status", "")).lower() in INACTIVE)
                    allocations[key] = allocator.allocate(combined_model, team, opp, cutoff, key[-1], unavailable, market=market)
                combination = combined_prediction(combined_model, f, allocations[key], row["player_id"])
                if combination:
                    combination["previous_desk_probability"] = chance
                    chance = combination["probability"]
                    model, version = combined_model, combined_model["version"]
                    blockers = [b for b in blockers if b != "opportunity_model_unavailable"]
                    team_tds = sum(combination["team_expected_tds"].values())
                    reasons.append(f"Team forecast: {team_tds:.2f} offensive touchdowns, allocated across {combination['pool_size']} recent teammates, including unpriced players.")
                    reasons.append(f"Player estimate {combination['player_probability']:.0%}; team allocation estimate {combination['allocation_probability']:.0%}; calibrated combined chance {chance:.0%}.")
                    risks.append("Team allocation uses recent roles; new replacements and unreported roster changes remain uncertain.")
            candidate_ready = f and f.get("snap_known") and f.get("pbp_known") and f.get("free_data_current")
            candidate_chance = predict(candidate_model, f) if candidate_ready and candidate_model and candidate_model.get("version") == NFL_VERSION else None
            if status in INACTIVE:
                chance = 0.0 if chance is not None else None
                candidate_chance = 0.0 if candidate_chance is not None else None
            evidence_reasons, evidence_risks = explanations(f or {})
            reasons.extend(evidence_reasons)
            risks.extend(evidence_risks)
            research_comparison = None
            if f and rich_candidate and cutoff >= as_utc(rich_candidate.get("validation_end", "1900-01-01")):
                rich_key = (row["game_id"], team, cutoff, home)
                if rich_key not in rich_allocations:
                    unavailable_ids = {pid for (gid,pid),value in latest_availability.items()
                                       if gid == row["game_id"] and str(value).lower() in INACTIVE}
                    rich_allocations[rich_key] = allocator.allocate(rich_candidate, team, opp, cutoff, home, unavailable_ids, market=market)
                research_comparison = combined_prediction(rich_candidate, f, rich_allocations[rich_key], row["player_id"])
            research_key = (team, opp, cutoff)
            if research_key not in scouting_inputs:
                scouting_inputs[research_key] = capture_matchup(history, team, opp, cutoff)
            row["desk_analysis"] = {
                "scouting_inputs": scouting_inputs[research_key],
                "fitted_feature_names": model_fields(combined_model) if combined_model else None,
                "current_context": current,
                "rich_research_comparison": research_comparison,
                "rich_research_model": (rich_candidate or {}).get("version"),
                "rich_research_status": "active" if combined_model and rich_candidate and combined_model.get("artifact_sha256") == rich_candidate.get("artifact_sha256") else "shadow_only_validation_gate_not_passed",
                "matchup_features_active": bool(combined_model and combined_model.get("rich_features") and combination),
                "free_opportunities_active": bool(combined_model and (combined_model.get("free_opportunities") or combined_model.get('scorer_model')) and combination),
                "free_opportunities_status": "active" if combined_model and (combined_model.get("free_opportunities") or combined_model.get('scorer_model')) else "context_and_shadow_only",
                "situational_features_active": bool(combined_model and (combined_model.get("situational_features") or combined_model.get('scorer_model')) and combination),
                "market_total_active": bool(combined_model and (combined_model.get('market_total_trained') or combined_model.get('scorer_model',{}).get('uses_market_totals'))),
                "version": version, "as_of": now.isoformat(), "features": f,
                "combination": combination,
                "combination_status": "active" if combination else ("insufficient_allocation_history" if combined_model else "artifact_not_enabled_or_unavailable"),
                "nflverse_context": supplement,
                "sources": (["SportsDataverse ESPN play-by-play"] if f and (f.get("rush_pbp_known") or f.get("target_pbp_known")) else []) +
                           (["nflverse play-by-play"] if f and f.get("pbp_games") else []) +
                           (["PFR snap counts via nflverse"] if f and f.get("snap_games") else []) +
                           (["NFL Next Gen Stats via nflverse"] if any(k in supplement for k in ("receiving", "rushing", "passing")) else []) +
                           (["Depth charts via nflverse"] if depth else []),
                "nflverse_research_probability": candidate_chance,
                "nflverse_research_model": (candidate_model or {}).get("artifact_sha256") if candidate_chance is not None else None,
                "nflverse_research_status": "shadow_only" if candidate_chance is not None else "insufficient_inputs",
                "candidate_probability": chance,
                "candidate_status": "experimental_research", "reasons": reasons, "risks": risks,
                "eligible": not blockers, "blockers": blockers,
                "selection_probability": chance,
            }
            if chance is not None:
                usage = dict(row.get("usage") or {})
                usage.update(games=f["games"], carries=f["carries"], receptions=f["receptions"])
                if league == "nfl":
                    usage["targets"] = f["targets"]
                if f["charting_known"]:
                    usage["gl_carries"] = f["gl_carries"]
                    usage["rz_targets"] = f["rz_targets"]
                row["usage"] = usage
                prior = dict(row.get("forecast") or {})
                row["forecast"] = {**prior, "base_probability": prior.get("p_estimate"),
                    "p_estimate": chance, "confidence_interval": (0.0, 1.0),
                    "uncertainty_status": "unvalidated", "model_version": version + "-" + league + "-" + model.get("artifact_sha256", "test")[:10],
                    "prediction_as_of": now.isoformat(), "data_cutoff": cutoff.isoformat(),
                    "tonight": None, "matchup": None, "research_only": True,
                    "validated_play": False, "play_enabled": False, "data_tier": "research",
                    "recommendation_status": "no_bet", "ev_after_haircut": None,
                    "ev": ev_per_dollar(chance, int(row["book_price"])) if row.get("book_price") and abs(row["book_price"]) >= 100 else None,
                    "reason_codes": list(dict.fromkeys([*prior.get("reason_codes", []), "DESK_EXPERIMENTAL", "PLAY_DISABLED"]))}
                row["data_tier"] = "research"
            if kick and kick > now:
                from spe.models.player_td.selection_safety import assess
                row['selection_safety']=assess(row,cutoff,bool(combined_model and combined_model.get('live_enabled') and combination))
    # One decision for the whole slate, before any UI filtering or sorting.
    from spe.models.player_td.scorer_eligibility import exclusions
    for row in atd:
        excluded = exclusions(row)
        if excluded and row.get('desk_analysis'):
            row['desk_analysis']['eligible'] = False
            row['desk_analysis']['blockers'] += excluded
    from spe.shadow.atd_desk_takes import select_desk_takes
    selected = {r["outcome_id"] for r in select_desk_takes(atd)}
    for row in atd:
        row["desk_selected"] = row["outcome_id"] in selected
        if row.get('desk_analysis'):
            from spe.models.player_td.scouting_report import build_report
            row['desk_analysis']['scouting_report'] = build_report(row, atd)
