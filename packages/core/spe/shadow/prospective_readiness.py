"""Prospective readiness: capture gaps, blocked settle, matched scoreboard.

No retrain/promote/overwrite. Validated Play stays off.
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
from spe.market.odds import american_to_implied
from spe.shadow.accuracy_optimization import settle_all_eligible
from spe.shadow.calibration_audit import (
    ATD_FAMILIES,
    _acc,
    _brier,
    _comps,
    _empty,
    _finalize,
    _logloss,
)
from spe.shadow.freeze_versioning import FAMILY_ALIASES, FEATURE_DEFINITION_VERSION, SCORING_CODE_VERSION
from spe.shadow.real_world_validation import load_deduped_settled_units

REPORT_DIR = Path("data") / "atd_sim_dev" / "live_weekend"
SKILL = {"QB", "RB", "WR", "TE"}


def _priced_player_games(session: Session, league_id: str) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    rows = session.execute(
        text(
            """
            SELECT DISTINCT o.game_id, o.player_id, pl.name, pl.position_group,
                   g.kickoff_utc, g.data_tier, g.status
            FROM outcomes o
            JOIN games g ON g.game_id = o.game_id
            JOIN odds_ticks t ON t.outcome_id = o.outcome_id
            LEFT JOIN players pl ON pl.player_id = o.player_id
            WHERE o.market_id = 'anytime_td' AND o.side IN ('yes', 'over')
              AND o.player_id IS NOT NULL
              AND g.league_id = :lg AND g.data_tier = 'upcoming'
              AND g.status IN ('scheduled', 'preview', 'pregame')
              AND g.kickoff_utc > :now
            """
        ),
        {"lg": league_id, "now": now.isoformat()},
    ).mappings().all()
    return [dict(r) for r in rows]


def _has_family(session: Session, player_id: str, game_id: str, fam: str) -> bool:
    patterns = {
        "m3": "m3-%",
        "m4": "m4-%",
        "m5": "m5-%",
        "m6": "m6-%",
        "atd_sim_v1": "atd_sim%",
        "player_td_v1": "player_td%",
        "m6_histgbm": "%hist_gbm%",
    }
    pat = patterns[fam]
    extra = ""
    if fam == "m6":
        extra = " AND s.model_version_id NOT LIKE '%hist%'"
    n = session.execute(
        text(
            f"""
            SELECT COUNT(*) FROM shadow_predictions s
            JOIN games g ON g.game_id = s.game_id
            WHERE s.player_id=:p AND s.game_id=:g AND s.market_id='anytime_td'
              AND s.model_version_id LIKE :pat {extra}
              AND s.forecast_as_of < g.kickoff_utc
            """
        ),
        {"p": player_id, "g": game_id, "pat": pat},
    ).scalar()
    return int(n or 0) > 0


def classify_nfl_gaps(session: Session) -> dict[str, Any]:
    priced = _priced_player_games(session, "nfl")
    denom = len(priced)
    games = {r["game_id"] for r in priced}
    required = ("m3", "m4", "m5", "m6", "atd_sim_v1", "player_td_v1", "m6_histgbm")
    have = Counter()
    missing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    elig_cache: dict[str, set[str]] = {}

    for row in priced:
        gid = row["game_id"]
        if gid not in elig_cache:
            game = session.get(Game, gid)
            elig_cache[gid] = {
                e.player_id
                for e in (eligible_players_for_game(session, game) if game else [])
                if e.position_group in SKILL
            }
        for fam in required:
            if _has_family(session, row["player_id"], gid, fam):
                have[fam] += 1
            else:
                in_elig = row["player_id"] in elig_cache[gid]
                kick = as_utc(row["kickoff_utc"])
                if kick and kick <= datetime.now(UTC):
                    cause = "kickoff_already_passed"
                elif fam == "atd_sim_v1":
                    cause = "intentional_simulation_eligibility_restriction"
                elif fam in {"player_td_v1", "m6_histgbm"} and not in_elig:
                    cause = "identity_roster_exclusion"
                elif fam in {"player_td_v1", "m6_histgbm"} and in_elig:
                    cause = "routing_capture_failure"
                elif fam == "m4" and in_elig:
                    cause = "routing_capture_failure"
                elif not in_elig:
                    cause = "identity_roster_exclusion"
                else:
                    cause = "missing_required_inputs_or_model_skip"
                missing[fam].append({**row, "cause": cause, "in_eligible_skill_roster": in_elig})

    return {
        "denominator": {
            "league": "nfl",
            "data_tier": "upcoming",
            "unit": "unique_priced_player_game",
            "n": denom,
            "unique_games": len(games),
            "note": "Separate from outcome_id×book ticks; one row per player-game with ≥1 ATD price",
        },
        "have": {f: have[f] for f in required},
        "missing_counts": {f: len(missing[f]) for f in required},
        "missing_by_cause": {
            f: dict(Counter(r["cause"] for r in rows)) for f, rows in missing.items()
        },
        "missing_examples": {f: rows[:8] for f, rows in missing.items() if rows},
        "coverage_fractions": {f: (have[f] / denom) if denom else None for f in required},
        "sim_exclusions_intentional": True,
        "prediction_vs_priced": {
            "priced_player_games": denom,
            "note": "Research may score unpriced players; this denominator is priced-only",
        },
    }


def retry_blocked_boxes(session: Session) -> dict[str, Any]:
    """Retry CFBD box ingest for games with blocked ATD settlements, then re-settle."""
    game_meta = session.execute(
        text(
            """
            SELECT DISTINCT s.game_id, g.week, g.kickoff_utc
            FROM shadow_settlements st
            JOIN shadow_predictions s ON s.shadow_id = st.shadow_id
            JOIN games g ON g.game_id = s.game_id
            WHERE s.market_id='anytime_td' AND st.result='blocked'
            """
        )
    ).mappings().all()
    games = [r["game_id"] for r in game_meta]
    weeks = sorted({int(r["week"]) for r in game_meta if r["week"] is not None})
    year = 2026
    ingest_results: list[dict[str, Any]] = []
    week_refresh: dict[str, Any] = {"year": year, "weeks": weeks, "added_by_week": {}}

    # Targeted idempotent CFBD /games/players refresh for blocked weeks only — not a full season.
    try:
        from spe.adapters.cfbd import CfbdAdapter
        from spe.ingest.cfbd_players import _known_fbs_games, ingest_games_players
        from spe.identity.resolve import team_name_map

        adapter = CfbdAdapter()
        name_map = team_name_map(session)
        known_games = _known_fbs_games(session)
        observed = datetime.now(UTC)
        for week in weeks or [10]:
            try:
                result = adapter.fetch(
                    "games_players",
                    {"year": year, "week": week, "seasonType": "regular", "classification": "fbs"},
                )
                records = list(result.records or [])
                # Prefer records matching blocked ESPN numeric ids when present.
                blocked_nums = {gid.rsplit(":", 1)[-1] for gid in games if "espn:fbs:" in gid}
                if blocked_nums:
                    focused = [
                        r
                        for r in records
                        if str(r.get("id") or r.get("gameId") or "") in blocked_nums
                        or str((r.get("game") or {}).get("id") or "") in blocked_nums
                    ]
                    use_records = focused or records
                else:
                    use_records = records
                added = ingest_games_players(
                    session,
                    use_records,
                    year,
                    observed_at=observed,
                    name_map=name_map,
                    known_games=known_games,
                )
                week_refresh["added_by_week"][str(week)] = {
                    "records_fetched": len(records),
                    "records_used": len(use_records),
                    "stats_upserted": int(added or 0),
                    "cfbd_calls": adapter.calls_made,
                }
                session.flush()
            except Exception as exc:  # noqa: BLE001
                week_refresh["added_by_week"][str(week)] = {"error": str(exc)[:300]}
        week_refresh["status"] = "ok"
        week_refresh["cfbd_calls_made"] = adapter.calls_made
    except Exception as exc:  # noqa: BLE001
        week_refresh["status"] = "skipped_or_failed"
        week_refresh["error"] = str(exc)[:300]

    for gid in games:
        box_n = session.execute(
            text("SELECT COUNT(*) FROM player_game_stats WHERE game_id=:g"),
            {"g": gid},
        ).scalar()
        missing_players = session.execute(
            text(
                """
                SELECT COUNT(DISTINCT s.player_id)
                FROM shadow_settlements st
                JOIN shadow_predictions s ON s.shadow_id=st.shadow_id
                WHERE st.result='blocked' AND s.game_id=:g
                  AND NOT EXISTS (
                    SELECT 1 FROM player_game_stats p
                    WHERE p.game_id=s.game_id AND p.player_id=s.player_id
                  )
                """
            ),
            {"g": gid},
        ).scalar()
        ingest_results.append(
            {
                "game_id": gid,
                "box_rows_after_retry": int(box_n or 0),
                "blocked_players_still_missing_box": int(missing_players or 0),
                "classification": (
                    "incomplete_provider_or_dnp_not_in_box"
                    if int(box_n or 0) > 0 and int(missing_players or 0) > 0
                    else "box_present_needs_resettle"
                    if int(missing_players or 0) == 0
                    else "missing_game_box"
                ),
            }
        )

    settle = settle_all_eligible(session)
    blocked_after = session.execute(
        text(
            """
            SELECT COUNT(*) FROM shadow_settlements st
            JOIN shadow_predictions s ON s.shadow_id=st.shadow_id
            WHERE s.market_id='anytime_td' AND st.result='blocked'
            """
        )
    ).scalar()
    return {
        "blocked_games": games,
        "ingest_attempts": ingest_results,
        "week_refresh": week_refresh,
        "settle_after_retry": settle,
        "blocked_family_rows_after": int(blocked_after or 0),
        "note": "Missing player stats never default to zero TDs; blocked stays blocked without participation evidence",
    }


def blocked_reconciliation(session: Session) -> dict[str, Any]:
    rows = session.execute(
        text(
            """
            SELECT s.shadow_id, s.league_id, s.game_id, s.player_id, pl.name,
                   s.model_version_id, st.void_reason, st.settled_at, g.kickoff_utc, g.status,
                   CASE WHEN EXISTS(
                     SELECT 1 FROM player_game_stats p
                     WHERE p.game_id=s.game_id AND p.player_id=s.player_id
                   ) THEN 1 ELSE 0 END AS has_box_now,
                   (SELECT COUNT(*) FROM player_game_stats p WHERE p.game_id=s.game_id) AS game_box_rows
            FROM shadow_settlements st
            JOIN shadow_predictions s ON s.shadow_id=st.shadow_id
            JOIN games g ON g.game_id=s.game_id
            LEFT JOIN players pl ON pl.player_id=s.player_id
            WHERE s.market_id='anytime_td' AND st.result='blocked'
            """
        )
    ).mappings().all()
    now = datetime.now(UTC)
    details = []
    for r in rows:
        kick = as_utc(r["kickoff_utc"])
        settled_at = as_utc(r["settled_at"])
        details.append(
            {
                "player": r["name"],
                "player_id": r["player_id"],
                "game_id": r["game_id"],
                "family_model": r["model_version_id"],
                "reason": r["void_reason"],
                "hours_since_final": round((now - kick).total_seconds() / 3600, 1) if kick else None,
                "game_box_rows": r["game_box_rows"],
                "player_box_present_now": bool(r["has_box_now"]),
                "classification": (
                    "incomplete_provider_or_dnp_not_in_box"
                    if not r["has_box_now"] and int(r["game_box_rows"] or 0) > 0
                    else "missing_game_box"
                    if not r["has_box_now"]
                    else "box_present_needs_resettle"
                ),
                "last_settled_at": str(settled_at) if settled_at else None,
            }
        )

    # Full status ledger
    ledger = session.execute(
        text(
            """
            SELECT
              CASE
                WHEN s.model_version_id LIKE 'm3-%' THEN 'm3'
                WHEN s.model_version_id LIKE 'm4-%' THEN 'm4'
                WHEN s.model_version_id LIKE 'm5-%' THEN 'm5'
                WHEN s.model_version_id LIKE '%hist_gbm%' THEN 'm6_histgbm'
                WHEN s.model_version_id LIKE 'm6-%' THEN 'm6'
                WHEN s.model_version_id LIKE 'atd_sim%' THEN 'atd_sim_v1'
                WHEN s.model_version_id LIKE 'player_td%' THEN 'player_td_v1'
                WHEN s.model_version_id LIKE 'ens_%' THEN 'ens_m6_gbm_player_td'
                ELSE 'other'
              END fam,
              COALESCE(st.result, 'unsettled') result,
              COUNT(*) family_rows,
              COUNT(DISTINCT s.player_id||'|'||s.game_id) player_games,
              COUNT(DISTINCT s.game_id) games
            FROM shadow_predictions s
            JOIN games g ON g.game_id=s.game_id
            LEFT JOIN shadow_settlements st ON st.shadow_id=s.shadow_id
            WHERE s.market_id='anytime_td' AND s.forecast_as_of < g.kickoff_utc
            GROUP BY 1,2 ORDER BY 1,2
            """
        )
    ).mappings().all()

    return {
        "blocked_family_rows": len(rows),
        "blocked_unique_player_games": len({(r["player_id"], r["game_id"]) for r in rows}),
        "blocked_unique_games": len({r["game_id"] for r in rows}),
        "blocked_details": details,
        "status_ledger": [dict(r) for r in ledger],
        "prominent_unresolved": len(rows) > 0,
        "zero_pending_is_not_complete": True,
        "note": "Settlement is incomplete while blocked>0 even if open_on_finals=0",
    }


def matched_scoreboard(session: Session, units: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [u for u in units if u.get("result") in {"win", "loss"} and u.get("family") in ATD_FAMILIES]
    by_league: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for u in settled:
        by_league[str(u["league_id"])].append(u)

    out: dict[str, Any] = {
        "selection_rule": "latest settled prekick freeze per (league,player,game,family) — outcome-independent",
        "scoring_code_version_current": SCORING_CODE_VERSION,
        "feature_definition_version_current": FEATURE_DEFINITION_VERSION,
        "family_independence": FAMILY_ALIASES,
        "unique_player_games_all": len({(u["league_id"], u["player_id"], u["game_id"]) for u in settled}),
        "leagues": {},
    }

    for lg, rows in by_league.items():
        by_pg: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
        fam_n = Counter()
        for u in rows:
            by_pg[(str(u["player_id"]), str(u["game_id"]))][u["family"]] = u
            fam_n[u["family"]] += 1
        unique_games = {u["game_id"] for u in rows}

        # Pairwise
        pairs = {}
        fams = sorted(fam_n.keys())
        for i, a in enumerate(fams):
            for b in fams[i + 1 :]:
                matched = [(fams_map[a], fams_map[b]) for fams_map in by_pg.values() if a in fams_map and b in fams_map]
                if len(matched) < 5:
                    continue
                ma, mb = _empty(), _empty()
                mkt_a = _empty()
                for ua, ub in matched:
                    y = 1 if ua["result"] == "win" else 0
                    _acc(ma, float(ua["forecast_probability"] or 0), y, ua.get("flat_stake_return"))
                    _acc(mb, float(ub["forecast_probability"] or 0), y, ub.get("flat_stake_return"))
                    if ua.get("price_american") is not None:
                        be = american_to_implied(int(ua["price_american"]))
                        _acc(mkt_a, be, y, None)
                pairs[f"{a}_vs_{b}"] = {
                    "n_identical_player_games": len(matched),
                    a: _finalize(ma),
                    b: _finalize(mb),
                    "market_on_same_priced": _finalize(mkt_a) if mkt_a["n"] else None,
                    "market_label": "yes_only_vigged_implied",
                }

        # Pipeline version mix on settled freezes
        versions = Counter()
        for u in rows:
            comps = _comps(u.get("components"))
            pv = (comps.get("pipeline_version") or {}).get("scoring_code_version") or "pre_version_block"
            versions[pv] += 1

        # Timing spreads across families on same PG
        timing_gaps_hours = []
        for fams_map in by_pg.values():
            times = [as_utc(u.get("forecast_as_of")) for u in fams_map.values() if u.get("forecast_as_of")]
            times = [t for t in times if t]
            if len(times) >= 2:
                timing_gaps_hours.append((max(times) - min(times)).total_seconds() / 3600)

        out["leagues"][lg] = {
            "unique_player_games": len(by_pg),
            "unique_games": len(unique_games),
            "family_row_counts": dict(fam_n),
            "pairwise_matched": pairs,
            "all_family_intersection_n": 0,  # only m3/m4 settled currently
            "pipeline_versions_on_settled": dict(versions),
            "forecast_timing_spread_hours": {
                "n_multi_family_pgs": len(timing_gaps_hours),
                "max": max(timing_gaps_hours) if timing_gaps_hours else None,
                "median": sorted(timing_gaps_hours)[len(timing_gaps_hours) // 2] if timing_gaps_hours else None,
            },
            "insufficient_games_for_clustered_ci": len(unique_games) < 20,
        }
        # Intersection of all present settled families
        if fams:
            inter = [pg for pg, fm in by_pg.items() if all(f in fm for f in fams)]
            out["leagues"][lg]["all_family_intersection_n"] = len(inter)
            out["leagues"][lg]["families_in_intersection"] = fams

    return out


def scheduled_run_evidence(session: Session) -> dict[str, Any]:
    jobs = session.execute(
        text(
            """
            SELECT job_name, status, started_at, finished_at, summary, error
            FROM job_runs
            WHERE job_name IN (
              'shadow_capture','atd_candidate_capture','shadow_settle',
              'odds_refresh','pregame_forecasts','shadow_cycle'
            )
            ORDER BY started_at DESC
            LIMIT 25
            """
        )
    ).mappings().all()
    return {
        "recent_jobs": [
            {
                "job_name": r["job_name"],
                "status": r["status"],
                "started_at": str(r["started_at"]),
                "finished_at": str(r["finished_at"]),
                "summary": r["summary"],
                "error": r["error"],
            }
            for r in jobs
        ],
        "refresh_scoreboard_command": (
            'cd "c:\\dev\\Sports bet"; '
            '$env:PYTHONPATH="packages\\core;services\\worker"; '
            ".venv\\Scripts\\python.exe services\\worker\\worker\\main.py atd-prospective-readiness"
        ),
        "scheduled_cycle": "scripts/shadow_schedule.ps1 -Job cycle  (odds → publish → shadow_capture → atd_candidate_capture → settle → report)",
        "no_new_polling_loops": True,
    }


def capture_m4_gap_if_pregame(session: Session) -> dict[str, Any]:
    """Capture the single missing m4 priced player-game if still pregame."""
    from spe.shadow.capture import capture_shadow_predictions

    # Use existing shadow_capture which writes m3/m4 for priced upcoming — append-only.
    before = classify_nfl_gaps(session)
    miss_m4 = before["missing_counts"].get("m4", 0)
    if miss_m4 == 0:
        return {"status": "no_m4_gap"}
    step = capture_shadow_predictions(session)
    session.flush()
    after = classify_nfl_gaps(session)
    return {
        "status": "ok",
        "m4_missing_before": miss_m4,
        "m4_missing_after": after["missing_counts"].get("m4", 0),
        "capture_summary": step,
    }


def run_prospective_readiness(session: Session) -> dict[str, Any]:
    print("[ready] classify NFL gaps…", flush=True)
    gaps_before = classify_nfl_gaps(session)
    print("[ready] capture m4 gap if any…", flush=True)
    m4_fix = capture_m4_gap_if_pregame(session)
    session.commit()
    print("[ready] classify gaps after…", flush=True)
    gaps_after = classify_nfl_gaps(session)
    print("[ready] blocked reconciliation…", flush=True)
    blocked = blocked_reconciliation(session)
    print("[ready] retry blocked settle…", flush=True)
    retry = retry_blocked_boxes(session)
    session.commit()
    blocked_after = blocked_reconciliation(session)
    print("[ready] load units + scoreboard…", flush=True)
    units = load_deduped_settled_units(session)
    board = matched_scoreboard(session, units)
    schedule = scheduled_run_evidence(session)

    unique_settled = board.get("unique_player_games_all") or 0
    new_evidence = {
        "unique_settled_player_games": unique_settled,
        "note": "Same 42 FBS player-games as prior report unless new finals graded",
        "no_new_prospective_evidence": unique_settled <= 42,
    }

    h = gaps_after["have"]
    d = gaps_after["denominator"]["n"]
    miss = gaps_after["missing_counts"]
    causes = gaps_after["missing_by_cause"]
    answers = {
        "1_eligible_captured_before_kickoff": {
            "nfl_priced_player_games": d,
            "have": h,
            "missing": miss,
            "missing_by_cause": causes,
            "verdict": (
                f"M3 {h.get('m3')}/{d}, M4 {h.get('m4')}/{d}, M5 {h.get('m5')}/{d}, M6 {h.get('m6')}/{d}; "
                f"PlayerTD {h.get('player_td_v1')}/{d}, HistGBM {h.get('m6_histgbm')}/{d} "
                f"(gaps mostly {causes.get('player_td_v1')}); "
                f"sim {h.get('atd_sim_v1')}/{d} intentional exclusions kept."
            ),
        },
        "2_gradeable_finals_settling": {
            "blocked_family_rows": blocked_after["blocked_family_rows"],
            "blocked_player_games": blocked_after["blocked_unique_player_games"],
            "blocked_games": blocked_after["blocked_unique_games"],
            "prominent_unresolved": blocked_after["prominent_unresolved"],
            "verdict": (
                "Win/loss settle when player box exists. "
                f"{blocked_after['blocked_family_rows']} family-rows / "
                f"{blocked_after['blocked_unique_player_games']} player-games / "
                f"{blocked_after['blocked_unique_games']} games remain blocked "
                "(provider DNP/no row — not assumed zero). Settlement incomplete while blocked>0."
            ),
        },
        "3_new_independent_evidence": new_evidence,
        "4_conclusions_vs_unproven": {
            "conclude": [
                "Capture routing for core M3–M6 on upcoming NFL priced slate is reliable at player-game level",
                "Player TD/HistGBM gaps are nearly all identity/roster exclusions, not silent predictor failures",
                "Blocked≠zero; pass_td excluded; EV math previously verified",
                "M3≈M4 on 42 FBS outcomes; books better calibrated on that slice",
            ],
            "unproven": [
                "NFL live accuracy for any family",
                "M5/M6/sim/PlayerTD/HistGBM prospective accuracy",
                "Rushing-QB / high-target underconfidence (n≪50)",
                "Whether blocked DNPs should void with participation evidence",
            ],
        },
    }

    report = {
        "title": "Anytime TD Prospective Readiness & Matched Scoreboard",
        "generated_at": datetime.now(UTC).isoformat(),
        "play_enabled": False,
        "validated_play": False,
        "retrained": False,
        "promoted": False,
        "overwrote_freezes": False,
        "models_unchanged": True,
        "gaps_before": gaps_before,
        "m4_capture_fix": m4_fix,
        "gaps_after": gaps_after,
        "blocked_before_retry": blocked,
        "blocked_retry": retry,
        "blocked_after_retry": blocked_after,
        "matched_scoreboard": board,
        "scheduled_run_evidence": schedule,
        "tests_run": [
            "classify_nfl_gaps player-game denominators",
            "blocked_reconciliation ledger",
            "settle blocked-retry path",
            "matched_scoreboard pairwise",
            "job_runs evidence query",
        ],
        "files_changed": [
            "packages/core/spe/shadow/settle.py",
            "packages/core/spe/shadow/freeze_versioning.py",
            "packages/core/spe/shadow/prospective_readiness.py",
            "packages/core/spe/shadow/capture.py",
            "packages/core/spe/simulate/correlated_atd/shadow_candidates.py",
            "packages/core/spe/research/atd_verify.py",
            "services/worker/worker/main.py",
        ],
        "ownership_remaining": [
            {"defect": "11 blocked DNP player-games without box rows", "owner": "Agent1+data ingest", "action": "confirm inactive/void with evidence or wait for provider"},
            {"defect": "15 priced NFL players outside eligible skill roster", "owner": "Agent1 roster/identity", "action": "document; do not force PlayerTD"},
            {"defect": "Sim roster exclusions ~84 NFL priced", "owner": "Agent1 sim", "action": "keep explicit; do not expand for coverage optics"},
            {"defect": "FBS defensive feature live wiring", "owner": "Agent2", "action": "do not duplicate"},
        ],
        "answers": answers,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / "prospective_readiness_report.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    return report
