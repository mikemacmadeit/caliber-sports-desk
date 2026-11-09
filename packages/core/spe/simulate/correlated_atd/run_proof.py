"""Prove Anytime TD accuracy milestone — no new models, no promotion."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from spe.simulate.correlated_atd.paired_compare import compare_league, league_selection
from spe.simulate.correlated_atd.proof_isolation import (
    assert_protected_unchanged,
    banner,
    development_database_url,
    report_path,
    snapshot_protected,
)
from spe.simulate.correlated_atd.sample_audit import (
    LABEL_HISTORICAL_REPLAY,
    _load_phase5_role_games,
    audit_phase5_prospective,
)
from spe.simulate.correlated_atd.scoring import load_league_frame
from spe.simulate.correlated_atd.shadow_candidates import capture_and_settle_candidates
from spe.simulate.correlated_atd.sportsbook_settled import sportsbook_performance


def _session() -> Session:
    return sessionmaker(bind=create_engine(development_database_url(), future=True), autoflush=False, autocommit=False)()


def run_proof_accuracy(
    *,
    skip_paired: bool = False,
    skip_shadow: bool = False,
    skip_sportsbook: bool = False,
    draw_count: int = 1_200,
    n_boot: int = 800,
    shadow_limit: int = 8,
) -> dict[str, Any]:
    before = snapshot_protected()
    report: dict[str, Any] = {
        "phase": "prove_atd_accuracy",
        "title": "Prove Anytime TD Prediction Accuracy",
        "generated_at": datetime.now(UTC).isoformat(),
        "safety": banner(),
        "play_enabled": False,
        "promoted": False,
        "validated_play": False,
        "develop_new_models": False,
        "promotion_decision": "DO_NOT_PROMOTE",
    }

    session = _session()
    try:
        print("[proof] auditing Phase 5 prospective provenance…", flush=True)
        audit = audit_phase5_prospective(session)
        report["sample_provenance"] = {
            "verdict": audit.get("verdict"),
            "definition": audit.get("definition"),
            "leagues": {
                lg: {
                    "n_games": body.get("n_games"),
                    "majority_label": body.get("majority_label"),
                    "label_counts": body.get("label_counts"),
                    "is_live_prospective": body.get("is_live_prospective"),
                    "accurate_description": body.get("accurate_description"),
                }
                for lg, body in (audit.get("leagues") or {}).items()
            },
            "live_shadow_universe": audit.get("live_shadow_universe"),
            "artifact_path": audit.get("artifact_path"),
        }

        frames = {}
        paired: dict[str, Any] = {}
        if not skip_paired:
            print("[proof] loading frames for paired comparison…", flush=True)
            for lg in ("nfl", "fbs"):
                frames[lg] = load_league_frame(session, lg)
            for lg in ("nfl", "fbs"):
                gids = _load_phase5_role_games(lg, "prospective")
                paired[lg] = compare_league(
                    session,
                    lg,
                    gids,
                    sample_label="phase5_prospective_holdout",
                    evidence_class=LABEL_HISTORICAL_REPLAY,
                    draw_count=draw_count,
                    n_boot=n_boot,
                    frame=frames.get(lg),
                )
            report["paired_comparisons"] = paired
            report["league_specific_selection"] = league_selection(paired.get("nfl") or {}, paired.get("fbs") or {})

        if not skip_shadow:
            print("[proof] freezing live candidate shadows (M3/M5/M6/sim)…", flush=True)
            shadow = capture_and_settle_candidates(session, limit_per_league=shadow_limit)
            session.commit()
            report["live_shadow"] = shadow
        else:
            session.rollback()

        if not skip_sportsbook:
            print("[proof] sportsbook performance (settled pre-kickoff only)…", flush=True)
            report["sportsbook_performance"] = sportsbook_performance(session)

    finally:
        session.close()

    report["summary"] = _summarize(report)
    report["remaining_blockers"] = [
        "Phase 5 'prospective' games are historical chronological holdout replays — not live proof.",
        "Genuine live proof requires pre-kickoff shadow freezes that later settle (currently sparse/zero for new candidates).",
        "NFL atd_sim_v1 vs M6 needs repeatable paired CI excluding 0 across more settled weeks.",
        "FBS: keep M5 as research leader unless overturned; do not force simulator architecture.",
        "Joint/parlay: no demonstrated paired improvement vs independence yet.",
        "Yes-only markets: no-vig unidentified; no profitability claims.",
        "Validated Play disabled; no auto-promotion; no new models in this phase.",
    ]
    report["recommendation"] = {
        "promote": False,
        "enable_validated_play": False,
        "build_m7": False,
        "focus": "Accumulate genuine pre-kickoff shadows → settle → paired league-specific ranking on live evidence",
        "product_decision": "NFL and FBS may use different Anytime TD research leaders.",
    }

    assert_protected_unchanged(before)
    report["protected_artifacts_unchanged"] = True
    path = report_path()
    # Compact disk copy: drop bulky per-game audit lists already written elsewhere.
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    print(f"[proof] wrote {path}", flush=True)
    return report


def _summarize(report: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "phase5_prospective_label": (report.get("sample_provenance") or {}).get("verdict"),
        "nfl_ranking": ((report.get("league_specific_selection") or {}).get("nfl") or {}),
        "fbs_ranking": ((report.get("league_specific_selection") or {}).get("fbs") or {}),
        "paired": {},
        "live_shadow_written": ((report.get("live_shadow") or {}).get("capture") or {}).get("written"),
        "live_shadow_settled": ((report.get("live_shadow") or {}).get("settle") or {}).get("settled"),
        "sportsbook_measurable": ((report.get("sportsbook_performance") or {}).get("measurable")),
        "sportsbook_n_settled": ((report.get("sportsbook_performance") or {}).get("n_eligible_settled")),
    }
    for lg, body in (report.get("paired_comparisons") or {}).items():
        out["paired"][lg] = {
            "evidence_class": body.get("evidence_class"),
            "n": body.get("n"),
            "n_games": body.get("n_games"),
            "ranking": body.get("ranking_by_brier"),
            "sim_minus_m6_brier": ((body.get("paired_deltas") or {}).get("atd_sim_v1__minus__m6") or {}).get("brier"),
            "m5_minus_m6_brier": ((body.get("paired_deltas") or {}).get("m6__minus__m5") or {}).get("brier"),
            "joint_sim_minus_indep": (body.get("joint") or {}).get("paired_brier_sim_minus_indep"),
        }
    return out
