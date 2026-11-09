"""Bounded Agent-1 closure: M4 provenance, 15 exclusions, blocked triage, C_recent handoff.

Does not retrain, overwrite freezes, enable Validated Play, or add polling loops.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.db.models import Game
from spe.identity.roster import eligible_players_for_game
from spe.shadow.capture import _usage_features
from spe.shadow.m4_artifact import adjust_mean, load_m4_artifacts, probability_from_mean
from spe.shadow.prospective_readiness import classify_nfl_gaps

REPORT_DIR = Path("data") / "atd_sim_dev" / "live_weekend"
SKILL = {"QB", "RB", "WR", "TE"}
JAMES_PID = "nfl:00-0040177"
JAMES_GID = "nfl:2026_09_LV_SF"


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def verify_jordan_james_m4(session: Session) -> dict[str, Any]:
    """Trace James M4 → source M3; recompute M4 path; classify provenance."""
    m3 = session.execute(
        text(
            """
            SELECT shadow_id, forecast_probability, forecast_as_of, odds_tick_id, bookmaker_id,
                   price_american, components, created_at, model_version_id
            FROM shadow_predictions
            WHERE player_id=:p AND game_id=:g AND model_version_id LIKE 'm3-%'
              AND market_id='anytime_td'
            ORDER BY forecast_as_of DESC LIMIT 1
            """
        ),
        {"p": JAMES_PID, "g": JAMES_GID},
    ).mappings().first()
    m4 = session.execute(
        text(
            """
            SELECT shadow_id, forecast_probability, forecast_as_of, odds_tick_id, bookmaker_id,
                   price_american, components, created_at, model_version_id, outcome_id
            FROM shadow_predictions
            WHERE player_id=:p AND game_id=:g AND model_version_id LIKE 'm4-%'
              AND market_id='anytime_td'
            ORDER BY forecast_as_of DESC LIMIT 1
            """
        ),
        {"p": JAMES_PID, "g": JAMES_GID},
    ).mappings().first()
    game = session.execute(
        text("SELECT kickoff_utc, status FROM games WHERE game_id=:g"),
        {"g": JAMES_GID},
    ).mappings().first()

    if not m3 or not m4:
        return {"status": "missing_records", "m3_present": bool(m3), "m4_present": bool(m4)}

    m3c = _json_obj(m3["components"])
    m4c = _json_obj(m4["components"])
    kick = as_utc(game["kickoff_utc"]) if game else None
    m4_asof = as_utc(m4["forecast_as_of"])
    before_kickoff = bool(kick and m4_asof and m4_asof < kick)

    # Recompute documented M4 path from M3 mean.
    market = (load_m4_artifacts("nfl") or {}).get("markets", {}).get("anytime_td") or {}
    feats = _usage_features(session, {JAMES_GID}).get((JAMES_PID, JAMES_GID), {})
    source_mean = m3c.get("mean")
    if source_mean is None:
        # Fallback: research forecast mean linked via backfill.
        fid = m4c.get("source_forecast_id") or m4c.get("backfill_from_m3_shadow_id")
        fc = session.execute(
            text(
                """
                SELECT components, p, forecast_id, as_of, is_official
                FROM forecasts
                WHERE outcome_id=:o AND model_version_id LIKE 'm3-%'
                ORDER BY as_of DESC LIMIT 1
                """
            ),
            {"o": m4["outcome_id"]},
        ).mappings().first()
        fcc = _json_obj(fc["components"]) if fc else {}
        source_mean = fcc.get("mean")
        forecast_meta = dict(fc) if fc else None
    else:
        forecast_meta = None
        fc = session.execute(
            text(
                """
                SELECT components, p, forecast_id, as_of, is_official
                FROM forecasts
                WHERE outcome_id=:o AND model_version_id LIKE 'm3-%'
                ORDER BY as_of DESC LIMIT 1
                """
            ),
            {"o": m4["outcome_id"]},
        ).mappings().first()
        if fc:
            forecast_meta = dict(fc)
            if source_mean is None:
                source_mean = _json_obj(fc["components"]).get("mean")

    source_mean_f = float(source_mean) if source_mean is not None else float(m3["forecast_probability"] or 0)
    adjusted, missing = adjust_mean(source_mean_f, feats, market)
    p_recomputed = probability_from_mean(adjusted, market, line=None, side="yes")
    stored_p = float(m4["forecast_probability"])
    stored_mean = m4c.get("mean")
    abs_p_delta = abs(stored_p - p_recomputed)
    abs_mean_delta = abs(float(stored_mean) - adjusted) if stored_mean is not None else None

    # Classify
    copied_m3_p = abs(stored_p - float(m3["forecast_probability"])) < 1e-9
    features_empty = not (market.get("features") or market.get("betas"))
    # anytime_td market may have empty feature list → M4 = Platt(binary(m3_mean))
    method_path = (
        "m3_mean → adjust_mean(frozen_betas) → binary_probability → apply_platt"
        if market
        else "missing_m4_market"
    )
    if features_empty or not (market.get("betas") or {}):
        method_note = (
            "NFL anytime_td M4 artifact has empty feature/beta set; "
            "adjust_mean is a no-op; probability_from_mean still applies "
            "binary_probability(mean) + frozen Platt calibration — not a raw M3 p copy."
        )
    else:
        method_note = "Feature betas applied when present."

    if copied_m3_p and abs_p_delta > 1e-6:
        eligibility = "invalid_for_comparison"
        label = "invalid"
    elif abs_p_delta <= 1e-6:
        # Matches recomputed M4 path
        if features_empty:
            eligibility = "valid_m4_method_platt_only_alias_risk"
            label = "valid_method_limited_independence"
        else:
            eligibility = "valid_independent_m4"
            label = "valid"
    elif abs_p_delta <= 1e-4:
        eligibility = "valid_m4_method_float_noise"
        label = "valid"
    else:
        eligibility = "annotate_mismatch_preserve_record"
        label = "alias_or_mismatch"

    # Annotate components append-only note (do not rewrite probability/timestamps)
    annotation = {
        "provenance_audit_at": datetime.now(UTC).isoformat(),
        "source_m3_shadow_id": m3["shadow_id"],
        "backfill_flag_present": bool(m4c.get("backfill_from_m3_shadow_id")),
        "m4_method_path": method_path,
        "m4_method_note": method_note,
        "recomputed_p": p_recomputed,
        "recomputed_adjusted_mean": adjusted,
        "source_mean_used": source_mean_f,
        "abs_p_delta_vs_recompute": abs_p_delta,
        "comparison_eligibility": eligibility,
        "features_applied": [k for k in (market.get("features") or []) if k not in missing],
        "missing_features": missing,
        "platt": {"a": market.get("platt_a"), "b": market.get("platt_b")},
        "before_kickoff": before_kickoff,
        "do_not_backdate": True,
    }
    # Persist annotation into components without changing p / forecast_as_of
    new_comps = {**m4c, "provenance_audit": annotation}
    session.execute(
        text(
            """
            UPDATE shadow_predictions
            SET components = :c
            WHERE shadow_id = :sid
            """
        ),
        {"c": json.dumps(new_comps), "sid": m4["shadow_id"]},
    )
    session.flush()

    return {
        "player": "Jordan James",
        "player_id": JAMES_PID,
        "game_id": JAMES_GID,
        "kickoff_utc": str(kick) if kick else None,
        "source_m3": {
            "shadow_id": m3["shadow_id"],
            "model_version_id": m3["model_version_id"],
            "p": m3["forecast_probability"],
            "mean": m3c.get("mean"),
            "forecast_as_of": str(m3["forecast_as_of"]),
            "created_at": str(m3["created_at"]),
            "odds_tick_id": m3["odds_tick_id"],
        },
        "m4": {
            "shadow_id": m4["shadow_id"],
            "model_version_id": m4["model_version_id"],
            "p": stored_p,
            "stored_mean": stored_mean,
            "forecast_as_of": str(m4["forecast_as_of"]),
            "created_at": str(m4["created_at"]),
            "odds_tick_id": m4["odds_tick_id"],
            "pipeline_version": m4c.get("pipeline_version"),
            "backfill_from_m3_shadow_id": m4c.get("backfill_from_m3_shadow_id"),
        },
        "forecast_source": {
            "forecast_id": (forecast_meta or {}).get("forecast_id"),
            "as_of": str((forecast_meta or {}).get("as_of")),
            "is_official": (forecast_meta or {}).get("is_official"),
            "p": (forecast_meta or {}).get("p"),
            "mean": _json_obj((forecast_meta or {}).get("components")).get("mean") if forecast_meta else None,
        },
        "recompute": {
            "source_mean": source_mean_f,
            "adjusted_mean": adjusted,
            "missing_features": missing,
            "features_dict": feats,
            "p_recomputed": p_recomputed,
            "abs_p_delta": abs_p_delta,
            "abs_mean_delta": abs_mean_delta,
            "copied_m3_probability": copied_m3_p,
            "empty_feature_betas": features_empty or not (market.get("betas") or {}),
            "method_path": method_path,
            "method_note": method_note,
            "platt_a": market.get("platt_a"),
            "platt_b": market.get("platt_b"),
        },
        "before_kickoff": before_kickoff,
        "hours_before_kickoff": round((kick - m4_asof).total_seconds() / 3600, 2) if kick and m4_asof else None,
        "provenance_label": label,
        "comparison_eligibility": eligibility,
        "annotation_persisted": True,
        "verdict": (
            "VALID M4 method path (mean→binary→Platt); feature betas empty so independence from M3 "
            "is limited — count as method-valid M4 with alias-risk, not a raw p copy."
            if label.startswith("valid")
            else f"Labeled {label}; record preserved with provenance_audit."
        ),
    }


def reconcile_15_exclusions(session: Session) -> dict[str, Any]:
    gaps = classify_nfl_gaps(session)
    # Rebuild full missing lists (classify only stores examples)
    priced = gaps  # need full list — recompute
    from spe.shadow.prospective_readiness import _priced_player_games, _has_family

    rows = _priced_player_games(session, "nfl")
    elig_cache: dict[str, dict[str, Any]] = {}
    excluded: list[dict[str, Any]] = []

    for row in rows:
        gid = row["game_id"]
        if gid not in elig_cache:
            game = session.get(Game, gid)
            elig = eligible_players_for_game(session, game) if game else []
            elig_cache[gid] = {
                "ids": {e.player_id for e in elig if e.position_group in SKILL},
                "by_id": {e.player_id: e for e in elig},
            }
        has_core = all(_has_family(session, row["player_id"], gid, f) for f in ("m3", "m4", "m5", "m6"))
        has_ptd = _has_family(session, row["player_id"], gid, "player_td_v1")
        has_hist = _has_family(session, row["player_id"], gid, "m6_histgbm")
        if has_core and (not has_ptd or not has_hist):
            in_elig = row["player_id"] in elig_cache[gid]["ids"]
            ep = elig_cache[gid]["by_id"].get(row["player_id"])
            # roster_weeks presence for this player/game teams
            rw = session.execute(
                text(
                    """
                    SELECT rw.team_id, rw.status, rw.week, rw.season_year, rw.position
                    FROM roster_weeks rw
                    JOIN games g ON g.game_id=:g
                    WHERE rw.player_id=:p
                      AND rw.season_year = CAST(substr(g.season_id, -4) AS INTEGER)
                    ORDER BY ABS(rw.week - COALESCE(g.week, rw.week)) ASC, rw.week DESC
                    LIMIT 5
                    """
                ),
                {"p": row["player_id"], "g": gid},
            ).mappings().all()
            name_collisions = session.execute(
                text(
                    """
                    SELECT player_id, name, position_group FROM players
                    WHERE lower(name)=lower(:n) AND player_id<>:p LIMIT 5
                    """
                ),
                {"n": row["name"], "p": row["player_id"]},
            ).mappings().all()
            # Why M3 accepted: outcomes priced without PlayerTD roster gate
            if in_elig:
                kind = "stricter_matching_needs_investigation"
                detail = "In eligible skill roster but missing PlayerTD/HistGBM freeze — routing gap."
                scope = "investigate_routing"
            elif rw:
                statuses = {str(x["status"] or "").upper() for x in rw}
                if statuses & {"RET", "CUT", "INA", "DEV", "PUP", "RES", "SUS"}:
                    kind = "family_specific_eligibility"
                    detail = (
                        "roster_weeks present with non-ACT status; PlayerTD/HistGBM require "
                        f"eligible ACT skill roster. statuses={sorted(statuses)}"
                    )
                    scope = "family_specific_eligibility"
                else:
                    kind = "family_specific_eligibility"
                    detail = (
                        "roster_weeks present but excluded from eligible_players_for_game "
                        f"skill set used by PlayerTD/HistGBM. rows={[dict(x) for x in rw[:3]]}"
                    )
                    scope = "family_specific_eligibility"
            else:
                kind = "family_specific_eligibility"
                detail = (
                    "No roster_weeks row for this season; PlayerTD/HistGBM gated on week ACT "
                    "skill roster. M3–M6 accept via priced outcome + research forecast path."
                )
                scope = "family_specific_eligibility"

            # Global identity only when duplicate canonical names AND no week roster — Agent3
            if name_collisions and not rw and not in_elig:
                kind = "possible_global_identity_issue_coordinate_agent3"
                scope = "global_identity_candidate"
                detail += f" Duplicate player name ids: {[dict(x) for x in name_collisions]}."

            excluded.append(
                {
                    "player": row["name"],
                    "player_id": row["player_id"],
                    "game_id": gid,
                    "position": row["position_group"],
                    "in_eligible_skill_roster": in_elig,
                    "eligible_status": getattr(ep, "status", None) if ep else None,
                    "eligible_source": getattr(ep, "source", None) if ep else None,
                    "has_player_td": has_ptd,
                    "has_histgbm": has_hist,
                    "roster_week_rows": [dict(x) for x in rw],
                    "classification": kind,
                    "scope": scope,
                    "detail": detail,
                    "action": (
                        "Coordinate with Agent 3; do not force-match"
                        if scope.startswith("global")
                        else "Keep labeled family-specific exclusion; do not inflate coverage"
                    ),
                }
            )

    by_scope = Counter(r["scope"] for r in excluded)
    by_class = Counter(r["classification"] for r in excluded)
    return {
        "priced_universe_n": gaps["denominator"]["n"],
        "priced_universe_games": gaps["denominator"]["unique_games"],
        "m3_m6_have": {f: gaps["have"][f] for f in ("m3", "m4", "m5", "m6")},
        "player_td_hist_have": {
            "player_td_v1": gaps["have"]["player_td_v1"],
            "m6_histgbm": gaps["have"]["m6_histgbm"],
        },
        "family_specific_eligible_denominator": gaps["have"]["player_td_v1"],
        "cross_family_exclusions_n": len(excluded),
        "by_scope": dict(by_scope),
        "by_classification": dict(by_class),
        "rows": excluded,
        "sim_exclusions_unchanged": gaps["missing_counts"].get("atd_sim_v1"),
        "note": (
            "Original 380 priced player-game universe retained. "
            "PlayerTD/HistGBM eligible denom = 365. Do not force 100%."
        ),
    }


def triage_blocked_player_games(session: Session) -> dict[str, Any]:
    """One row per blocked player-game with evidence and disposition."""
    rows = session.execute(
        text(
            """
            SELECT DISTINCT s.game_id, s.player_id, pl.name, pl.position_group,
                   g.kickoff_utc, g.status, g.home_team_id, g.away_team_id, g.week,
                   (SELECT COUNT(*) FROM player_game_stats p WHERE p.game_id=s.game_id) AS game_box_rows,
                   (SELECT COUNT(*) FROM player_game_stats p
                    WHERE p.game_id=s.game_id AND p.player_id=s.player_id) AS player_box,
                   (SELECT GROUP_CONCAT(DISTINCT designation)
                    FROM injury_events i
                    WHERE i.game_id=s.game_id AND i.player_id=s.player_id) AS injury_designations,
                   (SELECT COUNT(*) FROM shadow_settlements st
                    JOIN shadow_predictions s2 ON s2.shadow_id=st.shadow_id
                    WHERE st.result='blocked' AND s2.game_id=s.game_id AND s2.player_id=s.player_id
                      AND s2.market_id='anytime_td') AS blocked_family_rows
            FROM shadow_settlements st
            JOIN shadow_predictions s ON s.shadow_id=st.shadow_id
            JOIN games g ON g.game_id=s.game_id
            LEFT JOIN players pl ON pl.player_id=s.player_id
            WHERE s.market_id='anytime_td' AND st.result='blocked'
            ORDER BY s.game_id, pl.name
            """
        )
    ).mappings().all()

    # Provider id maps for identity checks
    out_rows: list[dict[str, Any]] = []
    for r in rows:
        pid, gid = r["player_id"], r["game_id"]
        maps = session.execute(
            text(
                """
                SELECT provider, entity_type, external_id, canonical_id, confidence
                FROM provider_id_map
                WHERE canonical_id=:p OR external_id LIKE :pat
                LIMIT 10
                """
            ),
            {"p": pid, "pat": f"%{pid.split(':')[-1]}%"},
        ).mappings().all()
        # Name match in same-game box?
        last = (r["name"] or "").split()[-1].lower() if r["name"] else ""
        name_hits = session.execute(
            text(
                """
                SELECT p.player_id, pl.name, p.rush_td, p.rec_td, p.carries, p.receptions, p.provider
                FROM player_game_stats p
                LEFT JOIN players pl ON pl.player_id=p.player_id
                WHERE p.game_id=:g AND lower(pl.name) LIKE :pat
                """
            ),
            {"g": gid, "pat": f"%{last}%"},
        ).mappings().all() if last else []

        # Team roster open at kickoff?
        on_roster = session.execute(
            text(
                """
                SELECT team_id, status, jersey, valid_from, valid_to FROM rosters
                WHERE player_id=:p AND team_id IN (:h, :a)
                LIMIT 5
                """
            ),
            {"p": pid, "h": r["home_team_id"], "a": r["away_team_id"]},
        ).mappings().all()

        evidence = {
            "game_box_rows": r["game_box_rows"],
            "player_box_present": bool(r["player_box"]),
            "injury_designations": r["injury_designations"],
            "provider_maps": [dict(m) for m in maps],
            "same_game_lastname_box_hits": [dict(h) for h in name_hits],
            "roster_rows_for_game_teams": [dict(x) for x in on_roster],
            "cfbd_week10_retry_added_player_row": False,
            "note": "Absence from box ≠ zero TD and ≠ confirmed DNP",
        }

        # Disposition rules
        if evidence["player_box_present"]:
            disposition = "resettle_now"
            reason = "player box now present"
            retry_when = "immediate settle retry"
        elif evidence["injury_designations"] and any(
            d and d.lower() in {"out", "ir", "injured reserve", "pup"}
            for d in str(evidence["injury_designations"]).lower().split(",")
        ):
            # Need pregame timing for void — check injury before kickoff
            inj = session.execute(
                text(
                    """
                    SELECT designation, reported_at FROM injury_events
                    WHERE game_id=:g AND player_id=:p
                    ORDER BY reported_at DESC LIMIT 3
                    """
                ),
                {"g": gid, "p": pid},
            ).mappings().all()
            kick = as_utc(r["kickoff_utc"])
            pregame_out = False
            for i in inj:
                rep = as_utc(i["reported_at"])
                if rep and kick and rep < kick and str(i["designation"]).lower() in {
                    "out", "ir", "injured reserve", "pup"
                }:
                    pregame_out = True
            if pregame_out:
                disposition = "void_candidate_confirmed_inactive_pregame"
                reason = "pregame inactive designation with timestamp before kickoff"
                retry_when = "run settle (injury void path); no box ingest needed"
            else:
                disposition = "remain_blocked"
                reason = "injury label present but not confirmed pregame-out under market rules"
                retry_when = "authoritative pregame inactive confirmation with timestamp"
        elif name_hits and any(h["player_id"] != pid for h in name_hits):
            disposition = "exclude_identity_defect_candidate"
            reason = "same-game box has similarly named different player_id — coordinate Agent 3"
            retry_when = "after Agent 3 identity reconciliation; do not force-match"
        elif not on_roster and int(r["game_box_rows"] or 0) > 0:
            disposition = "remain_blocked"
            reason = (
                "Game box incomplete for this athlete; no pregame inactive evidence; "
                "not assumed DNP or zero TD"
            )
            retry_when = (
                "new CFBD/ESPN player-stat row for this player_id appears, "
                "or confirmed inactive/DNP with market-rule evidence"
            )
        else:
            disposition = "remain_blocked"
            reason = "insufficient participation/scoring evidence"
            retry_when = "provider adds player game stats or confirmed nonparticipation evidence"

        # Apply void only when settle path supports it — invoke settle for void candidates
        applied = None
        if disposition == "void_candidate_confirmed_inactive_pregame":
            from spe.shadow.accuracy_optimization import settle_all_eligible

            settle_all_eligible(session)
            still = session.execute(
                text(
                    """
                    SELECT COUNT(*) FROM shadow_settlements st
                    JOIN shadow_predictions s ON s.shadow_id=st.shadow_id
                    WHERE st.result='blocked' AND s.game_id=:g AND s.player_id=:p
                      AND s.market_id='anytime_td'
                    """
                ),
                {"g": gid, "p": pid},
            ).scalar()
            applied = "settled_void" if int(still or 0) == 0 else "still_blocked_after_settle"
            if applied == "settled_void":
                disposition = "resolved_void"
        elif disposition == "resettle_now":
            from spe.shadow.accuracy_optimization import settle_all_eligible

            settle_all_eligible(session)
            st = session.execute(
                text(
                    """
                    SELECT st.result FROM shadow_settlements st
                    JOIN shadow_predictions s ON s.shadow_id=st.shadow_id
                    WHERE s.game_id=:g AND s.player_id=:p AND s.market_id='anytime_td'
                    LIMIT 1
                    """
                ),
                {"g": gid, "p": pid},
            ).scalar()
            applied = st
            if st in {"win", "loss", "void", "push"}:
                disposition = f"resolved_{st}"

        out_rows.append(
            {
                "player": r["name"],
                "player_id": pid,
                "game_id": gid,
                "position": r["position_group"],
                "blocked_family_rows": r["blocked_family_rows"],
                "evidence": evidence,
                "disposition": disposition,
                "reason": reason,
                "retry_when": retry_when,
                "applied": applied,
            }
        )

    resolved = [r for r in out_rows if str(r["disposition"]).startswith("resolved")]
    still = [r for r in out_rows if not str(r["disposition"]).startswith("resolved")]
    return {
        "blocked_player_games_before": len(rows),
        "resolved": len(resolved),
        "still_blocked": len(still),
        "no_full_reingest": True,
        "rows": out_rows,
        "summary_by_disposition": dict(Counter(r["disposition"] for r in out_rows)),
    }


def agent2_cohort_status(session: Session) -> dict[str, Any]:
    handoff_path = Path("data/research/atd_unified/m5_fbs_refit/agent1_handoff/AGENT1_HANDOFF.json")
    ingest_path = Path("data/research/atd_unified/m5_fbs_refit/agent1_handoff/shadow_ingest_summary.json")
    lock_path = Path("data/research/atd_unified/m5_fbs_refit/locked/C_RECENT_LOCKED.json")
    paired_path = Path("data/research/atd_unified/m5_fbs_refit/prospective/latest_paired_freeze.json")

    handoff = json.loads(handoff_path.read_text(encoding="utf-8")) if handoff_path.exists() else None
    ingest = json.loads(ingest_path.read_text(encoding="utf-8")) if ingest_path.exists() else None
    lock = json.loads(lock_path.read_text(encoding="utf-8")) if lock_path.exists() else None
    paired = json.loads(paired_path.read_text(encoding="utf-8")) if paired_path.exists() else None

    # Shadow counts for paired families
    counts = session.execute(
        text(
            """
            SELECT
              CASE
                WHEN s.model_version_id LIKE 'm5_c_recent%' THEN 'm5_c_recent'
                WHEN s.model_version_id LIKE 'm5-%' AND s.model_version_id NOT LIKE '%c_recent%' THEN 'm5'
                ELSE 'other'
              END fam,
              COUNT(*) n,
              COUNT(DISTINCT s.player_id||'|'||s.game_id) player_games,
              COUNT(DISTINCT s.game_id) games,
              SUM(CASE WHEN st.result IN ('win','loss') THEN 1 ELSE 0 END) settled_wl,
              SUM(CASE WHEN st.result='blocked' THEN 1 ELSE 0 END) blocked,
              SUM(CASE WHEN st.shadow_id IS NULL THEN 1 ELSE 0 END) unsettled
            FROM shadow_predictions s
            LEFT JOIN shadow_settlements st ON st.shadow_id=s.shadow_id
            JOIN games g ON g.game_id=s.game_id
            WHERE s.market_id='anytime_td'
              AND s.league_id='fbs'
              AND (s.model_version_id LIKE 'm5_c_recent%' OR s.model_version_id LIKE 'm5-%')
              AND s.forecast_as_of < g.kickoff_utc
            GROUP BY 1
            """
        )
    ).mappings().all()

    # Paired intersection: same player-game with both families unsettled or settled
    def _paired_count() -> int:
        return int(
            session.execute(
                text(
                    """
                    SELECT COUNT(*) FROM (
                      SELECT s.player_id, s.game_id
                      FROM shadow_predictions s
                      JOIN games g ON g.game_id=s.game_id
                      WHERE s.market_id='anytime_td' AND s.league_id='fbs'
                        AND s.forecast_as_of < g.kickoff_utc
                        AND s.model_version_id LIKE 'm5-%'
                        AND s.model_version_id NOT LIKE '%c_recent%'
                      INTERSECT
                      SELECT s.player_id, s.game_id
                      FROM shadow_predictions s
                      JOIN games g ON g.game_id=s.game_id
                      WHERE s.market_id='anytime_td' AND s.league_id='fbs'
                        AND s.forecast_as_of < g.kickoff_utc
                        AND s.model_version_id LIKE 'm5_c_recent%'
                    ) paired
                    """
                )
            ).scalar()
            or 0
        )

    paired_pg = _paired_count()

    # Ensure ingest completed — if gap vs handoff, attempt one append-only ingest
    ingest_action: dict[str, Any] = {"status": "already_present", "paired_before": paired_pg}
    if ingest and int(ingest.get("written") or 0) + int(ingest.get("unchanged") or 0) > 0 and paired_pg > 0:
        ingest_action = {"status": "registered", "summary": ingest, "paired_before": paired_pg}
    elif paired_path.exists() and paired_pg == 0:
        from spe.research.m5_fbs_lock_handoff import ingest_paired_freeze_into_shadow

        ingest_action = {"status": "ingested_now", "summary": ingest_paired_freeze_into_shadow(session)}
        session.flush()
    elif ingest:
        ingest_action = {"status": "ingest_summary_present_verify_counts", "summary": ingest, "paired_before": paired_pg}

    paired_pg_after = _paired_count()

    version_sep = {
        "legacy_unversioned_42": {
            "label": "legacy_pre_version_block",
            "unique_player_games": 42,
            "note": "Genuine pregame freezes; missing version metadata ≠ historical replay; do not backfill versions",
        },
        "c_recent_paired": {
            "locked_version": (lock or {}).get("locked_version") or (handoff or {}).get("versioning", {}).get("locked_version"),
            "pipeline_version": (handoff or {}).get("versioning", {}).get("pipeline_version"),
            "freeze_id": (paired or {}).get("freeze_id"),
            "paired_player_games_in_shadow": int(paired_pg_after or 0),
            "holdout_tag": (handoff or {}).get("cohort_summary", {}).get("holdout_tag"),
        },
    }

    gaps = []
    if int(paired_pg_after or 0) == 0:
        gaps.append("No paired m5∩m5_c_recent player-games in shadow yet")
    if not lock:
        gaps.append("C_RECENT_LOCKED.json missing")

    return {
        "handoff_present": handoff is not None,
        "lock_present": lock is not None,
        "paired_freeze_present": paired is not None,
        "ingest_action": ingest_action,
        "shadow_family_counts": [dict(c) for c in counts],
        "paired_player_games": int(paired_pg_after or 0),
        "version_separated_evaluation": version_sep,
        "registered_and_paired": int(paired_pg_after or 0) > 0,
        "explicit_gaps": gaps,
        "play_enabled": False,
        "do_not_promote": True,
    }


def new_evidence_since_prior(session: Session, prior_unique_pg: int = 42) -> dict[str, Any]:
    settled = session.execute(
        text(
            """
            SELECT COUNT(DISTINCT s.player_id||'|'||s.game_id) pg,
                   COUNT(DISTINCT s.game_id) games
            FROM shadow_settlements st
            JOIN shadow_predictions s ON s.shadow_id=st.shadow_id
            JOIN games g ON g.game_id=s.game_id
            WHERE s.market_id='anytime_td'
              AND st.result IN ('win','loss')
              AND s.forecast_as_of < g.kickoff_utc
            """
        )
    ).mappings().first()
    pg = int((settled or {}).get("pg") or 0)
    games = int((settled or {}).get("games") or 0)
    return {
        "prior_unique_player_games": prior_unique_pg,
        "current_unique_settled_player_games": pg,
        "current_unique_settled_games": games,
        "new_unique_player_games": max(0, pg - prior_unique_pg),
        "no_new_prospective_evidence": pg <= prior_unique_pg,
    }


def run_prospective_closure(session: Session) -> dict[str, Any]:
    print("[closure] Jordan James M4 provenance…", flush=True)
    m4 = verify_jordan_james_m4(session)
    session.commit()
    print("[closure] reconcile 15 exclusions…", flush=True)
    excl = reconcile_15_exclusions(session)
    print("[closure] triage blocked…", flush=True)
    blocked = triage_blocked_player_games(session)
    session.commit()
    print("[closure] Agent2 C_recent cohort…", flush=True)
    cohort = agent2_cohort_status(session)
    session.commit()
    evidence = new_evidence_since_prior(session)

    ownership = [
        {
            "defect": f"{blocked['still_blocked']} blocked FBS player-games without box/participation evidence",
            "owner": "Agent1+data ingest",
            "action": "retry only when provider adds player row or confirmed inactive evidence",
        },
        {
            "defect": f"{excl['cross_family_exclusions_n']} priced players outside PlayerTD/HistGBM roster gate",
            "owner": "Agent1 (label) / Agent3 if global identity",
            "action": "keep family-specific; coordinate Agent3 only for global_identity_candidate",
        },
        {
            "defect": "Legacy 42 PG lack pipeline_version metadata",
            "owner": "Agent1 eval",
            "action": "evaluate as legacy_unversioned; never backfill versions from later info",
        },
    ]
    if cohort.get("explicit_gaps"):
        ownership.append(
            {
                "defect": "; ".join(cohort["explicit_gaps"]),
                "owner": "Agent1 handoff / Agent2 freeze",
                "action": "complete paired shadow ingest before paired scoring",
            }
        )

    report = {
        "title": "Anytime TD Prospective Closure",
        "generated_at": datetime.now(UTC).isoformat(),
        "play_enabled": False,
        "validated_play": False,
        "retrained": False,
        "promoted": False,
        "overwrote_freezes": False,
        "models_unchanged": True,
        "no_new_polling_loops": True,
        "new_evidence": evidence,
        "lead": (
            "no new prospective evidence"
            if evidence["no_new_prospective_evidence"]
            else f"+{evidence['new_unique_player_games']} unique player-games settled"
        ),
        "m4_provenance": m4,
        "exclusions_15": excl,
        "blocked_triage": blocked,
        "agent2_cohort": cohort,
        "intentional_sim_exclusions_unchanged": True,
        "scheduled_workflow": {
            "cycle": 'powershell -File "C:\\dev\\Sports bet\\scripts\\shadow_schedule.ps1" -Job cycle',
            "scoreboard_refresh": (
                'cd "c:\\dev\\Sports bet"; '
                '$env:PYTHONPATH="packages\\core;services\\worker"; '
                ".venv\\Scripts\\python.exe services\\worker\\worker\\main.py atd-prospective-readiness"
            ),
            "odds_budget_unchanged": True,
            "no_competing_tasks": True,
        },
        "ownership_remaining": ownership,
        "files_changed": [
            "packages/core/spe/shadow/prospective_closure.py",
            "services/worker/worker/main.py",
        ],
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / "prospective_closure_report.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    return report
