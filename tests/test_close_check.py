"""Close calls move only when the last month and the schedule both disagree."""

from datetime import UTC, datetime

from spe.models.ats.close_check import CloseLook, close_swap_side

KICK = datetime(2026, 11, 14, 23, tzinfo=UTC)


def _look() -> CloseLook:
    recent = {
        "Home": [
            (datetime(2026, 10, 1, tzinfo=UTC), -10.0),
            (datetime(2026, 10, 8, tzinfo=UTC), -8.0),
            (datetime(2026, 10, 15, tzinfo=UTC), -12.0),
            (datetime(2026, 10, 22, tzinfo=UTC), -6.0),
        ],
        "Away": [
            (datetime(2026, 10, 1, tzinfo=UTC), 4.0),
            (datetime(2026, 10, 8, tzinfo=UTC), 2.0),
            (datetime(2026, 10, 15, tzinfo=UTC), -1.0),
            (datetime(2026, 10, 22, tzinfo=UTC), 3.0),
        ],
    }
    # Home rating is worse, so the schedule also wants the away team against a home favorite.
    return CloseLook("fbs", recent, {"Home": -4.0, "Away": 1.0})


def _row(**extra):
    base = {
        "ats_basis": "score",
        "ats_side": "home",
        "home_team": "Home",
        "away_team": "Away",
        "home_handicap": -3.0,
        "kickoff_utc": KICK,
        "league_id": "fbs",
    }
    base.update(extra)
    return base


def test_both_checks_swap_a_close_favorite():
    assert close_swap_side(_row(), _look()) == "away"


def test_one_check_keeps_the_pick():
    look = _look()
    look.ratings = {"Home": 6.0, "Away": 0.0}
    assert close_swap_side(_row(), look) is None


def test_a_big_gap_is_not_reopened():
    assert close_swap_side(_row(ats_basis="number"), _look()) is None
