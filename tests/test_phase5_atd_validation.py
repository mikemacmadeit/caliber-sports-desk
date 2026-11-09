"""Phase 5 validation helpers — research-only; no promotion."""

import numpy as np

from spe.simulate.correlated_atd.phase5_isolation import PHASE, PLAY_ENABLED, phase5_banner
from spe.simulate.correlated_atd.scoring import bootstrap_ci, bucket_metrics, summarize_predictions
from spe.models.metrics import brier_score
import pandas as pd


def test_phase5_play_stays_off():
    banner = phase5_banner()
    assert PHASE == 5
    assert PLAY_ENABLED is False
    assert banner["validated_play"] is False
    assert banner["promoted"] is False


def test_bootstrap_ci_contains_point():
    rng = np.random.default_rng(0)
    y = rng.binomial(1, 0.2, size=400).astype(float)
    p = np.clip(y * 0.5 + 0.15 + rng.normal(0, 0.05, size=400), 0.01, 0.99)
    ci = bootstrap_ci(y, p, brier_score, n_boot=200, seed=1)
    assert ci["lo"] <= ci["point"] <= ci["hi"]


def test_summarize_predictions_shape():
    y = np.array([0.0, 1.0, 0.0, 1.0, 0.0] * 30)
    p = np.array([0.1, 0.4, 0.2, 0.35, 0.15] * 30)
    meta = pd.DataFrame({"position_group": ["RB", "WR", "TE", "QB", "RB"] * 30})
    out = summarize_predictions(y, p, meta, n_boot=50, seed=2)
    assert out["n"] == 150
    assert "brier_ci" in out
    assert out["by_probability_range"]
    buckets = bucket_metrics(y, p, [0.0, 0.2, 0.5, 1.0])
    assert buckets
