"""Human-readable explanations of material sim vs M6 differences."""

from __future__ import annotations

from typing import Any

from spe.simulate.correlated_atd.engine import CorrelatedAtdResult


def explain_m6_deltas(result: CorrelatedAtdResult, *, min_abs: float = 0.04) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    by_team: dict[str, list] = {}
    for p in result.players:
        by_team.setdefault(p.team_id, []).append(p)

    for p in result.players:
        if p.m6_p is None or p.delta_vs_m6 is None:
            continue
        if abs(p.delta_vs_m6) < min_abs:
            continue
        teammates = by_team.get(p.team_id, [])
        teammate_mass = sum(t.p_anytime_td for t in teammates if t.player_id != p.player_id)
        team_exp = (result.team_expected.get(p.team_id) or {}).get("expected_off_tds")
        reasons: list[str] = []
        if p.delta_vs_m6 < 0:
            reasons.append("team_td_budget_and_teammate_competition_compress_marginal_rate")
            if teammate_mass > 0.8:
                reasons.append("high_teammate_scoring_mass")
        else:
            reasons.append("concentrated_opportunity_share_or_elevated_team_lambda")
            if (p.rush_weight + p.rec_weight) > 0.25:
                reasons.append("large_verified_usage_share")
        if p.position_group == "QB":
            reasons.append("qb_anytime_is_rush_only_pass_tds_excluded")
        if team_exp is not None and team_exp < 2.0 and p.m6_p > 0.2:
            reasons.append("team_expected_tds_below_m6_implied_usage")
        notes.append(
            {
                "player_id": p.player_id,
                "name": p.name,
                "position": p.position_group,
                "sim_p": p.p_anytime_td,
                "m6_p": p.m6_p,
                "delta": p.delta_vs_m6,
                "team_expected_off_tds": team_exp,
                "reasons": reasons,
            }
        )
    notes.sort(key=lambda r: abs(r["delta"]), reverse=True)
    return notes[:12]
