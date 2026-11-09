"""Mechanics validation for the correlated Anytime TD simulator."""

from __future__ import annotations

from typing import Any

import numpy as np

from spe.models.player_td.integration import TeamScoringContext
from spe.simulate.correlated_atd.engine import CorrelatedAtdResult, simulate_correlated_atd
from spe.simulate.correlated_atd.player_weights import PlayerOpportunity


def synthetic_fixture() -> tuple[
    list[PlayerOpportunity],
    dict[str, TeamScoringContext],
    str,
    str,
]:
    home, away = "team_home", "team_away"
    # Concentrated shares so teammate competition is measurable vs independence.
    opps = [
        PlayerOpportunity("qb1", home, "Home QB", "QB", "eligible", 1.0, 0.08, 0.0, 0.08, 0.08, 0.05, 4.0, 0.0, 0.2, 0.0, 0.9, ["synth"]),
        PlayerOpportunity("rb1", home, "Home RB", "RB", "eligible", 1.0, 0.55, 0.12, 0.28, 0.35, 0.25, 14.0, 3.0, 1.5, 0.4, 0.7, ["synth"]),
        PlayerOpportunity("wr1", home, "Home WR", "WR", "eligible", 1.0, 0.02, 0.45, 0.18, 0.20, 0.12, 0.5, 7.0, 0.0, 1.2, 0.8, ["synth"]),
        PlayerOpportunity("te1", home, "Home TE", "TE", "eligible", 1.0, 0.01, 0.15, 0.12, 0.14, 0.08, 0.2, 4.0, 0.1, 0.8, 0.65, ["synth"]),
        PlayerOpportunity("rb2", away, "Away RB", "RB", "eligible", 1.0, 0.55, 0.10, 0.22, 0.28, 0.18, 12.0, 2.0, 1.2, 0.3, 0.75, ["synth"]),
        PlayerOpportunity("wr2", away, "Away WR", "WR", "eligible", 1.0, 0.02, 0.45, 0.16, 0.18, 0.10, 0.4, 8.0, 0.0, 1.0, 0.85, ["synth"]),
        PlayerOpportunity("qb2", away, "Away QB", "QB", "eligible", 1.0, 0.08, 0.0, 0.06, 0.06, 0.04, 3.0, 0.0, 0.1, 0.0, 0.95, ["synth"]),
    ]
    # Renormalize per team with other=0.08
    other = 0.08
    for tid in (home, away):
        rows = [o for o in opps if o.team_id == tid]
        rs = sum(o.rush_weight for o in rows) or 1.0
        cs = sum(o.rec_weight for o in rows) or 1.0
        for o in rows:
            o.rush_weight = (1 - other) * o.rush_weight / rs
            o.rec_weight = 0.0 if o.position_group == "QB" else (1 - other) * o.rec_weight / cs
    ctx = {
        home: TeamScoringContext(
            game_id="g1",
            team_id=home,
            league_id="nfl",
            expected_off_tds=3.4,
            expected_rush_tds=1.4,
            expected_rec_tds=2.0,
            off_td_pmf=(0.04, 0.12, 0.22, 0.26, 0.20, 0.16),
            model_version="synth",
            research_only=True,
        ),
        away: TeamScoringContext(
            game_id="g1",
            team_id=away,
            league_id="nfl",
            expected_off_tds=3.0,
            expected_rush_tds=1.2,
            expected_rec_tds=1.8,
            off_td_pmf=(0.05, 0.14, 0.24, 0.26, 0.18, 0.13),
            model_version="synth",
            research_only=True,
        ),
    }
    return opps, ctx, home, away


def run_mechanics_suite(*, draws: int = 5_000, seed: int = 7) -> dict[str, Any]:
    opps, ctx, home, away = synthetic_fixture()
    result = simulate_correlated_atd(
        "nfl",
        "g1",
        home,
        away,
        opps,
        ctx,
        draw_count=draws,
        seed=seed,
        other_share=0.08,
    )
    # Reproducibility
    result2 = simulate_correlated_atd(
        "nfl",
        "g1",
        home,
        away,
        opps,
        ctx,
        draw_count=draws,
        seed=seed,
        other_share=0.08,
    )
    probs1 = {p.player_id: p.p_anytime_td for p in result.players}
    probs2 = {p.player_id: p.p_anytime_td for p in result2.players}
    reproducible = probs1 == probs2

    # Convergence: larger draw count should shrink SE
    result_hi = simulate_correlated_atd(
        "nfl", "g1", home, away, opps, ctx, draw_count=draws * 2, seed=seed, other_share=0.08
    )
    se_ok = result_hi.convergence["max_td_mc_se"] <= result.convergence["max_td_mc_se"] + 1e-9

    # Teammate joint ≠ independent product
    rb = next(p for p in result.players if p.player_id == "rb1")
    wr = next(p for p in result.players if p.player_id == "wr1")
    joint_key = "rb1|wr1"
    joint = result.joint_same_team.get(joint_key) or result.joint_td.get(joint_key)
    independent = rb.p_anytime_td * wr.p_anytime_td
    correlated = joint is not None and abs(joint - independent) > 0.001

    # QB has rush path, zero receiving weight
    qb = next(p for p in result.players if p.player_id == "qb1")
    qb_ok = qb.p_rec_td == 0.0 or qb.rec_weight == 0.0

    checks = {
        "team_equals_allocated_plus_other": result.mechanics["team_equals_allocated_plus_other"],
        "qb_rec_weight_all_zero": result.mechanics["qb_rec_weight_all_zero"],
        "prob_in_unit_interval": result.mechanics["prob_in_unit_interval"],
        "reproducible_fixed_seed": reproducible,
        "se_shrinks_with_draws": se_ok,
        "teammate_joint_not_independent": correlated,
        "qb_no_receiving_td_path": qb_ok,
        "joint_rb_wr": joint,
        "independent_product_rb_wr": independent,
    }
    checks["all_passed"] = all(
        bool(checks[k])
        for k in (
            "team_equals_allocated_plus_other",
            "qb_rec_weight_all_zero",
            "prob_in_unit_interval",
            "reproducible_fixed_seed",
            "se_shrinks_with_draws",
            "teammate_joint_not_independent",
            "qb_no_receiving_td_path",
        )
    )
    return {
        "checks": checks,
        "convergence": result.convergence,
        "sample_top": result.to_dict(top_n=5)["top_scorers"],
        "team_expected": result.team_expected,
    }


def compare_to_m6(result: CorrelatedAtdResult) -> dict[str, Any]:
    rows = []
    for p in result.players:
        if p.m6_p is None:
            continue
        rows.append(
            {
                "player_id": p.player_id,
                "name": p.name,
                "position": p.position_group,
                "sim_p": p.p_anytime_td,
                "m6_p": p.m6_p,
                "delta": p.delta_vs_m6,
                "mean_tds": p.mean_tds,
            }
        )
    if not rows:
        return {"n": 0, "note": "no_m6_overlap"}
    deltas = np.array([r["delta"] for r in rows], dtype=float)
    return {
        "n": len(rows),
        "mean_abs_delta": float(np.mean(np.abs(deltas))),
        "mean_delta": float(np.mean(deltas)),
        "max_abs_delta": float(np.max(np.abs(deltas))),
        "largest_moves": sorted(rows, key=lambda r: abs(r["delta"] or 0), reverse=True)[:8],
        "explanation": (
            "Material gaps vs M6 are expected: the simulator enforces team TD budgets and "
            "teammate competition, while M6 scores players marginally. Do not force agreement."
        ),
    }
