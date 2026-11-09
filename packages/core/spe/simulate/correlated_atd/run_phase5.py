"""Phase 5 orchestration: expand validation, FBS diagnosis, matchups, shadow, sportsbooks."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from spe.shadow.settle import settle_shadow_predictions
from spe.simulate.correlated_atd.expanded_validation import merge_examined, validate_league
from spe.simulate.correlated_atd.fbs_diagnosis import run_fbs_diagnosis
from spe.simulate.correlated_atd.matchup_ablation import run_matchup_ablation
from spe.simulate.correlated_atd.phase5_isolation import (
    assert_protected_unchanged,
    development_database_url,
    phase5_banner,
    report_path,
    snapshot_protected,
)
from spe.simulate.correlated_atd.scoring import load_league_frame
from spe.simulate.correlated_atd.shadow_atd import capture_atd_sim_shadow
from spe.simulate.correlated_atd.sportsbook_compare import compare_sportsbooks
from spe.simulate.correlated_atd.validate import run_mechanics_suite


def _session() -> Session:
    engine = create_engine(development_database_url(), future=True)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def run_phase5(
    *,
    skip_expanded: bool = False,
    skip_fbs: bool = False,
    skip_matchup: bool = False,
    skip_shadow: bool = False,
    skip_sportsbook: bool = False,
    examined_games: int = 100,
    prospective_games: int = 60,
    draw_count: int = 1_500,
    shadow_limit_per_league: int | None = 6,
) -> dict[str, Any]:
    before = snapshot_protected()
    report: dict[str, Any] = {
        "phase": 5,
        "title": "Anytime TD Validation",
        "generated_at": datetime.now(UTC).isoformat(),
        "safety": phase5_banner(),
        "play_enabled": False,
        "promoted": False,
        "validated_play": False,
        "research_only": True,
        "promotion_decision": "DO_NOT_PROMOTE",
    }

    print("[phase5] mechanics sanity…", flush=True)
    report["mechanics_validation"] = run_mechanics_suite(draws=3_000, seed=5)

    session = _session()
    frames: dict[str, Any] = {}
    try:
        if not skip_expanded or not skip_fbs or not skip_matchup:
            for lg in ("nfl", "fbs"):
                frames[lg] = load_league_frame(session, lg)

        if not skip_expanded:
            print("[phase5] expanded chronological validation…", flush=True)
            report["expanded_validation"] = {}
            examined_ids: dict[str, list[str]] = {}
            for lg in ("nfl", "fbs"):
                block = validate_league(
                    session,
                    lg,
                    examined_games=examined_games,
                    prospective_games=prospective_games,
                    draw_count=draw_count,
                    frame=frames.get(lg),
                )
                report["expanded_validation"][lg] = block
                examined_ids[lg] = list(block.get("examined_game_ids") or [])
            merge_examined(examined_ids.get("nfl") or [], examined_ids.get("fbs") or [])

        if not skip_fbs:
            print("[phase5] FBS calibration diagnosis…", flush=True)
            report["fbs_diagnosis"] = run_fbs_diagnosis(
                session,
                test_games=max(40, prospective_games),
                train_games=max(60, examined_games),
                draw_count=draw_count,
                frame=frames.get("fbs"),
            )

        if not skip_matchup:
            print("[phase5] matchup ablation…", flush=True)
            report["matchup_ablation"] = run_matchup_ablation(
                session,
                test_games=max(50, prospective_games),
                draw_count=draw_count,
                frames=frames,
            )

        if not skip_shadow:
            print("[phase5] prospective shadow capture (M6 + atd_sim)…", flush=True)
            cap = capture_atd_sim_shadow(session, limit_per_league=shadow_limit_per_league)
            print("[phase5] settle open shadow rows…", flush=True)
            settled = settle_shadow_predictions(session)
            session.commit()
            report["shadow"] = {
                "capture": cap,
                "settle": settled,
                "note": "Append-only; existing M3/M4 shadow rows untouched. All recommendations no_bet.",
            }
        else:
            session.rollback()

        if not skip_sportsbook:
            print("[phase5] sportsbook comparison…", flush=True)
            # Fresh session view after commit.
            report["sportsbook_comparison"] = compare_sportsbooks(session)

    finally:
        session.close()

    report["summary"] = _summarize(report)
    report["remaining_blockers_before_promotion"] = [
        "Prospective sample must show consistent NFL sim ≥ M6 on Brier + log loss with non-overlapping CI preference.",
        "FBS calibration must improve or remain explicitly research-only / not promoted.",
        "Joint/parlay probabilities need larger settled sample separate from marginal ATD.",
        "Shadow settlements for atd_sim_v1/m6 must accumulate real results before EV claims.",
        "Validated Play stays disabled; Anytime TD Finder stays unwired to sim.",
        "No automatic promotion.",
    ]
    report["recommendation"] = {
        "promote": False,
        "enable_validated_play": False,
        "next": (
            "Continue prospective shadow weekly; expand NFL prospective games; "
            "apply only retained FBS/matchup changes after OOS confirmation."
        ),
    }

    assert_protected_unchanged(before)
    report["protected_artifacts_unchanged"] = True
    path = report_path()
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    print(f"[phase5] wrote {path}", flush=True)
    return report


def _summarize(report: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for lg, body in (report.get("expanded_validation") or {}).items():
        roles = body.get("roles") or {}
        out[lg] = {}
        for role in ("prospective", "expanded_all", "examined"):
            block = roles.get(role) or {}
            models = block.get("models") or {}
            if not models:
                continue
            out[lg][role] = {
                "n": block.get("n"),
                "n_games": block.get("n_games"),
                "sim_brier": (models.get("atd_sim_v1") or {}).get("brier"),
                "m6_brier": (models.get("m6") or {}).get("brier"),
                "sim_vs_m6": (block.get("sim_vs_m6") or {}).get("brier_delta"),
                "joint_sim_minus_indep": ((block.get("joint_same_team") or {}).get("sim_minus_indep_brier")),
            }
    fbs = report.get("fbs_diagnosis") or {}
    out["fbs_retained_experiments"] = fbs.get("retained")
    out["fbs_findings"] = fbs.get("findings")
    mu = report.get("matchup_ablation") or {}
    out["matchup_retained"] = {
        lg: (body.get("retained") or []) for lg, body in (mu.get("leagues") or {}).items()
    }
    sh = report.get("shadow") or {}
    out["shadow_written"] = (sh.get("capture") or {}).get("written")
    out["shadow_settled"] = (sh.get("settle") or {}).get("settled")
    return out
