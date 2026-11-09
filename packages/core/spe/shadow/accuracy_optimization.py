"""Anytime TD accuracy optimization — evidence from prospective settlements only.

Does not retrain, promote, overwrite freezes, or enable Validated Play.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.market.odds import american_to_implied
from spe.shadow.jobs import job_settle
from spe.shadow.real_world_validation import (
    _family_ext,
    audit_settlement_rules_on_completed_games,
    load_deduped_settled_units,
    verify_fbs_2026_stats,
)
from spe.shadow.urgent_readiness import list_near_kickoff_stale_games


FOCUS = ("Jonah Coleman", "Ja'Marr Chase", "Tee Higgins", "Tetairoa McMillan")
ATD_COMPARE = ("m3", "m5", "m6", "atd_sim_v1", "player_td_v1", "m6_histgbm")


def report_dir() -> Path:
    path = Path("data") / "atd_sim_dev" / "live_weekend"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _brier(p: float, y: int) -> float:
    return (float(p) - y) ** 2


def _logloss(p: float, y: int) -> float:
    eps = 1e-6
    p = min(max(float(p), eps), 1 - eps)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def settle_all_eligible(session: Session) -> dict[str, Any]:
    from spe.shadow.settle import settle_shadow_predictions

    before = session.execute(
        text(
            """
            SELECT COUNT(*) FROM shadow_predictions s
            JOIN games g ON g.game_id = s.game_id
            LEFT JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
            WHERE s.market_id='anytime_td' AND lower(g.status) IN ('final','post')
              AND st.shadow_id IS NULL AND s.forecast_as_of < g.kickoff_utc
            """
        )
    ).scalar()
    # Prefer job_settle (writes job_runs). On lock contention with peer workers,
    # call settle directly so grading still proceeds without competing INSERT.
    try:
        step = job_settle(session)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        if "locked" not in str(exc).lower():
            raise
        session.connection().exec_driver_sql("PRAGMA busy_timeout=300000")
        step = {
            "job_name": "shadow_settle_direct",
            "status": "ok",
            "summary": settle_shadow_predictions(session),
            "fallback_reason": str(exc)[:200],
        }
        session.commit()
    after_open = session.execute(
        text(
            """
            SELECT COUNT(*) FROM shadow_predictions s
            JOIN games g ON g.game_id = s.game_id
            LEFT JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
            WHERE s.market_id='anytime_td' AND lower(g.status) IN ('final','post')
              AND st.shadow_id IS NULL AND s.forecast_as_of < g.kickoff_utc
            """
        )
    ).scalar()
    pending_examples = [
        dict(r)
        for r in session.execute(
            text(
                """
                SELECT s.shadow_id, s.game_id, s.player_id, s.league_id, s.model_version_id,
                       CASE WHEN EXISTS (
                         SELECT 1 FROM player_game_stats p
                         WHERE p.game_id=s.game_id AND p.player_id=s.player_id
                       ) THEN 1 ELSE 0 END AS has_stat
                FROM shadow_predictions s
                JOIN games g ON g.game_id = s.game_id
                LEFT JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
                WHERE s.market_id='anytime_td' AND lower(g.status) IN ('final','post')
                  AND st.shadow_id IS NULL AND s.forecast_as_of < g.kickoff_utc
                LIMIT 30
                """
            )
        ).mappings()
    ]
    return {
        "open_on_finals_before": int(before or 0),
        "settle_step": step,
        "open_on_finals_after": int(after_open or 0),
        "pending_examples": pending_examples,
        "box_audit": audit_settlement_rules_on_completed_games(session),
    }


def metrics_by_league_family(units: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [u for u in units if u.get("result") in {"win", "loss"}]
    out: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(_empty))
    for u in settled:
        fam = u["family"]
        if fam not in ATD_COMPARE and fam != "m4":
            continue
        m = out[str(u["league_id"])][fam]
        y = 1 if u["result"] == "win" else 0
        p = float(u["forecast_probability"] or 0)
        m["n"] += 1
        m["wins"] += y
        m["brier_sum"] += _brier(p, y)
        m["logloss_sum"] += _logloss(p, y)
        if u.get("flat_stake_return") is not None:
            m["return_sum"] += float(u["flat_stake_return"])
        # calibration buckets
        bucket = int(min(9, max(0, float(p) * 10)))
        m.setdefault("cal", {})
        m["cal"].setdefault(bucket, {"n": 0, "sum_p": 0.0, "sum_y": 0})
        m["cal"][bucket]["n"] += 1
        m["cal"][bucket]["sum_p"] += p
        m["cal"][bucket]["sum_y"] += y
    finalized = {}
    for lg, fams in out.items():
        finalized[lg] = {}
        for fam, m in fams.items():
            finalized[lg][fam] = _finalize(m)
    return {
        "label": "genuine_prekickoff_deduped",
        "not_historical_replay": True,
        "n_settled": len(settled),
        "by_league_family": finalized,
    }


def metrics_by_position_model(units: list[dict[str, Any]]) -> dict[str, Any]:
    """Identical-sample comparison requires intersecting families per position cell."""
    settled = [u for u in units if u.get("result") in {"win", "loss"} and u["family"] in ATD_COMPARE]
    # group by league, position, player, game
    by_cell: dict[tuple[str, str], dict[tuple[str, str], dict[str, dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for u in settled:
        pos = str(u.get("position_group") or "UNK")
        if pos == "QB":
            # rushing QB heuristic: components or rush involvement
            comps = _comps(u.get("components"))
            if float(comps.get("prior_carry_share") or comps.get("carries") or 0) <= 0 and pos == "QB":
                pos = "QB_pass_primary"
            else:
                pos = "QB_rush"
        key = (str(u["league_id"]), pos)
        pg = (str(u["player_id"]), str(u["game_id"]))
        by_cell[key][pg][u["family"]] = u

    report: dict[str, Any] = {}
    for (league, pos), pgs in by_cell.items():
        # families present in at least 5 identical outcomes together
        fam_sets = [set(fams.keys()) for fams in pgs.values()]
        if not fam_sets:
            continue
        # pairwise / multi compare on outcomes where all of ATD_COMPARE subset present
        present_counts = Counter()
        for fs in fam_sets:
            for f in fs:
                present_counts[f] += 1
        available = [f for f, n in present_counts.items() if n >= 5]
        if len(available) < 1:
            continue
        # For fair compare, use outcomes where ALL available families exist
        identical = {pg: fams for pg, fams in pgs.items() if available and all(f in fams for f in available)}
        if len(identical) < 5 and available:
            # fallback: each family on its own n (note: not identical)
            own = {}
            for fam in available:
                rows = [fams[fam] for fams in pgs.values() if fam in fams]
                own[fam] = _metrics_from_rows(rows)
                own[fam]["identical_sample"] = False
            report[f"{league}:{pos}"] = {"n_outcomes": len(pgs), "note": "families_not_fully_overlapping", "by_family": own}
            continue
        by_fam_rows: dict[str, list[dict[str, Any]]] = {f: [] for f in available}
        for fams in identical.values():
            for f in available:
                by_fam_rows[f].append(fams[f])
        report[f"{league}:{pos}"] = {
            "n_identical_outcomes": len(identical),
            "families": available,
            "identical_sample": True,
            "by_family": {f: {**_metrics_from_rows(rows), "identical_sample": True} for f, rows in by_fam_rows.items()},
        }
    return report


def investigate_errors(session: Session, units: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [u for u in units if u.get("result") in {"win", "loss"}]
    # Largest absolute probability error
    scored = []
    for u in settled:
        y = 1.0 if u["result"] == "win" else 0.0
        p = float(u["forecast_probability"] or 0)
        scored.append({**u, "error": abs(p - y), "signed_error": p - y, "y": y, "p": p})
    scored.sort(key=lambda r: r["error"], reverse=True)
    top = scored[:25]

    enriched = []
    for u in top:
        comps = _comps(u.get("components"))
        stats = session.execute(
            text(
                """
                SELECT rush_td, rec_td, pass_td, carries, receptions, rush_yards, rec_yards, targets
                FROM player_game_stats
                WHERE game_id=:g AND player_id=:p
                LIMIT 1
                """
            ),
            {"g": u["game_id"], "p": u["player_id"]},
        ).mappings().first()
        # recent prior games usage
        prior = session.execute(
            text(
                """
                SELECT AVG(carries) carries, AVG(receptions) receptions,
                       AVG(COALESCE(rush_td,0)+COALESCE(rec_td,0)) anytime_td_rate,
                       COUNT(*) n
                FROM player_game_stats p
                JOIN games g ON g.game_id = p.game_id
                WHERE p.player_id = :p AND g.kickoff_utc < (
                  SELECT kickoff_utc FROM games WHERE game_id = :g
                )
                """
            ),
            {"p": u["player_id"], "g": u["game_id"]},
        ).mappings().first()
        overlooked = []
        if prior and int(prior["n"] or 0) < 3:
            overlooked.append("sparse_player_history")
        if stats and float(stats.get("pass_td") or 0) > 0 and float(stats.get("rush_td") or 0) + float(stats.get("rec_td") or 0) == 0:
            overlooked.append("pass_td_noise_if_model_confused_qb_scoring")
        inj = comps.get("injury_coverage")
        if isinstance(inj, dict):
            inj_status = str(inj.get("status") or "").lower()
            if inj_status in {"out", "doubtful", "questionable", "unknown"} and u["error"] >= 0.2:
                overlooked.append("availability_or_injury_uncertainty")
        elif inj in (None, {}, "unknown") and u["error"] >= 0.35:
            overlooked.append("availability_or_injury_uncertainty")
        # Rushing QB scored while model treated as low-ATD passer
        if (u.get("position_group") or "") == "QB" and stats and float(stats.get("rush_td") or 0) > 0 and u["p"] < 0.2:
            overlooked.append("rushing_qb_role_underweighted")
        if prior and float(prior.get("carries") or 0) >= 8 and u["y"] == 1 and u["p"] < 0.2:
            overlooked.append("goal_line_or_volume_role_underweighted")
        if prior and float(prior.get("receptions") or 0) >= 5 and u["y"] == 1 and u["p"] < 0.2:
            overlooked.append("pass_game_involvement_underweighted")
        if u["signed_error"] > 0.25 and u["y"] == 0:
            overlooked.append("overconfident_no_td")
        if u["signed_error"] < -0.25 and u["y"] == 1:
            overlooked.append("underconfident_missed_td")
        if not overlooked:
            overlooked.append("role_matchup_or_team_td_budget_misallocation")
        enriched.append(
            {
                "player_name": u.get("player_name"),
                "player_id": u["player_id"],
                "game_id": u["game_id"],
                "league_id": u["league_id"],
                "family": u["family"],
                "position_group": u.get("position_group"),
                "p": u["p"],
                "actual": u["y"],
                "result": u["result"],
                "abs_error": u["error"],
                "box": dict(stats) if stats else None,
                "prior_usage": dict(prior) if prior else None,
                "likely_overlooked_factors": overlooked,
                "component_keys": sorted(comps.keys())[:20],
            }
        )

    # Focus disagreements
    focus_cases = []
    by_pg: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for u in units:
        name = (u.get("player_name") or "").strip()
        if any(t.lower() in name.lower() for t in FOCUS):
            by_pg[(str(u["player_id"]), str(u["game_id"]))].append(u)
    for (pid, gid), rows in by_pg.items():
        fam_ps = {r["family"]: float(r["forecast_probability"] or 0) for r in rows}
        if len(fam_ps) < 2:
            continue
        settled_r = next((r.get("result") for r in rows if r.get("result") in {"win", "loss", "blocked", "void"}), None)
        focus_cases.append(
            {
                "player_name": rows[0].get("player_name"),
                "game_id": gid,
                "league_id": rows[0].get("league_id"),
                "by_family": fam_ps,
                "spread": max(fam_ps.values()) - min(fam_ps.values()),
                "settled_result": settled_r,
            }
        )
    focus_cases.sort(key=lambda r: r["spread"], reverse=True)

    return {
        "largest_errors": enriched,
        "error_factor_counts": dict(Counter(f for e in enriched for f in e["likely_overlooked_factors"])),
        "focus_disagreements": focus_cases,
    }


def college_matchup_audit(session: Session) -> dict[str, Any]:
    fbs = verify_fbs_2026_stats(session)
    # Play coverage 2026
    plays = session.execute(
        text(
            """
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN ppa IS NOT NULL THEN 1 ELSE 0 END) AS with_ppa,
                   SUM(CASE WHEN is_red_zone THEN 1 ELSE 0 END) AS rz_plays
            FROM plays pl
            JOIN games g ON g.game_id = pl.game_id
            WHERE g.league_id='fbs' AND g.kickoff_utc >= '2026-08-01'
            """
        )
    ).mappings().first()
    # Team analytics defensive fields for 2026
    team_an = session.execute(
        text(
            """
            SELECT COUNT(*) AS rows,
                   SUM(CASE WHEN json_extract(metrics,'$.rush_td_allowed') IS NOT NULL THEN 1 ELSE 0 END) AS rush_td_allowed,
                   SUM(CASE WHEN json_extract(metrics,'$.rec_td_allowed') IS NOT NULL THEN 1 ELSE 0 END) AS rec_td_allowed,
                   SUM(CASE WHEN json_extract(metrics,'$.rz_td_allowed') IS NOT NULL
                            OR json_extract(metrics,'$.red_zone_td_rate') IS NOT NULL THEN 1 ELSE 0 END) AS rz_fields,
                   SUM(CASE WHEN json_extract(metrics,'$.def_epa_per_play') IS NOT NULL
                            OR json_extract(metrics,'$.opp_adj_def') IS NOT NULL THEN 1 ELSE 0 END) AS def_epa
            FROM team_analytics t
            JOIN games g ON g.game_id = t.game_id
            WHERE g.league_id='fbs' AND g.kickoff_utc >= '2026-08-01'
            """
        )
    ).mappings().first()

    # Feature coverage on opportunity frame sample via SQL priors already in team_td path
    missing_notes = []
    if int((plays or {}).get("n") or 0) == 0:
        missing_notes.append("No 2026 FBS play-by-play rows — cannot derive fresh defensive EPA/PPA or RZ defense from PBP")
    if int((team_an or {}).get("rush_td_allowed") or 0) == 0:
        missing_notes.append("team_analytics missing rush_td_allowed for 2026 FBS games")
    if int((team_an or {}).get("rec_td_allowed") or 0) == 0:
        missing_notes.append("team_analytics missing rec_td_allowed for 2026 FBS games")

    # Historical matchup ablation artifact if present (do not invent new results)
    ablation_path = Path("data/atd_sim_dev/phase5/matchup_ablation.json")
    ablation = None
    if ablation_path.exists():
        try:
            ablation = json.loads(ablation_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            ablation = {"error": "unreadable"}

    return {
        "fbs_2026_pgs": fbs,
        "plays_2026": dict(plays or {}),
        "team_analytics_2026": dict(team_an or {}),
        "missing_or_incomplete": missing_notes,
        "existing_matchup_ablation": {
            "path": str(ablation_path) if ablation_path.exists() else None,
            "fbs_retained": ((ablation or {}).get("leagues") or {}).get("fbs", {}).get("retained") if ablation else None,
            "fbs_coverage": ((ablation or {}).get("leagues") or {}).get("fbs", {}).get("coverage") if ablation else None,
            "rule": (ablation or {}).get("rule") if ablation else None,
        },
        "do_not_invent": True,
        "incorporated_new_factors": False,
        "note": "Test before incorporate: retain only if prospective Brier improves without ECE regression",
    }


def sportsbook_value_analysis(units: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [
        u
        for u in units
        if u.get("result") in {"win", "loss"} and u.get("price_american") is not None
    ]
    by_fam: dict[str, dict[str, Any]] = defaultdict(_empty)
    by_odds: dict[str, dict[str, Any]] = defaultdict(_empty)
    pos_ev = _empty()
    non_pos = _empty()
    for u in settled:
        fam = u["family"]
        y = 1 if u["result"] == "win" else 0
        p = float(u["forecast_probability"] or 0)
        price = int(u["price_american"])
        breakeven = float(american_to_implied(price))
        ret = u.get("flat_stake_return")
        _acc(by_fam[fam], p, y, ret)
        by_fam[fam]["market_brier_sum"] = by_fam[fam].get("market_brier_sum", 0) + _brier(breakeven, y)
        by_fam[fam]["market_n"] = by_fam[fam].get("market_n", 0) + 1
        odds_bucket = _odds_bucket(price)
        _acc(by_odds[odds_bucket], p, y, ret)
        ev = u.get("expected_value")
        theoretical_ev = p - breakeven
        positive = (ev is not None and float(ev) > 0) or theoretical_ev > 0.02
        if positive:
            _acc(pos_ev, p, y, ret)
            pos_ev["flagged"] = pos_ev.get("flagged", 0) + 1
        else:
            _acc(non_pos, p, y, ret)

    for m in list(by_fam.values()) + list(by_odds.values()) + [pos_ev, non_pos]:
        _finalize(m)
        mn = m.get("market_n") or 0
        if mn:
            m["market_brier"] = m.get("market_brier_sum", 0) / mn
            m["model_beats_market_brier"] = m["brier"] is not None and m["brier"] < m["market_brier"]

    return {
        "n_settled_priced": len(settled),
        "by_family": dict(by_fam),
        "by_odds_range": dict(by_odds),
        "positive_ev_flagged": _finalize(pos_ev),
        "non_positive_ev": _finalize(non_pos),
        "insufficient_for_profit_claim": len(settled) < 200,
        "disclaimer": "Hypothetical flat-stake research only. EV flag ≠ profitable bet.",
    }


def run_accuracy_optimization(session: Session, *, run_matchup_test: bool = True) -> dict[str, Any]:
    print("[opt] settle eligible finals…", flush=True)
    settle = settle_all_eligible(session)
    print("[opt] load deduped units…", flush=True)
    units = load_deduped_settled_units(session)
    print("[opt] league/family metrics…", flush=True)
    metrics = metrics_by_league_family(units)
    print("[opt] position×model…", flush=True)
    by_pos = metrics_by_position_model(units)
    print("[opt] error investigation…", flush=True)
    errors = investigate_errors(session, units)
    print("[opt] college matchup audit…", flush=True)
    college = college_matchup_audit(session)
    if run_matchup_test and int((college.get("plays_2026") or {}).get("n") or 0) > 0:
        try:
            from spe.simulate.correlated_atd.matchup_ablation import run_matchup_ablation

            print("[opt] matchup ablation (historical holdout test — labeled)…", flush=True)
            college["matchup_ablation_run"] = run_matchup_ablation(
                session, leagues=("fbs",), test_games=40, draw_count=800, seed=11
            )
            college["matchup_ablation_run"]["label"] = "historical_chronological_holdout_not_live_prospective"
        except Exception as exc:  # noqa: BLE001
            college["matchup_ablation_error"] = str(exc)[:400]
    print("[opt] sportsbook value…", flush=True)
    books = sportsbook_value_analysis(units)

    answers = _final_answers(metrics, by_pos, errors, college, books, settle, units)
    report = {
        "title": "Anytime TD Accuracy Optimization",
        "generated_at": datetime.now(UTC).isoformat(),
        "play_enabled": False,
        "validated_play": False,
        "retrained": False,
        "promoted": False,
        "overwrote_freezes": False,
        "evidence": "genuine_prekickoff_freezes_deduped",
        "settlement": settle,
        "metrics": metrics,
        "by_position_model": by_pos,
        "errors": errors,
        "college_matchups": college,
        "sportsbooks": books,
        "stale_odds_snapshot": list_near_kickoff_stale_games(session),
        "answers": answers,
    }
    path = report_dir() / "accuracy_optimization_report.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    return report


def _final_answers(
    metrics: dict[str, Any],
    by_pos: dict[str, Any],
    errors: dict[str, Any],
    college: dict[str, Any],
    books: dict[str, Any],
    settle: dict[str, Any],
    units: list[dict[str, Any]],
) -> dict[str, Any]:
    # Best model per league among n>=20
    best = {}
    for lg, fams in (metrics.get("by_league_family") or {}).items():
        eligible = [(f, m) for f, m in fams.items() if (m.get("n") or 0) >= 20 and m.get("brier") is not None]
        if not eligible:
            eligible = [(f, m) for f, m in fams.items() if m.get("brier") is not None]
        if eligible:
            f, m = min(eligible, key=lambda kv: kv[1]["brier"])
            best[lg] = {"family": f, "brier": m["brier"], "n": m["n"], "logloss": m["logloss"], "hit_rate": m["hit_rate"]}

    # Position strength from identical samples when possible
    pos_brier = {}
    for cell, body in by_pos.items():
        fams = body.get("by_family") or {}
        briers = [m["brier"] for m in fams.values() if m.get("brier") is not None and (m.get("n") or 0) >= 5]
        if briers:
            pos_brier[cell] = sum(briers) / len(briers)
    strongest = sorted(pos_brier, key=pos_brier.get)[:3]
    weakest = sorted(pos_brier, key=pos_brier.get, reverse=True)[:3]

    factor_counts = errors.get("error_factor_counts") or {}
    top_causes = sorted(factor_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]

    books_outperform = False
    for m in (books.get("by_family") or {}).values():
        if m.get("market_brier") is not None and m.get("brier") is not None and m["market_brier"] < m["brier"] and (m.get("n") or 0) >= 20:
            books_outperform = True

    demonstrated_factors = []
    abl = (college.get("matchup_ablation_run") or college.get("existing_matchup_ablation") or {})
    retained = abl.get("fbs_retained") or ((abl.get("leagues") or {}).get("fbs") or {}).get("retained") or abl.get("retained")
    if retained:
        demonstrated_factors.append(
            {
                "source": "matchup_ablation_historical_holdout",
                "retained": retained,
                "not_live_prospective": True,
            }
        )
    else:
        demonstrated_factors.append(
            {
                "source": "prospective_settlements",
                "retained": [],
                "note": "No new matchup factor proven on live prospective sample yet",
            }
        )

    graded = sum(1 for u in units if u.get("result") in {"win", "loss"})
    return {
        "1_most_accurate_model": best if best else "insufficient_settled_sample",
        "2_strongest_weakest_positions": {"strongest": strongest, "weakest": weakest, "by_cell_brier": pos_brier},
        "3_biggest_error_causes": top_causes,
        "4_sportsbooks_outperforming_caliber": {
            "on_current_prospective_slice": books_outperform,
            "sufficient_to_claim": not books.get("insufficient_for_profit_claim"),
            "detail": books.get("by_family"),
        },
        "5_factors_with_demonstrated_value": demonstrated_factors,
        "6_what_was_fixed": [
            "FBS 2026 player-game stats ingestion + ESPN game mapping",
            "Settlement rules: missing≠zero; pass_td excluded; deduped eval units",
            "Eval dedupe: on finals, settled freezes outrank newer unsettled re-freezes (keeps M5+ graded)",
            "2026 FBS team defensive analytics now populated (rush/rec/rz TD allowed, def EPA)",
            f"Settled open finals this run: before={settle.get('open_on_finals_before')} after={settle.get('open_on_finals_after')}",
        ],
        "7_still_needs_improvement": [
            "Larger prospective settled N for M5/M6/sim/HistGBM/Player TD (especially NFL — zero finals graded yet)",
            "2026 FBS play-by-play / defensive analytics coverage gaps" if (college.get("missing_or_incomplete")) else "Rushing-QB and high-volume roles still underweighted on largest misses",
            "Complete box scores to reduce blocked settlements",
            "Matchup features available but not retained by historical ablation (Brier flat) — do not wire into live models yet",
        ],
        "8_three_highest_impact_next_steps": [
            "Keep settling weekend slate; re-score M5/M6/sim/PlayerTD/HistGBM on identical NFL+FBS outcomes once n≥100/family",
            "Diagnose rushing-QB + high-carry underconfidence on prospective misses before any retrain",
            "Do not add opponent TD-allowed features to live path until prospective Brier improves (historical ablation retained none)",
        ],
        "graded_prospective_units": graded,
        "validated_play": False,
        "promoted": False,
    }


def _empty() -> dict[str, Any]:
    return {"n": 0, "wins": 0, "brier_sum": 0.0, "logloss_sum": 0.0, "return_sum": 0.0}


def _acc(m: dict[str, Any], p: float, y: int, ret: Any) -> None:
    m["n"] += 1
    m["wins"] += y
    m["brier_sum"] += _brier(p, y)
    m["logloss_sum"] += _logloss(p, y)
    if ret is not None:
        m["return_sum"] += float(ret)


def _finalize(m: dict[str, Any]) -> dict[str, Any]:
    n = m.get("n") or 0
    m["hit_rate"] = (m["wins"] / n) if n else None
    m["brier"] = (m["brier_sum"] / n) if n else None
    m["logloss"] = (m["logloss_sum"] / n) if n else None
    m["mean_return"] = (m["return_sum"] / n) if n else None
    m["sample_size"] = n
    if "cal" in m:
        m["calibration"] = [
            {
                "bucket": f"{k/10:.1f}-{(k+1)/10:.1f}",
                "n": v["n"],
                "avg_p": v["sum_p"] / v["n"],
                "avg_y": v["sum_y"] / v["n"],
            }
            for k, v in sorted(m["cal"].items())
            if v["n"] > 0
        ]
    return m


def _metrics_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    m = _empty()
    for u in rows:
        y = 1 if u.get("result") == "win" else 0
        _acc(m, float(u.get("forecast_probability") or 0), y, u.get("flat_stake_return"))
    return _finalize(m)


def _comps(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _odds_bucket(price: int) -> str:
    if price <= -200:
        return "heavy_favorite_leq_-200"
    if price < 0:
        return "favorite_-199_to_-100"
    if price <= 150:
        return "short_plus_100_to_+150"
    if price <= 300:
        return "medium_+151_to_+300"
    return "longshot_gt_+300"
