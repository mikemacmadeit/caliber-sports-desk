"""Pregame eligibility does not invent activity from postgame snaps or same-week rosters."""

from __future__ import annotations

import pandas as pd

from spe.models.validation import market_comparison_status
from spe.models.eligibility import (
    absence_rows,
    availability_class,
    include_in_corrected,
    label_stat_rows,
    summarize_eligibility,
)


def test_practice_and_missing_reports_are_not_availability():
    assert availability_class("ACT", "Full Participation in Practice") == "expected_active"
    assert availability_class(None, "Did Not Participate In Practice") == "unknown"
    assert availability_class(None, None) == "unknown"
    assert availability_class("RES", None) == "unknown"
    assert availability_class("ACT", "Out") == "confirmed_inactive"
    assert availability_class("ACT", "Questionable") == "uncertain"
    assert availability_class("ACT", None) == "expected_active"


def test_confirmed_active_is_never_assigned_and_zeros_stay():
    assert include_in_corrected(True, "unknown")
    assert include_in_corrected(False, "expected_active")
    assert include_in_corrected(False, "uncertain")
    assert not include_in_corrected(False, "unknown")
    assert not include_in_corrected(True, "confirmed_inactive")


def test_same_week_roster_does_not_label_the_current_game():
    stats = pd.DataFrame(
        [
            {
                "player_id": "p1",
                "game_id": "g2",
                "season_year": 2024,
                "week": 2,
                "team_id": "nfl:KC",
                "targets": 0,
                "receptions": 0,
                "carries": 0,
                "rec_yards": 0,
                "rush_yards": 0,
                "position_group": "WR",
            }
        ]
    )
    roster = pd.DataFrame(
        [
            {"player_id": "p1", "season_year": 2024, "week": 1, "team_id": "nfl:KC", "status": "ACT"},
            {"player_id": "p1", "season_year": 2024, "week": 2, "team_id": "nfl:KC", "status": "INA"},
        ]
    )
    labeled = label_stat_rows(stats, roster, pd.DataFrame())
    assert labeled.loc[0, "availability"] == "expected_active"
    assert bool(labeled.loc[0, "involved"]) is False


def test_two_prior_teams_stay_unknown_and_absences_are_zero_outcomes():
    roster = pd.DataFrame(
        [
            {"player_id": "p1", "season_year": 2024, "week": 1, "team_id": "nfl:KC", "status": "ACT", "position_group": "RB"},
            {"player_id": "p2", "season_year": 2024, "week": 1, "team_id": "nfl:KC", "status": "ACT", "position_group": "RB"},
            {"player_id": "p2", "season_year": 2024, "week": 1, "team_id": "nfl:BAL", "status": "ACT", "position_group": "RB"},
        ]
    )
    games = pd.DataFrame(
        [
            {
                "game_id": "g2",
                "season_year": 2024,
                "week": 2,
                "home_team_id": "nfl:KC",
                "away_team_id": "nfl:BAL",
                "kickoff_utc": "2024-09-15",
            }
        ]
    )
    added = absence_rows(games, roster, pd.DataFrame(), pd.DataFrame({"game_id": ["g2"], "player_id": ["p1"]}))
    assert added.empty or set(added["player_id"]) == set()
    added = absence_rows(games, roster, pd.DataFrame(), pd.DataFrame(columns=["game_id", "player_id"]))
    assert set(added["player_id"]) == {"p1"}
    assert int(added.iloc[0]["carries"]) == 0
    assert "offense_snaps" not in added.columns


def test_research_lines_are_not_stored_as_market_comparisons():
    status = market_comparison_status(0, 0)
    assert status["market_comparison"] == "unavailable"
    assert status["research_lines_are_market_comparisons"] is False


def test_summary_counts_zeros_and_inactive_exclusions():
    rows = pd.DataFrame(
        [
            {"position_group": "WR", "involved": True, "has_stat_row": True, "availability": "expected_active"},
            {"position_group": "WR", "involved": False, "has_stat_row": True, "availability": "unknown"},
            {"position_group": "RB", "involved": False, "has_stat_row": False, "availability": "expected_active"},
            {"position_group": "RB", "involved": True, "has_stat_row": True, "availability": "confirmed_inactive"},
            {"position_group": "QB", "involved": False, "has_stat_row": False, "availability": "unknown"},
            {"position_group": "K", "involved": False, "has_stat_row": True, "availability": "unknown"},
        ]
    )
    summary = summarize_eligibility(rows)
    assert summary["current_involved_skill_rows"] == 2
    assert summary["added_recorded_zeros"] == 1
    assert summary["added_absent_expected_active"] == 1
    assert summary["excluded_confirmed_inactive"] == 1
    assert summary["excluded_unknown_absent"] == 1
    assert summary["confirmed_active_pregame"] == 0
    assert summary["corrected_skill_rows"] == 3
