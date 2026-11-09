"""A backup's few scoring games do not become next week's chance."""

from __future__ import annotations

import numpy as np
import pandas as pd

from spe.models.player_td.role_workload import (
    ROLE_BACKUP,
    ROLE_STARTER,
    score_current_share,
    score_historical_rate,
    score_role_workload,
)

BASES = {
    ("RB", ROLE_STARTER): {"carries": 14.0, "targets": 3.0, "gl": 1.2, "rz": 0.5, "rush_td": 0.35, "rec_td": 0.05},
    ("RB", ROLE_BACKUP): {"carries": 1.5, "targets": 0.3, "gl": 0.1, "rz": 0.05, "rush_td": 0.04, "rec_td": 0.01},
    ("WR", ROLE_STARTER): {"carries": 0.2, "targets": 7.0, "gl": 0.0, "rz": 1.4, "rush_td": 0.01, "rec_td": 0.28},
    ("WR", ROLE_BACKUP): {"carries": 0.0, "targets": 1.0, "gl": 0.0, "rz": 0.1, "rush_td": 0.0, "rec_td": 0.03},
}


def _game() -> pd.DataFrame:
    """Same history for the backup and a copy listed as the starter."""
    rows = [
        _row("starter-rb", ROLE_STARTER, "RB", carries=15, targets=3, gl=1.5, rush_td=0.4, rec_td=0.05, rate=0.45, games=16),
        _row("hot-backup", ROLE_BACKUP, "RB", carries=6, targets=0.2, gl=0.2, rush_td=0.67, rec_td=0.0, rate=0.67, games=3),
        _row("same-history-as-starter", ROLE_STARTER, "RB", carries=6, targets=0.2, gl=0.2, rush_td=0.67, rec_td=0.0, rate=0.67, games=3),
        _row("starter-wr", ROLE_STARTER, "WR", carries=0.1, targets=8, gl=0.0, rush_td=0.0, rec_td=0.3, rate=0.32, games=16),
    ]
    frame = pd.DataFrame(rows)
    frame["team_lambda_rush_td"] = 1.2
    frame["team_lambda_pass_td"] = 1.6
    return frame


def _row(player_id: str, role: str, position: str, **values: float) -> dict:
    return {
        "player_id": player_id,
        "game_id": "g1",
        "team_id": "TM",
        "position_group": position,
        "role": role,
        "prior_carries": values["carries"],
        "prior_targets": values["targets"],
        "prior_gl_carries": values["gl"],
        "prior_rz_targets": 0.2,
        "prior_rush_td": values["rush_td"],
        "prior_rec_td": values["rec_td"],
        "prior_td_rate": values["rate"],
        "opportunity_games": values["games"],
        "prior_games": values["games"],
    }


def test_backup_scoring_rate_does_not_become_next_weeks_chance():
    frame = _game()
    historical = score_historical_rate(frame)
    current = score_current_share(frame)
    candidate = score_role_workload(frame, BASES, goal_line_weight=2.5, backup_games=8.0)
    backup = frame.index[frame["player_id"].eq("hot-backup")][0]
    listed_starter = frame.index[frame["player_id"].eq("same-history-as-starter")][0]
    assert historical[backup] > 0.5
    assert candidate[backup] < historical[backup] * 0.5
    assert candidate[backup] < current[backup]
    assert candidate[listed_starter] > candidate[backup]
    assert np.isfinite(candidate).all()
