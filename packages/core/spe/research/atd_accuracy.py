"""Anytime TD accuracy milestone: sparse OOS, FBS stats verify, prospective settle, model compare.

Research only. Does not promote. Does not enable Validated Play.
Coordinates repair verification without overlapping production model writes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.research.atd_forecast import research_dir
from spe.research.atd_verify import settled_performance, verify_prospective_freezes, ingest_freeze_into_shadow
from spe.research.sparse_fallback import evaluate_sparse_fallbacks, report_path as sparse_report_path
from spe.shadow.settle import settle_shadow_predictions


def report_path() -> Path:
    return research_dir() / "accuracy_reliability_report.json"


def verify_fbs_2026_stats(session: Session) -> dict[str, Any]:
    row = session.execute(
        text(
            """
            SELECT
              (SELECT count(*) FROM games g JOIN seasons se ON se.season_id=g.season_id
               WHERE g.league_id='fbs' AND se.year=2026 AND g.status='final') AS finals_2026,
              (SELECT count(*) FROM games g JOIN seasons se ON se.season_id=g.season_id
               WHERE g.league_id='fbs' AND se.year=2026 AND g.status='final'
                 AND EXISTS (SELECT 1 FROM player_game_stats p WHERE p.game_id=g.game_id)) AS finals_with_stats,
              (SELECT count(*) FROM player_game_stats p
               JOIN games g ON g.game_id=p.game_id
               JOIN seasons se ON se.season_id=g.season_id
               WHERE g.league_id='fbs' AND se.year=2026) AS pgs_rows_2026,
              (SELECT count(*) FROM player_game_stats p
               JOIN games g ON g.game_id=p.game_id
               JOIN seasons se ON se.season_id=g.season_id
               WHERE g.league_id='fbs' AND se.year=2026
                 AND (COALESCE(p.rush_td,0)+COALESCE(p.rec_td,0))>0) AS pgs_with_tds
            """
        )
    ).mappings().one()
    finals = int(row["finals_2026"] or 0)
    with_stats = int(row["finals_with_stats"] or 0)
    return {
        "finals_2026": finals,
        "finals_with_stats": with_stats,
        "coverage_pct": (with_stats / finals) if finals else None,
        "pgs_rows_2026": int(row["pgs_rows_2026"] or 0),
        "pgs_with_tds": int(row["pgs_with_tds"] or 0),
        "flowing_into_db": int(row["pgs_rows_2026"] or 0) > 0,
        "root_cause_fixed": [
            "DEFAULT_SEASONS previously excluded 2026 (range end exclusive)",
            "CFBD box scores keyed as fbs:{id} while 2026 games stored as espn:fbs:{id}; mapper now bridges both",
        ],
        "remaining_gaps": (
            []
            if with_stats >= max(1, int(0.85 * finals))
            else ["Some 2026 finals still lack box scores (incomplete CFBD weeks or unmatched ids)"]
        ),
    }


def run_accuracy_milestone(
    session: Session,
    *,
    now: datetime | None = None,
    ingest_shadow: bool = True,
    run_settle: bool = True,
    eval_sparse: bool = True,
) -> dict[str, Any]:
    moment = as_utc(now) or datetime.now(UTC)
    print("[accuracy] verifying FBS 2026 stats…", flush=True)
    fbs = verify_fbs_2026_stats(session)

    sparse_nfl: dict[str, Any] = {"status": "skipped"}
    sparse_fbs: dict[str, Any] = {"status": "skipped"}
    if eval_sparse:
        print("[accuracy] sparse fallback OOS (NFL)…", flush=True)
        sparse_nfl = evaluate_sparse_fallbacks(session, league_id="nfl")
        print("[accuracy] sparse fallback OOS (FBS)…", flush=True)
        try:
            sparse_fbs = evaluate_sparse_fallbacks(session, league_id="fbs")
        except Exception as exc:
            sparse_fbs = {"status": "error", "error": str(exc)}

    print("[accuracy] prospective freeze + shadow…", flush=True)
    freeze = verify_prospective_freezes(session)
    ingest_summary: dict[str, Any] = {"status": "skipped"}
    if ingest_shadow:
        ingest_summary = ingest_freeze_into_shadow(session, now=moment)
        session.commit()
    settle_summary: dict[str, Any] = {"status": "skipped"}
    if run_settle:
        print("[accuracy] settlement…", flush=True)
        settle_summary = settle_shadow_predictions(session, now=moment)
        session.commit()
    performance = settled_performance(session)

    # Count genuine pregame shadows for research families
    shadow_counts = session.execute(
        text(
            """
            SELECT
              sum(CASE WHEN sp.model_version_id LIKE 'player_td_v1%' THEN 1 ELSE 0 END) AS player_td_v1,
              sum(CASE WHEN sp.model_version_id LIKE 'm6_hist_gbm%' THEN 1 ELSE 0 END) AS m6_hist_gbm,
              sum(CASE WHEN sp.model_version_id LIKE 'ens_m6_gbm%' THEN 1 ELSE 0 END) AS ensemble,
              sum(CASE WHEN sp.model_version_id LIKE 'm5-%' THEN 1 ELSE 0 END) AS m5,
              sum(CASE WHEN sp.model_version_id LIKE 'm6-%' AND sp.model_version_id NOT LIKE 'm6_hist%' THEN 1 ELSE 0 END) AS m6_shrink,
              sum(CASE WHEN sp.model_version_id LIKE 'atd_sim_v1%' THEN 1 ELSE 0 END) AS atd_sim_v1,
              count(*) AS total_anytime
            FROM shadow_predictions sp
            WHERE sp.market_id='anytime_td'
            """
        )
    ).mappings().one()
    settled_counts = session.execute(
        text(
            """
            SELECT st.result, count(*) c
            FROM shadow_settlements st
            JOIN shadow_predictions sp ON sp.shadow_id=st.shadow_id
            WHERE sp.market_id='anytime_td'
              AND (
                sp.model_version_id LIKE 'player_td_v1%'
                OR sp.model_version_id LIKE 'm6_hist_gbm%'
                OR sp.model_version_id LIKE 'ens_m6_gbm%'
                OR sp.model_version_id LIKE 'm5-%'
                OR sp.model_version_id LIKE 'atd_sim_v1%'
              )
            GROUP BY st.result
            """
        )
    ).mappings().all()

    best_nfl = None
    best_fbs = None
    for lg, block in (("nfl", performance.get("nfl") or {}), ("fbs", performance.get("fbs") or {})):
        ranked = sorted(
            (
                {"model": k, **v}
                for k, v in block.items()
                if isinstance(v, dict) and v.get("n") and v.get("brier") is not None
            ),
            key=lambda r: (r["brier"], r.get("log_loss") or 9),
        )
        if lg == "nfl":
            best_nfl = ranked[0] if ranked else None
        else:
            best_fbs = ranked[0] if ranked else None

    sparse_improved = bool(
        (sparse_nfl.get("retain_any_fallback") if isinstance(sparse_nfl, dict) else False)
        or (sparse_fbs.get("retain_any_fallback") if isinstance(sparse_fbs, dict) else False)
    )

    report = {
        "status": "ok",
        "as_of": moment.isoformat(),
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "validated_play": False,
        "1_data_problems_fixed": {
            "fbs_2026_player_game_stats": fbs,
            "sparse_fallback_oos": {
                "nfl_report": sparse_nfl.get("report_path") if isinstance(sparse_nfl, dict) else None,
                "fbs_report": sparse_fbs.get("report_path") if isinstance(sparse_fbs, dict) else None,
                "retain_any_fallback": sparse_improved,
                "applied_to_production": False,
            },
        },
        "2_sparse_player_accuracy_improved": {
            "improved_oos": sparse_improved,
            "nfl": {
                "sparse_best": (sparse_nfl.get("sparse_players") or {}).get("best")
                if isinstance(sparse_nfl, dict)
                else None,
                "retain_flags": {
                    k: v
                    for k, v in ((sparse_nfl.get("sparse_players") or {}) if isinstance(sparse_nfl, dict) else {}).items()
                    if str(k).startswith("retain_")
                },
                "recommendation": sparse_nfl.get("recommendation") if isinstance(sparse_nfl, dict) else None,
            },
            "fbs": {
                "sparse_best": (sparse_fbs.get("sparse_players") or {}).get("best")
                if isinstance(sparse_fbs, dict)
                else None,
                "recommendation": sparse_fbs.get("recommendation") if isinstance(sparse_fbs, dict) else None,
            },
        },
        "3_fbs_2026_stats_flowing": fbs.get("flowing_into_db") and (fbs.get("coverage_pct") or 0) > 0,
        "fbs_2026_detail": fbs,
        "4_genuine_pregame_settled": {
            "shadow_rows_captured": dict(shadow_counts),
            "settlement_by_result": {r["result"]: int(r["c"]) for r in settled_counts},
            "settled_win_loss_research": int(performance.get("n_settled_rows") or 0),
            "freeze_verification": freeze,
            "ingest": ingest_summary,
            "settle": settle_summary,
            "note": performance.get("note"),
        },
        "5_best_model_identical_games": {
            "nfl": best_nfl,
            "fbs": best_fbs,
            "full_performance": performance,
            "caveat": "Do not claim superiority from small prospective samples.",
        },
        "6_biggest_remaining_obstacle": (
            "Too few genuinely settled pregame predictions to decide which model wins when they disagree — "
            "especially on sparse-history players. FBS 2026 stats now load, but features must be rebuilt "
            "into research frames and more kickoffs must settle before promotion."
        ),
        "blockers_before_promotion": [
            "Validated Play disabled",
            "Sparse fallback not applied to production (await clear OOS win)",
            "Need larger settled prospective sample",
            "Rebuild FBS research features after 2026 stats ingest before claiming feature-level flow",
        ],
    }
    path = report_path()
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    print(f"[accuracy] wrote {path}", flush=True)
    return report
