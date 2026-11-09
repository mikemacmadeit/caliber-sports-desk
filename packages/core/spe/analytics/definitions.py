"""Metric catalog. Missing premium charting is explicit, never filled with zeros-as-truth."""

from __future__ import annotations

TEAM_METRICS = (
    "off_epa_per_play",
    "def_epa_per_play",
    "pass_epa_per_play",
    "rush_epa_per_play",
    "off_success_rate",
    "def_success_rate",
    "explosive_play_rate",
    "explosive_play_rate_allowed",
    "pass_success_rate",
    "rush_success_rate",
    "third_down_rate",
    "red_zone_td_rate",
    "plays_per_game",
    "pace_sec_per_play",
    "neutral_pass_rate",
    "points_per_scoring_opp",
    "adj_off_epa",
    "adj_def_epa",
    "pass_yards",
    "rush_yards",
    "pass_yards_allowed",
    "rush_yards_allowed",
    "rec_td_allowed",
    "rush_td_allowed",
    "rz_td_allowed",
    "pass_epa_allowed",
    "rush_epa_allowed",
)

PLAYER_METRICS = (
    "targets",
    "receptions",
    "rec_yards",
    "rush_yards",
    "targets_per_game",
    "receptions_per_game",
    "target_share",
    "team_td_share",
    "rz_targets",
    "rz_carries",
    "gl_carries",
    "rec_yards_per_target",
    "rush_yards_per_carry",
    "expected_td_opps",
    "adj_rec_yards_per_game",
    "adj_rush_yards_per_game",
    "roll3_targets",
    "roll5_targets",
    "roll10_targets",
    "roll3_carries",
    "roll5_carries",
    "roll10_carries",
    "workload_delta_3v10",
)

# Play-level NGS route and coverage labels are stored on participation_plays.
# They are not a player route chart and not a WR/CB assignment.
# Weekly NGS separation lives on usage_pregame rows, not in the box-score feature set.
UNAVAILABLE = (
    "route_participation",
    "routes_run",
    "coverage_assignment",
    "wr_cb_matchup",
    "ngs_separation",
    "charted_air_yards_intended",
)

# CFBD plays do not identify ball-handlers. Do not fill these from play text.
FBS_UNAVAILABLE = UNAVAILABLE + (
    "play_level_player_ids",
    "rz_targets_from_pbp",
    "rz_carries_from_pbp",
    "gl_carries_from_pbp",
)

# NFL EPA and FBS PPA share a column name after mapping. They are not the same unit.
# Never concatenate leagues or compare raw EPA to raw PPA.
EPA_PPA_NOTE = (
    "NFL models use EPA/play; FBS models use PPA/play. Each league is z-scored "
    "inside that league before opponent adjustment. Do not mix in one model."
)

SHRINKAGE_K = {"nfl": 8.0, "fbs": 14.0}
EXPLOSIVE_PASS_YARDS = 16.0
EXPLOSIVE_RUSH_YARDS = 10.0
FEATURE_VERSION = "pregame_v1"
ANALYTICS_SOURCE = "caliber_analytics_v1"
