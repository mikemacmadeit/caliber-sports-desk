"""Out and Doubtful counts stay on the right side of the ball."""

import pandas as pd

from spe.models.ats.injuries import _counts
from spe.models.ats.model import AtsModelArtifact, availability_shift


def test_qb_out_is_counted_for_the_team_on_the_report():
    group = pd.DataFrame(
        [
            {"is_home": True, "is_qb": True, "is_skill": False},
            {"is_home": True, "is_qb": False, "is_skill": True},
            {"is_home": False, "is_qb": False, "is_skill": True},
            {"is_home": False, "is_qb": False, "is_skill": True},
        ]
    )
    counts = _counts(group)
    assert counts["home_qb_out"] == 1.0
    assert counts["away_qb_out"] == 0.0
    assert counts["home_skill_out"] == 1.0
    assert counts["away_skill_out"] == 2.0


def test_a_model_without_availability_uses_the_measured_quarterback_weight(monkeypatch):
    monkeypatch.setattr(
        "spe.models.ats.model._availability_weights",
        lambda: {"home_qb_out": -2.0, "away_qb_out": 3.0, "home_skill_out": 0.0, "away_skill_out": 0.0},
    )
    artifact = AtsModelArtifact(
        league_id="fbs",
        family="ridge_margin",
        feature_cols=["home_field"],
        coef=[1.0],
        intercept=0.0,
        sigma=14.0,
        hfa=2.5,
        ridge_alpha=12.0,
        platt_a=0.0,
        platt_b=1.0,
        train_n=10,
    )
    frame = pd.DataFrame({"home_qb_out": [1.0], "away_qb_out": [0.0]})
    assert availability_shift(artifact, frame)[0] == -2.0
    trained = AtsModelArtifact(
        league_id="nfl",
        family="ridge_margin",
        feature_cols=["home_qb_out"],
        coef=[-2.0],
        intercept=0.0,
        sigma=13.0,
        hfa=2.5,
        ridge_alpha=8.0,
        platt_a=0.0,
        platt_b=1.0,
        train_n=10,
    )
    assert availability_shift(trained, frame)[0] == 0.0
