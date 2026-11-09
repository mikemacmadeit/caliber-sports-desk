"""Scheduled collection mode: settle, Agent-2 pair reconciliation, material-update gate.

No retrain/promote/threshold changes. Validated Play stays off.
Does not reconstruct predictions after kickoff — uses freeze-file probs only.
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
from spe.shadow.accuracy_optimization import settle_all_eligible
from spe.shadow.jobs import job_atd_candidates, job_capture, job_settle
from spe.shadow.prospective_readiness import blocked_reconciliation

REPORT_DIR = Path("data") / "atd_sim_dev" / "live_weekend"
PAIRED_FREEZE = Path("data/research/atd_unified/m5_fbs_refit/prospective/latest_paired_freeze.json")
PRIOR_UNIQUE_PG = 42
PRIOR_BLOCKED_PG = 11


def _brier(p: float, y: int) -> float:
    return (float(p) - y) ** 2


def _logloss(p: float, y: int) -> float:
    eps = 1e-6
    p = min(max(float(p), eps), 1 - eps)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


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


def load_paired_freeze() -> dict[str, Any]:
    if not PAIRED_FREEZE.exists():
        return {}
    return json.loads(PAIRED_FREEZE.read_text(encoding="utf-8"))


def run_scheduled_capture_settle(session: Session) -> dict[str, Any]:
    """Existing capture → candidates → CFB board freeze → settle. No odds refresh."""
    steps = []
    steps.append(job_capture(session))
    session.commit()
    steps.append(job_atd_candidates(session))
    session.commit()
    # Freeze the live CFB board blend (what users see) for prospective grading.
    from spe.shadow.fbs_board_live_proof import (
        build_board_freeze,
        ingest_board_freeze_into_shadow,
    )

    freeze = build_board_freeze(session)
    board_ingest = ingest_board_freeze_into_shadow(session, freeze=freeze)
    session.commit()
    steps.append(
        {
            "job_name": "fbs_board_live_freeze",
            "status": "ok" if board_ingest.get("status") == "ok" else "failed",
            "summary": board_ingest,
            "error": None if board_ingest.get("status") == "ok" else board_ingest.get("status"),
        }
    )
    steps.append(job_settle(session))
    session.commit()
    return {
        "play_enabled": False,
        "validated_play": False,
        "odds_refresh": False,
        "odds_budget_unchanged": True,
        "steps": [
            {
                "job_name": s.get("job_name"),
                "status": s.get("status"),
                "summary": s.get("summary"),
                "error": s.get("error"),
            }
            for s in steps
        ],
    }


def settled_unique_counts(session: Session) -> dict[str, Any]:
    row = session.execute(
        text(
            """
            SELECT
              COUNT(DISTINCT s.player_id||'|'||s.game_id) AS player_games,
              COUNT(DISTINCT s.game_id) AS games
            FROM shadow_settlements st
            JOIN shadow_predictions s ON s.shadow_id=st.shadow_id
            JOIN games g ON g.game_id=s.game_id
            WHERE s.market_id='anytime_td'
              AND st.result IN ('win','loss')
              AND s.forecast_as_of < g.kickoff_utc
            """
        )
    ).mappings().first()
    pg = int((row or {}).get("player_games") or 0)
    games = int((row or {}).get("games") or 0)
    return {
        "unique_settled_player_games": pg,
        "unique_settled_games": games,
        "new_unique_player_games_since_prior": max(0, pg - PRIOR_UNIQUE_PG),
        "prior_unique_player_games": PRIOR_UNIQUE_PG,
    }


def reconcile_agent2_pairs(session: Session, freeze: dict[str, Any]) -> dict[str, Any]:
    """Account for all 821 frozen pairs; separate priced vs unpriced; grade from freeze probs."""
    if not freeze:
        return {"status": "missing_paired_freeze"}

    # Priced anytime_td outcomes currently in DB (for membership, not for reconstructing p)
    priced_keys = {
        (r["game_id"], r["player_id"])
        for r in session.execute(
            text(
                """
                SELECT DISTINCT o.game_id, o.player_id
                FROM outcomes o
                JOIN odds_ticks t ON t.outcome_id=o.outcome_id
                WHERE o.market_id='anytime_td' AND o.side IN ('yes','over')
                  AND o.player_id IS NOT NULL
                """
            )
        ).mappings().all()
    }

    # Shadow rows for market-priced analysis (freeze-tagged when present)
    shadow_by_pg: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in session.execute(
        text(
            """
            SELECT s.game_id, s.player_id, s.model_version_id, s.forecast_probability,
                   s.price_american, s.forecast_as_of, st.result, st.actual_value,
                   s.components, g.kickoff_utc, g.status
            FROM shadow_predictions s
            JOIN games g ON g.game_id=s.game_id
            LEFT JOIN shadow_settlements st ON st.shadow_id=s.shadow_id
            WHERE s.market_id='anytime_td' AND s.league_id='fbs'
              AND s.forecast_as_of < g.kickoff_utc
              AND (
                s.model_version_id LIKE 'm5_c_recent%'
                OR s.model_version_id = 'm5-fbs-anytime_td'
              )
            """
        )
    ).mappings().all():
        comps = _json_obj(r["components"])
        fam = "m5_c_recent" if "c_recent" in str(r["model_version_id"]) else "m5"
        # Prefer freeze-tagged rows; otherwise keep latest
        key = (str(r["game_id"]), str(r["player_id"]))
        prev = shadow_by_pg[key].get(fam)
        if prev is None or (comps.get("freeze_id") == freeze.get("freeze_id")):
            shadow_by_pg[key][fam] = {**dict(r), "components": comps, "family": fam}

    # Box scores for finals in freeze
    game_ids = [g["game_id"] for g in (freeze.get("games") or [])]
    stats: dict[tuple[str, str], float] = {}
    game_status: dict[str, Any] = {}
    if game_ids:
        from sqlalchemy import bindparam

        for r in session.execute(
            text(
                """
                SELECT game_id, status, kickoff_utc FROM games WHERE game_id IN :g
                """
            ).bindparams(bindparam("g", expanding=True)),
            {"g": game_ids},
        ).mappings().all():
            game_status[str(r["game_id"])] = dict(r)
        for r in session.execute(
            text(
                """
                SELECT game_id, player_id,
                       COALESCE(rush_td,0)+COALESCE(rec_td,0) AS atd
                FROM player_game_stats WHERE game_id IN :g
                """
            ).bindparams(bindparam("g", expanding=True)),
            {"g": game_ids},
        ).mappings().all():
            stats[(str(r["game_id"]), str(r["player_id"]))] = float(r["atd"] or 0)

    pairs_total = 0
    priced = 0
    unpriced = 0
    gradeable = []  # probability accuracy rows
    market_rows = []  # priced only
    pending = 0
    blocked_like = 0
    kicked_ungradeable = 0

    for g in freeze.get("games") or []:
        gid = str(g["game_id"])
        gst = game_status.get(gid) or {}
        status = str(gst.get("status") or "").lower()
        is_final = status in {"final", "post"}
        for pl in g.get("players") or []:
            if not pl.get("paired"):
                continue
            pairs_total += 1
            pid = str(pl["player_id"])
            key = (gid, pid)
            is_priced = key in priced_keys
            if is_priced:
                priced += 1
            else:
                unpriced += 1

            p_m5 = pl.get("p_m5")
            p_c = pl.get("p_c_recent")
            if p_m5 is None or p_c is None:
                continue

            # Never reconstruct — only freeze-file probs (already frozen pregame)
            if not is_final:
                pending += 1
                continue

            if key not in stats:
                blocked_like += 1
                kicked_ungradeable += 1
                continue

            y = 1 if stats[key] > 0 else 0
            row = {
                "game_id": gid,
                "player_id": pid,
                "player_name": pl.get("player_name"),
                "position_group": pl.get("position_group"),
                "priced": is_priced,
                "y": y,
                "p_m5": float(p_m5),
                "p_c_recent": float(p_c),
                "source": "paired_freeze_file",
                "freeze_id": freeze.get("freeze_id"),
            }
            gradeable.append(row)

            if is_priced:
                sh = shadow_by_pg.get(key) or {}
                # Market analysis only with qualifying frozen shadow price
                price = None
                for fam in ("m5_c_recent", "m5"):
                    if fam in sh and sh[fam].get("price_american") is not None:
                        price = int(sh[fam]["price_american"])
                        break
                if price is not None:
                    market_rows.append({**row, "price_american": price, "market_implied_vigged": american_to_implied(price)})

    def _acc(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
        if not rows:
            return {"n": 0}
        b = ll = 0.0
        wins = 0
        sum_p = 0.0
        for r in rows:
            p = float(r[field])
            y = int(r["y"])
            b += _brier(p, y)
            ll += _logloss(p, y)
            wins += y
            sum_p += p
        n = len(rows)
        return {
            "n": n,
            "brier": b / n,
            "logloss": ll / n,
            "hit_rate": wins / n,
            "mean_p": sum_p / n,
            "base_rate": wins / n,
            "calibration_gap": (sum_p / n) - (wins / n),
        }

    gradeable_priced = [r for r in gradeable if r["priced"]]
    gradeable_unpriced = [r for r in gradeable if not r["priced"]]

    return {
        "freeze_id": freeze.get("freeze_id"),
        "frozen_at": freeze.get("frozen_at"),
        "locked_version": "fbs_m5_c_recent_locked_v1",
        "pipeline_version": "fbs_m5_research_refit_v1+recent8_prior_td_rate",
        "accounting": {
            "n_paired_declared": freeze.get("n_paired_player_outcomes"),
            "n_paired_walked": pairs_total,
            "priced_pairs": priced,
            "unpriced_pairs": unpriced,
            "priced_plus_unpriced": priced + unpriced,
            "matches_821": pairs_total == int(freeze.get("n_paired_player_outcomes") or -1),
            "expected_priced_unpriced_split": {"priced": 231, "unpriced": 590, "note": "from prior ingest summary"},
        },
        "grading_status": {
            "pending_not_final": pending,
            "final_missing_box_not_assumed_zero": blocked_like,
            "gradeable_total": len(gradeable),
            "gradeable_priced": len(gradeable_priced),
            "gradeable_unpriced": len(gradeable_unpriced),
        },
        "probability_accuracy": {
            "note": "All validly gradeable freeze pairs; probs from freeze file only (no post-kickoff reconstruction)",
            "all_gradeable": {
                "m5": _acc(gradeable, "p_m5"),
                "m5_c_recent": _acc(gradeable, "p_c_recent"),
            },
            "priced_only": {
                "m5": _acc(gradeable_priced, "p_m5"),
                "m5_c_recent": _acc(gradeable_priced, "p_c_recent"),
            },
            "unpriced_only": {
                "m5": _acc(gradeable_unpriced, "p_m5"),
                "m5_c_recent": _acc(gradeable_unpriced, "p_c_recent"),
            },
        },
        "market_return_analysis": {
            "note": "Restricted to qualifying frozen sportsbook prices only; yes-only implied is vigged",
            "n_with_frozen_price": len(market_rows),
            "market_vigged": _acc(
                [{"y": r["y"], "p_m5": r["market_implied_vigged"]} for r in market_rows],
                "p_m5",
            )
            if market_rows
            else {"n": 0},
            "label": "yes_only_vigged_implied",
        },
        "cohort_separation": {
            "legacy_m3_m4": "legacy_pre_version_block — evaluate separately",
            "newer_nfl_families": "separate from FBS C_recent",
            "fbs_m5_c_recent": "locked paired cohort only",
        },
        "play_enabled": False,
        "do_not_promote": True,
    }


def blocked_status(session: Session) -> dict[str, Any]:
    rec = blocked_reconciliation(session)
    return {
        "blocked_player_games": rec.get("blocked_unique_player_games"),
        "blocked_family_rows": rec.get("blocked_family_rows"),
        "blocked_games": rec.get("blocked_unique_games"),
        "prior_blocked_player_games": PRIOR_BLOCKED_PG,
        "changed": int(rec.get("blocked_unique_player_games") or 0) != PRIOR_BLOCKED_PG,
        "policy": "remain blocked until qualifying new evidence; no blind reingest; no zero/DNP assumption",
    }


def run_prospective_collection(session: Session) -> dict[str, Any]:
    print("[collect] scheduled capture+settle (no odds)…", flush=True)
    cycle = run_scheduled_capture_settle(session)
    # Idempotent settle pass for any blocked→terminal upgrades
    settle_all_eligible(session)
    session.commit()

    print("[collect] settled unique counts…", flush=True)
    settled = settled_unique_counts(session)
    print("[collect] Agent2 821-pair reconciliation…", flush=True)
    freeze = load_paired_freeze()
    pairs = reconcile_agent2_pairs(session, freeze)
    print("[collect] blocked status…", flush=True)
    blocked = blocked_status(session)

    failures: list[dict[str, Any]] = []
    for step in cycle.get("steps") or []:
        if step.get("status") == "failed":
            failures.append({"type": "scheduling_or_job_failure", "job": step.get("job_name"), "error": step.get("error")})

    acct = (pairs.get("accounting") or {})
    if freeze and not acct.get("matches_821"):
        failures.append(
            {
                "type": "capture_or_freeze_accounting",
                "detail": f"walked {acct.get('n_paired_walked')} != declared {acct.get('n_paired_declared')}",
            }
        )

    # Handoff reconciliation completion is operationally material once; subsequent identical runs are not.
    handoff_path = REPORT_DIR / "agent2_pair_reconciliation.json"
    first_handoff = not handoff_path.exists()
    handoff_path.write_text(json.dumps(pairs, indent=2, default=str), encoding="utf-8")

    evidence_material = settled["new_unique_player_games_since_prior"] > 0
    operational_material = first_handoff or bool(failures) or bool(blocked.get("changed"))

    if evidence_material:
        status = "new_evidence"
        lead = (
            f"+{settled['new_unique_player_games_since_prior']} unique player-games / "
            f"settled games now {settled['unique_settled_games']}"
        )
    elif operational_material:
        status = "operational_update_no_new_evidence"
        lead = (
            "no new prospective evidence; Agent 2 pair reconciliation recorded"
            if first_handoff
            else "no new prospective evidence; operational change recorded"
        )
    else:
        status = "no_material_update"
        lead = "no material update"

    report = {
        "title": "Anytime TD Scheduled Collection Status",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": status,
        "lead": lead,
        "play_enabled": False,
        "validated_play": False,
        "retrained": False,
        "promoted": False,
        "overwrote_freezes": False,
        "models_unchanged": True,
        "odds_budget_unchanged": True,
        "cycle": cycle,
        "settled": settled,
        "blocked": blocked,
        "agent2_pair_reconciliation": {
            "path": str(handoff_path),
            "accounting": pairs.get("accounting"),
            "grading_status": pairs.get("grading_status"),
            "probability_accuracy_n": (pairs.get("probability_accuracy") or {}).get("all_gradeable"),
            "market_n": (pairs.get("market_return_analysis") or {}).get("n_with_frozen_price"),
            "first_reconciliation_write": first_handoff,
        },
        "failures_to_escalate": failures,
        "scheduled_workflow": {
            "cycle": 'powershell -File "C:\\dev\\Sports bet\\scripts\\shadow_schedule.ps1" -Job cycle',
            "collect_no_odds": (
                'cd "c:\\dev\\Sports bet"; '
                '$env:PYTHONPATH="packages\\core;services\\worker"; '
                ".venv\\Scripts\\python.exe services\\worker\\worker\\main.py atd-prospective-collection"
            ),
        },
    }
    # Full pair metrics only when first reconciliation or new evidence — avoid repeat accuracy dumps
    if first_handoff or evidence_material:
        report["agent2_pair_reconciliation_full"] = pairs
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / "prospective_collection_status.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    return report
