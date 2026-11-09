"""Independent ATS grading cases — arithmetic must not depend on model helpers for truth."""

from __future__ import annotations

import pytest

from spe.models.ats.labels import grade_home_spread, grade_side


def _independent(home_score: float, away_score: float, home_handicap: float) -> str:
    margin = home_score - away_score
    edge = margin + home_handicap
    if abs(edge) < 1e-12:
        return "push"
    return "home_cover" if edge > 0 else "away_cover"


@pytest.mark.parametrize(
    "hs,aws,hh,expect",
    [
        (24, 21, -7, "away_cover"),  # home -7 wins 24-21: loses ATS
        (28, 21, -7, "push"),
        (31, 21, -7, "home_cover"),
        (21, 24, 7, "home_cover"),  # home +7 loses 21-24: covers
        (24, 21, -3.5, "away_cover"),  # home -3.5 wins by 3: loses ATS
        (28, 17, 7, "home_cover"),  # away favorite, home +7 wins big
        (21, 17, 0, "home_cover"),  # pick'em
        (20, 20, 0, "push"),
        (23, 20, -3, "push"),  # neutral-style line, exact push
    ],
)
def test_mandatory_ats_cases(hs, aws, hh, expect):
    indep = _independent(hs, aws, hh)
    helper = {"cover": "home_cover", "loss": "away_cover", "push": "push"}[
        grade_home_spread(hs - aws, hh)
    ]
    assert indep == expect
    assert helper == expect
    # Away side is complementary except push
    away = grade_side(hs - aws, hh, "away")
    if expect == "push":
        assert away == "push"
    elif expect == "home_cover":
        assert away == "loss"
    else:
        assert away == "cover"


def test_label_is_ats_not_moneyline():
    # Home wins the game but loses ATS
    assert _independent(24, 21, -7) == "away_cover"
    assert grade_home_spread(3, -7) == "loss"
