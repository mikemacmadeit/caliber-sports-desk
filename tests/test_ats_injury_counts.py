"""Out and Doubtful counts stay on the right side of the ball."""

import pandas as pd

from spe.models.ats.injuries import _counts


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
