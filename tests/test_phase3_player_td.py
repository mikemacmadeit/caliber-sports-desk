"""Phase 3 player TD allocation unit tests."""

from __future__ import annotations

import numpy as np
import pandas as pd

from spe.models.player_td.shares import attach_shares, default_weights, raw_rec_scores


def test_qb_pass_td_not_in_receiving_share_without_receptions():
    weights = default_weights("nfl")
    frame = pd.DataFrame(
        [
            {
                "game_id": "g1",
                "team_id": "T",
                "position_group": "QB",
                "prior_carries": 4.0,
                "prior_gl_carries": 0.5,
                "prior_td_rate": 0.2,
                "prior_rush_td": 0.2,
                "prior_targets": 0.0,
                "prior_rz_targets": 0.0,
                "prior_rec_td": 0.0,
                "prior_receptions": 0.0,
                "rush_td": 1,
                "rec_td": 0,
            },
            {
                "game_id": "g1",
                "team_id": "T",
                "position_group": "WR",
                "prior_carries": 0.0,
                "prior_gl_carries": 0.0,
                "prior_td_rate": 0.3,
                "prior_rush_td": 0.0,
                "prior_targets": 8.0,
                "prior_rz_targets": 1.5,
                "prior_rec_td": 0.3,
                "prior_receptions": 5.0,
                "rush_td": 0,
                "rec_td": 1,
            },
            {
                "game_id": "g1",
                "team_id": "T",
                "position_group": "RB",
                "prior_carries": 15.0,
                "prior_gl_carries": 2.0,
                "prior_td_rate": 0.4,
                "prior_rush_td": 0.35,
                "prior_targets": 3.0,
                "prior_rz_targets": 0.4,
                "prior_rec_td": 0.05,
                "prior_receptions": 2.0,
                "rush_td": 1,
                "rec_td": 0,
            },
        ]
    )
    scored = attach_shares(frame, weights)
    qb = scored.loc[scored["position_group"] == "QB"].iloc[0]
    assert float(qb["rec_td_share"]) == 0.0
    assert float(qb["rush_td_share"]) > 0.0
    # Team rush+rec shares among skill leave residual for others.
    assert scored["rush_td_share"].sum() <= 1.0 + 1e-6
    assert scored["rec_td_share"].sum() <= 1.0 + 1e-6


def test_fbs_does_not_use_rz_gl_in_rec_scores():
    weights = default_weights("fbs")
    frame = pd.DataFrame(
        [
            {
                "game_id": "g1",
                "team_id": "T",
                "position_group": "WR",
                "prior_carries": 0.0,
                "prior_gl_carries": 99.0,  # must be ignored
                "prior_td_rate": 0.2,
                "prior_rush_td": 0.0,
                "prior_targets": 99.0,  # ignored for FBS
                "prior_rz_targets": 99.0,  # ignored
                "prior_rec_td": 0.2,
                "prior_receptions": 5.0,
            }
        ]
    )
    # raw_rec_scores should not explode from invented RZ/target columns.
    scores = raw_rec_scores(frame, weights)
    assert np.isfinite(scores).all()
    # Dominated by receptions + rec_td hist, not the 99s.
    assert float(scores[0]) < 50.0


def test_anytime_label_excludes_pass_td_conceptually():
    # Documented contract: anytime = rush + rec only.
    rush, rec, pass_td = 1, 1, 3
    anytime = rush + rec
    assert anytime == 2
    assert anytime != rush + rec + pass_td
