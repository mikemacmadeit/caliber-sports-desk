from __future__ import annotations

import numpy as np
import pandas as pd

from spe.shadow.atd_role_calibration import apply_role_calibration, fit_role_calibration, role_tags


def test_role_tags_rushing_qb_and_rb():
    frame = pd.DataFrame(
        {
            "position_group": ["QB", "QB", "RB", "WR"],
            "prior_carries": [6, 1, 12, 0],
        }
    )
    tags = role_tags(frame)
    assert bool(tags.loc[0, "rushing_qb"]) is True
    assert bool(tags.loc[1, "rushing_qb"]) is False
    assert bool(tags.loc[2, "high_carry_rb"]) is True
    assert bool(tags.loc[3, "high_carry_rb"]) is False


def test_apply_positive_shift_raises_p():
    art = {"ok": True, "logit_shifts": {"rushing_qb": 0.5, "high_carry_rb": 0.0, "rb": 0.0, "qb": 0.0}}
    before = 0.25
    after = apply_role_calibration(before, position_group="QB", prior_carries=8, artifact=art)
    assert float(after) > before


def test_holdout_labels_do_not_select_calibration_shifts(tmp_path, monkeypatch):
    import spe.shadow.atd_role_calibration as calibration
    monkeypatch.setattr(calibration, "artifact_path", lambda: tmp_path / "role.json")
    rng = np.random.default_rng(0)
    n = 400
    pos = np.array(["RB"] * 200 + ["QB"] * 200)
    carries = np.concatenate([rng.integers(8, 20, 200), rng.integers(4, 12, 200)]).astype(float)
    # True rate higher than naive p for these roles
    p = np.full(n, 0.22)
    y = (rng.random(n) < 0.35).astype(float)
    frame = pd.DataFrame(
        {
            "kickoff_utc": pd.date_range("2026-09-01", periods=n // 10, freq="h", tz="UTC").repeat(10),
            "position_group": pos,
            "prior_carries": carries,
            "p_blend": p,
            "y": y,
            "game_id": [f"g{i // 10}" for i in range(n)],
        }
    )
    fit = fit_role_calibration(frame, p_col="p_blend", y_col="y")
    assert fit["ok"] is True
    changed = frame.copy()
    changed.loc[280:, "y"] = 1 - changed.loc[280:, "y"]
    again = fit_role_calibration(changed, p_col="p_blend", y_col="y")
    assert fit["logit_shifts"] == again["logit_shifts"]
    assert fit["holdout"]["brier_after"] != again["holdout"]["brier_after"]
    assert not fit["evaluation_used_for_selection"]
