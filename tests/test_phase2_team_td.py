"""Phase 2 team TD opportunity engine unit tests."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from spe.models.team_td.labels import team_game_labels
from spe.models.team_td.model import (
    fit_team_td_model,
    multinomial_log_loss,
    poisson_pmf_buckets,
    predict_team_row,
)


def test_labels_separate_offensive_and_def_st():
    plays = pd.DataFrame(
        [
            # Offensive pass TD
            {
                "game_id": "g1",
                "offense_id": "A",
                "defense_id": "B",
                "is_scrimmage": True,
                "is_td": True,
                "is_pass": True,
                "is_rush": False,
                "is_red_zone": True,
                "is_turnover": False,
                "drive_id": "d1",
            },
            # Offensive rush TD
            {
                "game_id": "g1",
                "offense_id": "A",
                "defense_id": "B",
                "is_scrimmage": True,
                "is_td": True,
                "is_pass": False,
                "is_rush": True,
                "is_red_zone": True,
                "is_turnover": False,
                "drive_id": "d2",
            },
            # Non-scrimmage (pick-six) credited to defense B? Wait — defense_id on the play is B,
            # but the scoring team for a pick-six is typically the defense. Our labels count
            # non-scrimmage TDs by defense_id as def_st_td_scored for that team.
            {
                "game_id": "g1",
                "offense_id": "A",
                "defense_id": "B",
                "is_scrimmage": False,
                "is_td": True,
                "is_pass": False,
                "is_rush": False,
                "is_red_zone": False,
                "is_turnover": True,
                "drive_id": "",
            },
            # Ordinary scrimmage play
            {
                "game_id": "g1",
                "offense_id": "B",
                "defense_id": "A",
                "is_scrimmage": True,
                "is_td": False,
                "is_pass": True,
                "is_rush": False,
                "is_red_zone": False,
                "is_turnover": False,
                "drive_id": "d3",
            },
        ]
    )
    labels = team_game_labels(plays)
    a = labels.loc[labels["team_id"] == "A"].iloc[0]
    assert int(a["off_td"]) == 2
    assert int(a["pass_td"]) == 1
    assert int(a["rush_td"]) == 1
    # Def/ST TD is on B (defense_id), not mixed into A's offensive total.
    b = labels.loc[labels["team_id"] == "B"].iloc[0]
    assert int(b["off_td"]) == 0
    assert int(b["def_st_td_scored"]) == 1


def test_poisson_buckets_sum_to_one():
    probs = poisson_pmf_buckets(np.array([0.5, 2.0, 4.5]))
    assert probs.shape == (3, 6)
    np.testing.assert_allclose(probs.sum(axis=1), 1.0, atol=1e-6)


def test_pass_rush_preserve_total():
    train = pd.DataFrame(
        {
            "off_td": [1, 2, 3, 2, 1, 4, 2, 0, 3, 2] * 30,
            "pass_td": [1, 1, 2, 1, 0, 3, 1, 0, 2, 1] * 30,
            "rush_td": [0, 1, 1, 1, 1, 1, 1, 0, 1, 1] * 30,
            "possessions": [11] * 300,
            "off_plays": [65] * 300,
            "rz_plays": [8] * 300,
            "scoring_drives": [3] * 300,
            "prior_off_td": np.linspace(1.0, 3.0, 300),
            "prior_pass_td": np.linspace(0.5, 2.0, 300),
            "prior_rush_td": np.linspace(0.5, 1.5, 300),
            "prior_possessions": [11.0] * 300,
            "prior_off_plays": [65.0] * 300,
            "prior_rz_plays": [8.0] * 300,
            "prior_scoring_drives": [3.0] * 300,
            "prior_off_epa": np.linspace(-0.1, 0.2, 300),
            "prior_pace": [28.0] * 300,
            "prior_explosive": [0.12] * 300,
            "prior_rz_td_rate": [0.2] * 300,
            "prior_pass_share": [0.6] * 300,
            "opp_prior_def_epa": np.linspace(-0.1, 0.1, 300),
            "opp_prior_off_td_allowed": np.linspace(1.5, 2.8, 300),
            "opp_prior_pass_td_allowed": [1.4] * 300,
            "opp_prior_rush_td_allowed": [0.9] * 300,
            "opp_prior_explosive_allowed": [0.12] * 300,
            "opp_prior_rz_td_allowed": [1.5] * 300,
            "exp_possessions": [11.5] * 300,
            "epa_edge": np.linspace(-0.1, 0.2, 300),
            "home_field": [1.0, -1.0] * 150,
            "is_home": [1.0, 0.0] * 150,
            "weather_temp_c": [0.0] * 300,
            "weather_wind_mps": [0.0] * 300,
            "weather_available": [0.0] * 300,
            "injury_out_skill": [0.0] * 300,
            "injury_report_complete": [0.0] * 300,
            "sample_size": list(range(300)),
        }
    )
    model = fit_team_td_model(train, "nfl", datetime(2025, 8, 1, tzinfo=UTC).isoformat())
    pred = predict_team_row(model, train.iloc[10].to_dict())
    assert abs(pred["lambda_off_td"] - (pred["lambda_pass_td"] + pred["lambda_rush_td"])) < 1e-6
    assert abs(sum(pred["p_off_td"].values()) - 1.0) < 1e-6


def test_multinomial_log_loss_finite():
    y = np.array([0, 1, 2, 5, 3])
    probs = poisson_pmf_buckets(np.array([1.0, 1.0, 2.0, 4.0, 2.5]))
    ll = multinomial_log_loss(y, probs)
    assert np.isfinite(ll)
    assert ll > 0
