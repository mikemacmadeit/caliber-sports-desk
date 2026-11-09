"""Proof-of-accuracy helpers — no promotion, no new models."""

import numpy as np

from spe.simulate.correlated_atd.paired_compare import game_clustered_paired_delta
from spe.simulate.correlated_atd.proof_isolation import PLAY_ENABLED, PROMOTED, banner
from spe.simulate.correlated_atd.sample_audit import LABEL_HISTORICAL_REPLAY


def test_proof_flags_off():
    b = banner()
    assert PLAY_ENABLED is False
    assert PROMOTED is False
    assert b["validated_play"] is False
    assert b["develop_new_models"] is False


def test_historical_replay_label_constant():
    assert "historical" in LABEL_HISTORICAL_REPLAY
    assert "live" not in LABEL_HISTORICAL_REPLAY


def test_game_clustered_paired_delta_detects_better_model():
    rng = np.random.default_rng(0)
    games = np.array([f"g{i//10}" for i in range(200)])
    y = rng.binomial(1, 0.2, size=200).astype(float)
    # Model A closer to truth than B.
    p_a = np.clip(0.15 + 0.5 * y + rng.normal(0, 0.03, 200), 0.01, 0.99)
    p_b = np.clip(0.25 + rng.normal(0, 0.08, 200), 0.01, 0.99)
    d = game_clustered_paired_delta(y, p_a, p_b, games, metric="brier", n_boot=200, seed=1)
    assert d["delta"] < 0
    assert d["hi"] < 0 or d["excludes_zero"] or d["delta"] < -0.005
