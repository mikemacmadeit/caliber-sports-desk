"""Advanced-model checks: no leakage, no fabricated zeros, M3 files stay put."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from spe.models.advanced import (
    add_player_priors,
    apply_beta,
    assign_roster_team,
    candidate_features,
    fit_log_beta,
    keep_feature,
    write_comparison,
)
from spe.models.feature_audit import issue_time_is_inferred


def _games() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "player_id": "p1",
                "kickoff_utc": datetime(2022, 9, 1, tzinfo=UTC),
                "targets": 10,
                "receptions": 6,
                "carries": 2,
                "rec_yards": 40,
                "rush_yards": 8,
                "rz_targets": 3,
                "gl_carries": 1,
                "anytime_tds": 1,
                "target_share": 0.4,
                "carry_share": 0.1,
            },
            {
                "player_id": "p1",
                "kickoff_utc": datetime(2022, 9, 8, tzinfo=UTC),
                "targets": 2,
                "receptions": 1,
                "carries": 0,
                "rec_yards": 5,
                "rush_yards": 0,
                "rz_targets": 0,
                "gl_carries": 0,
                "anytime_tds": 0,
                "target_share": 0.1,
                "carry_share": 0.0,
            },
        ]
    )


def test_prior_features_do_not_include_the_current_game():
    out = add_player_priors(_games(), "nfl")
    assert np.isnan(out.iloc[0]["prior_targets"])
    assert out.iloc[1]["prior_targets"] == 10
    assert out.iloc[1]["prior_rz_targets"] == 3


def test_college_red_zone_zeros_are_not_used_as_measurements():
    out = add_player_priors(_games(), "fbs")
    assert out["prior_rz_targets"].isna().all()
    assert out["prior_gl_carries"].isna().all()


def test_feature_must_help_on_every_selection_season():
    assert keep_feature([0.02, 0.02], 0.02)
    assert not keep_feature([0.05, 0.001], 0.02)
    assert not keep_feature([], 0.02)


def test_missing_feature_does_not_change_the_mean():
    base = np.array([10.0, 12.0])
    feature = np.array([0.0, 0.0])
    assert np.allclose(apply_beta(base, feature, fit_log_beta(base, feature, np.array([10.0, 12.0]))), base)


def test_open_meteo_issue_time_exactly_one_day_before_the_hour_is_inferred():
    valid = datetime(2024, 9, 8, 20, tzinfo=UTC)
    assert issue_time_is_inferred(valid - timedelta(hours=24), valid)
    assert not issue_time_is_inferred(valid - timedelta(hours=18, minutes=30), valid)


def test_research_features_do_not_include_inferred_weather():
    nfl = candidate_features("nfl", "opportunity")
    fbs = candidate_features("fbs", "anytime_td")
    assert "wind_mps" not in nfl
    assert "snap_delta" in nfl
    assert "ngs_separation" in nfl
    assert "snap_roll3" not in fbs
    assert "prior_rz_targets" not in fbs


def test_missing_nfl_team_comes_only_from_a_unique_roster_week():
    stats = pd.DataFrame(
        [
            {"player_id": "p1", "season_year": 2024, "week": 1, "team_id": None, "home_team_id": "nfl:KC", "away_team_id": "nfl:BAL"},
            {"player_id": "p2", "season_year": 2024, "week": 1, "team_id": None, "home_team_id": "nfl:KC", "away_team_id": "nfl:BAL"},
            {"player_id": "p3", "season_year": 2024, "week": 1, "team_id": None, "home_team_id": "nfl:KC", "away_team_id": "nfl:BAL"},
            {"player_id": "p4", "season_year": 2024, "week": 1, "team_id": "nfl:KC", "home_team_id": "nfl:KC", "away_team_id": "nfl:BAL"},
        ]
    )
    roster = pd.DataFrame(
        [
            {"player_id": "p1", "season_year": 2024, "week": 1, "team_id": "nfl:KC"},
            {"player_id": "p2", "season_year": 2024, "week": 1, "team_id": "nfl:KC"},
            {"player_id": "p2", "season_year": 2024, "week": 1, "team_id": "nfl:BAL"},
            {"player_id": "p3", "season_year": 2024, "week": 1, "team_id": "nfl:DET"},
        ]
    )
    out = assign_roster_team(stats, roster)
    assert out.loc[0, "team_id"] == "nfl:KC"
    assert pd.isna(out.loc[1, "team_id"])
    assert pd.isna(out.loc[2, "team_id"])
    assert out.loc[3, "team_id"] == "nfl:KC"


def test_comparison_report_does_not_replace_m3(tmp_path):
    m3 = tmp_path / "nfl_m3.json"
    m3.write_text('{"play_enabled": false}', encoding="utf-8")
    before = m3.read_bytes()
    write_comparison({"play_enabled": False, "promoted": False, "leagues": []}, tmp_path / "m4_comparison.json")
    assert m3.read_bytes() == before
    assert (tmp_path / "m4_comparison.json").exists()
