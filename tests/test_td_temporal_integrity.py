import numpy as np
import pandas as pd
import pytest

from spe.models.player_td.dataset import temporal_team_predictions
from spe.models.player_td.model import fit_player_td_model, predict_frame
from spe.models.player_td.shares import attach_shares, default_weights
from spe.models.player_td.temporal import split_games


def test_whole_simultaneous_games_stay_in_one_period():
    frame = pd.DataFrame({"game_id": ["a", "a", "b", "c", "d"],
        "kickoff_utc": pd.to_datetime(["2025-01-01", "2025-01-01", "2025-01-02", "2025-01-02", "2025-01-03"], utc=True)})
    fit, cal = split_games(frame, .5)
    assert set(fit.game_id).isdisjoint(cal.game_id)
    assert fit.kickoff_utc.max() < cal.kickoff_utc.min()
    frame.loc[1, "kickoff_utc"] = pd.Timestamp("2025-01-04", tz="UTC")
    with pytest.raises(ValueError, match="inconsistent"):
        split_games(frame)


def test_future_team_results_do_not_change_earlier_predictions():
    team = pd.DataFrame([{"game_id": f"{year}-{i}", "team_id": "t",
        "kickoff_utc": pd.Timestamp(f"{year}-09-01", tz="UTC") + pd.Timedelta(days=i*7),
        "prior_off_td": 2 + i % 3, "opp_prior_off_td_allowed": 2., "home_field": i % 2,
        "off_td": 2 + i % 2, "pass_td": 1., "rush_td": 1 + i % 2,
        "possessions": 10., "off_plays": 60., "rz_plays": 6., "scoring_drives": 3.}
        for year in (2023, 2024, 2025) for i in range(12)])
    players = team[["game_id", "team_id", "kickoff_utc"]].copy()
    first = temporal_team_predictions(team, players, "nfl", min_train_rows=10)
    changed = team.copy()
    changed.loc[changed.kickoff_utc.dt.year >= 2024, ["off_td", "pass_td", "rush_td"]] *= 4
    second = temporal_team_predictions(changed, players, "nfl", min_train_rows=10)
    mask = players.kickoff_utc.dt.year == 2024
    np.testing.assert_allclose(first.loc[mask, "team_lambda_off_td"], second.loc[mask, "team_lambda_off_td"])
    assert first.loc[mask, "team_td_artifact_sha256"].tolist() == second.loc[mask, "team_td_artifact_sha256"].tolist()
    assert first.loc[players.kickoff_utc.dt.year == 2023, "team_lambda_off_td"].isna().all()
    assert (pd.to_datetime(first.loc[mask, "team_td_train_end"]) < first.loc[mask, "kickoff_utc"]).all()


def test_player_calibration_does_not_refit_base_components():
    rng = np.random.default_rng(42)
    frame = pd.DataFrame([{"game_id": f"g{game}", "team_id": "t", "position_group": "RB" if p < 4 else "WR",
        "kickoff_utc": pd.Timestamp("2024-09-01", tz="UTC") + pd.Timedelta(days=game),
        "prior_carries": 10. if p < 4 else 0., "prior_receptions": 1. if p < 4 else 5.,
        "prior_targets": 2. if p < 4 else 7., "rush_td": int(rng.random() < .2),
        "rec_td": int(rng.random() < .1), "team_lambda_rush_td": 1.4,
        "team_lambda_pass_td": 1.8, "is_home": game % 2} for game in range(60) for p in range(10)])
    frame["anytime_tds"] = frame.rush_td + frame.rec_td
    first = fit_player_td_model(frame, "nfl", "2025-01-01", default_weights("nfl"))
    changed = frame.copy()
    changed.loc[changed.kickoff_utc >= pd.Timestamp(first.calibration_start), ["anytime_tds", "rush_td", "rec_td"]] = 1
    second = fit_player_td_model(changed, "nfl", "2025-01-01", default_weights("nfl"))
    assert first.share_weights == second.share_weights
    assert first.feature_betas == second.feature_betas
    assert first.feature_z == second.feature_z
    assert pd.Timestamp(first.train_end) < pd.Timestamp(first.calibration_start)
    # Missing availability probability must not crash a correctly labeled status.
    out = predict_frame(first, frame.iloc[:10].assign(availability_status="active"))
    assert np.isfinite(out.p_atd).all()
    assert out.uncertainty_status.eq("unvalidated_sensitivity_range").all()


def test_no_receiver_cannot_restore_receiving_share_to_pocket_qb():
    frame = pd.DataFrame([{"game_id": "g", "team_id": "t", "position_group": "QB",
                           "prior_carries": 0, "prior_receptions": 0, "prior_targets": 0}])
    assert attach_shares(frame, default_weights("nfl")).rec_td_share.iloc[0] == 0
