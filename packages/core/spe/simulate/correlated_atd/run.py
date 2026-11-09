"""Phase 4 orchestration: validate → backtest → examples → report.

Research-only. Never enables Validated Play. Never writes live Anytime TD Finder rows.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from spe.simulate.correlated_atd.backtest import backtest_league
from spe.simulate.correlated_atd.examples import run_upcoming_examples
from spe.simulate.correlated_atd.isolation import (
    VERSION,
    assert_protected_unchanged,
    development_database_url,
    examples_path,
    report_path,
    safety_banner,
    snapshot_protected,
)
from spe.simulate.correlated_atd.validate import run_mechanics_suite


def _session() -> Session:
    url = development_database_url()
    engine = create_engine(url, future=True)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def run_phase4(
    *,
    skip_backtest: bool = False,
    skip_examples: bool = False,
    shadow_days: int = 45,
    max_games: int = 80,
    draw_count_backtest: int = 2_000,
    draw_count_examples: int = 5_000,
    example_limit: int = 2,
) -> dict[str, Any]:
    before = snapshot_protected()
    banner = safety_banner()
    report: dict[str, Any] = {
        "phase": 4,
        "title": "Advanced Anytime TD Simulation Engine",
        "generated_at": datetime.now(UTC).isoformat(),
        "safety": banner,
        "version": VERSION,
        "integration_architecture": {
            "team_provider": "TeamTdV1Provider → TeamScoringContext",
            "player_weights": "eligible roster + verified priors + M6 rates",
            "engine": "simulate_correlated_atd (team PMF/Poisson → rush/rec split → multinomial)",
            "artifacts": "data/parquet/models/atd_sim/ and data/atd_sim_dev/",
            "does_not_wire": "live Anytime TD Finder",
            "preserves": banner["preserves"],
        },
        "simulation_mechanics": {
            "team_tds": "Poisson(λ) or PMF buckets with 5+ tail",
            "rush_rec_split": "binomial with λ_rush / λ_off; mild game-script conversion",
            "allocation": "multinomial among live eligible skill players + OTHER residual",
            "qb": "rush TDs only; passing TDs allocated to receivers",
            "parlays": "joint probabilities counted on shared draws",
            "seeds": "numpy Generator with fixed seed → reproducible",
        },
    }

    print("[atd_sim] mechanics validation…", flush=True)
    report["mechanics_validation"] = run_mechanics_suite(draws=4_000, seed=11)

    session = _session()
    try:
        if not skip_backtest:
            print("[atd_sim] chronological backtests…", flush=True)
            report["backtests"] = {}
            for league in ("nfl", "fbs"):
                report["backtests"][league] = backtest_league(
                    session,
                    league,
                    shadow_days=shadow_days,
                    draw_count=draw_count_backtest,
                    max_games=max_games,
                )
        if not skip_examples:
            print("[atd_sim] upcoming game examples…", flush=True)
            examples = run_upcoming_examples(
                session,
                limit_per_league=example_limit,
                draw_count=draw_count_examples,
            )
            report["examples"] = examples
            examples_path().write_text(json.dumps(examples, indent=2, default=str), encoding="utf-8")
            report["examples_path"] = str(examples_path())
    finally:
        session.close()

    # Accuracy / calibration / comparison sections for the final report.
    bt = report.get("backtests") or {}
    report["nfl_accuracy"] = bt.get("nfl")
    report["fbs_accuracy"] = bt.get("fbs")
    report["probability_calibration"] = {
        lg: (body.get("atd_sim_v1") or {}).get("by_odds_proxy_bucket")
        for lg, body in bt.items()
        if isinstance(body, dict) and "atd_sim_v1" in body
    }
    report["m6_versus_integrated"] = {
        lg: body.get("sim_vs_m6")
        for lg, body in bt.items()
        if isinstance(body, dict)
    }
    report["remaining_weaknesses"] = [
        "Marginal Brier may lag M6 when team budgets compress star players.",
        "FBS opportunity features are thinner (no fabricated RZ/GL).",
        "Historical backtest uses frame-eligible skill rows; live path uses roster_weeks ACT.",
        "No individual CB/WR assignment modeling by design.",
        "Game-script conversion is intentionally mild and not play-by-play.",
        "Joint parlay calibration sample is sparse (top same-team pair proxy).",
        "Not yet connected to sportsbook EV / Anytime TD Finder.",
    ]
    report["recommendation_next_milestone"] = {
        "milestone": "Phase 5 — matchup refinement + EV shadow against sportsbooks",
        "focus": [
            "Improve position-path defense features with verified team-level signals only",
            "Prospective shadow: store sim probs beside M6 without enabling Play",
            "Expand joint-probability grading on settled parlays where books offer prices",
            "Promote only after sim mechanics stay green and NFL calibration matches or beats M6 on Brier+joint",
        ],
        "do_not": [
            "auto-promote",
            "enable Validated Play",
            "overwrite M3–M6 or team_td_v1 artifacts",
        ],
    }

    assert_protected_unchanged(before)
    report["protected_artifacts_unchanged"] = True
    path = report_path()
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    print(f"[atd_sim] wrote {path}", flush=True)
    return report
