"""Position-specific Anytime TD mechanisms.

QB Anytime TD = rushing (or rare receiving) score — never a thrown touchdown pass.
"""

from __future__ import annotations

from typing import Any

POSITIONS = ("RB", "WR", "TE", "QB")

POSITION_FEATURE_SETS: dict[str, list[str]] = {
    "RB": [
        "prior_td_rate",
        "roll3_td_rate",
        "prior_carry_share",
        "roll3_carries",
        "prior_gl_carries",
        "prior_rz_carries",
        "prior_rz_touches",
        "prior_rush_td_rate",
        "prior_rec_td_rate",
        "prior_target_share",
        "workload_delta",
        "snap_roll3",
        "own_off_epa",
        "team_rz_td",
        "opp_rush_td_allowed",
        "opp_rz_td_allowed",
        "carry_x_rush_def",
        "is_home",
        "injury_designation_out",
        "injury_designation_uncertain",
    ],
    "WR": [
        "prior_td_rate",
        "roll3_td_rate",
        "prior_target_share",
        "roll3_targets",
        "prior_rz_targets",
        "prior_rz_touches",
        "prior_rec_td_rate",
        "prior_rec_yards",
        "workload_delta",
        "snap_roll3",
        "depth_rank",
        "starter",
        "own_off_epa",
        "team_points_per_opp",
        "opp_rec_td_allowed",
        "opp_pass_epa_allowed",
        "target_x_pass_def",
        "is_home",
        "injury_designation_out",
        "injury_designation_uncertain",
    ],
    "TE": [
        "prior_td_rate",
        "roll3_td_rate",
        "prior_target_share",
        "roll3_targets",
        "prior_rz_targets",
        "prior_rz_touches",
        "prior_rec_td_rate",
        "prior_gl_carries",
        "workload_delta",
        "snap_roll3",
        "depth_rank",
        "own_off_epa",
        "team_rz_td",
        "opp_rec_td_allowed",
        "opp_rz_td_allowed",
        "target_x_pass_def",
        "is_home",
        "injury_designation_out",
        "injury_designation_uncertain",
    ],
    "QB": [
        # Rushing-oriented Anytime TD path only.
        "prior_rush_td_rate",
        "roll3_rush_td",
        "prior_carry_share",
        "roll3_carries",
        "prior_gl_carries",
        "prior_rz_carries",
        "prior_rush_yards",
        "workload_delta",
        "snap_roll3",
        "own_off_epa",
        "opp_rush_td_allowed",
        "opp_rush_epa_allowed",
        "carry_x_rush_def",
        "is_home",
        "injury_designation_out",
        "injury_designation_uncertain",
    ],
}

POSITION_NOTES: dict[str, str] = {
    "RB": "Goal-line / RZ carries + receiving TD share; rush defense matchup.",
    "WR": "Targets, RZ targets, end-zone involvement proxies; pass TD allowed.",
    "TE": "RZ targets and occasional GL; receiving TD path dominant.",
    "QB": "Rushing TDs only — thrown TDs do not count as Anytime TD.",
}


def features_for_position(position: str, league_id: str) -> list[str]:
    feats = list(POSITION_FEATURE_SETS.get(position, []))
    if league_id == "fbs":
        blocked = {
            "prior_gl_carries",
            "prior_rz_carries",
            "prior_rz_targets",
            "prior_rz_touches",
            "prior_target_share",
            "snap_roll3",
            "depth_rank",
            "starter",
            "target_x_pass_def",
        }
        feats = [f for f in feats if f not in blocked]
    return feats


def position_catalog(league_id: str) -> dict[str, Any]:
    return {
        pos: {
            "features": features_for_position(pos, league_id),
            "note": POSITION_NOTES[pos],
            "label": "rush_td+rec_td>0" if pos != "QB" else "rush_td(+rare rec_td)>0; pass_td excluded",
        }
        for pos in POSITIONS
    }
