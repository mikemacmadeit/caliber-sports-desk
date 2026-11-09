"""ATS pick naming: the side of the number, not a hidden probability."""

from datetime import UTC, datetime, timedelta

import pandas as pd

from spe.models.ats.predict import (
    _apply_live_spreads,
    apply_recorded_result,
    attach_spread_pick,
    blend_margin_toward_market,
    choose_ats_pick,
    choose_spread_pick,
    format_spread_line,
    grade_displayed_pick,
    select_ats_board_frame,
)


def test_format_spread_line_keeps_the_sign():
    assert format_spread_line(-3.5) == "-3.5"
    assert format_spread_line(3.0) == "+3"
    assert format_spread_line(7.5) == "+7.5"


def test_pick_is_the_team_expected_to_win():
    # Home is a dog on the number, and the score still has them winning.
    home_side = choose_ats_pick(
        predicted_margin=1.0,
        home_handicap=3.5,
        p_home_cover=0.52,
        p_away_cover=0.48,
        home_team="Buffalo Bills",
        away_team="Miami Dolphins",
    )
    assert home_side["pick_market"] == "straight_up"
    assert home_side["favored_side"] == "home"
    assert home_side["pick_label"] == "Buffalo Bills to win"
    assert home_side["pick_line"] == 3.5

    # The number says the road team covers. The score still has the home team winning.
    home_favorite = choose_ats_pick(
        predicted_margin=2.0,
        home_handicap=-7.5,
        p_home_cover=0.54,
        p_away_cover=0.46,
        home_team="Kansas City Chiefs",
        away_team="Buffalo Bills",
    )
    assert home_favorite["favored_side"] == "home"
    assert home_favorite["pick_line"] == -7.5
    assert home_favorite["pick_label"] == "Kansas City Chiefs to win"


def test_posted_number_can_flip_a_close_score():
    # Score model has the home dog by 4. The books have the road team by 4.
    # 85% of the weight stays with the books, so the road team is the pick.
    blended = blend_margin_toward_market(4.0, -4.0)
    assert blended < 0
    pick = choose_ats_pick(
        predicted_margin=blended,
        home_handicap=4.0,
        p_home_cover=0.45,
        p_away_cover=0.55,
        home_team="NC State",
        away_team="Duke",
    )
    assert pick["favored_side"] == "away"
    assert pick["pick_label"] == "Duke to win"


def test_missing_number_is_a_straight_up_pick():
    pick = choose_ats_pick(
        predicted_margin=-4.0,
        home_handicap=None,
        p_home_cover=0.4,
        p_away_cover=0.6,
        home_team="Home",
        away_team="Away",
    )
    assert pick["pick_market"] == "straight_up"
    assert pick["pick_label"] == "Away to win"
    assert pick["pick_line"] is None


def test_board_keeps_recent_finals_and_drops_old_ones():
    now = datetime(2026, 11, 7, 18, tzinfo=UTC)
    frame = pd.DataFrame(
        [
            {"game_id": "future", "kickoff_utc": now + timedelta(days=1), "status": "scheduled"},
            {"game_id": "just_final", "kickoff_utc": now - timedelta(hours=3), "status": "final", "margin": 7},
            {"game_id": "live", "kickoff_utc": now - timedelta(hours=1), "status": "in_progress"},
            {"game_id": "old", "kickoff_utc": now - timedelta(days=6), "status": "final", "margin": 3},
            {"game_id": "scratched", "kickoff_utc": now - timedelta(hours=2), "status": "canceled"},
        ]
    )
    board = select_ats_board_frame(frame, now)
    assert set(board["game_id"]) == {"future", "just_final", "live"}


def test_winner_pick_is_graded_on_who_won():
    # Home won by 3. They did not cover -7, and the pick is still correct.
    assert (
        grade_displayed_pick(
            status="final",
            home_score=24,
            away_score=21,
            pick_market="straight_up",
            favored_side="home",
            home_handicap=-7.0,
        )
        == "correct"
    )
    assert (
        grade_displayed_pick(
            status="final",
            home_score=24,
            away_score=21,
            pick_market="straight_up",
            favored_side="away",
            home_handicap=-7.0,
        )
        == "miss"
    )


def test_final_spread_pick_is_graded_against_the_number():
    # Home won by 3 against -3.5, so the home side missed.
    assert (
        grade_displayed_pick(
            status="final",
            home_score=20,
            away_score=17,
            pick_market="spread",
            favored_side="home",
            home_handicap=-3.5,
        )
        == "miss"
    )
    # Same score against -2.5 covers.
    assert (
        grade_displayed_pick(
            status="final",
            home_score=20,
            away_score=17,
            pick_market="spread",
            favored_side="home",
            home_handicap=-2.5,
        )
        == "correct"
    )
    assert (
        grade_displayed_pick(
            status="final",
            home_score=24,
            away_score=21,
            pick_market="spread",
            favored_side="home",
            home_handicap=-3.0,
        )
        == "push"
    )


def test_open_game_is_not_graded():
    assert (
        grade_displayed_pick(
            status="scheduled",
            home_score=None,
            away_score=None,
            pick_market="spread",
            favored_side="home",
            home_handicap=-3.5,
        )
        is None
    )


def test_finished_game_keeps_the_closing_number_when_the_live_quote_is_gone():
    now = datetime(2026, 11, 7, 18, tzinfo=UTC)
    upcoming = pd.DataFrame(
        [
            {
                "game_id": "done",
                "kickoff_utc": now - timedelta(hours=2),
                "spread_line": 3.5,
                "home_spread_price": -110.0,
                "away_spread_price": -110.0,
                "source": "nflverse_consensus",
            },
            {
                "game_id": "next",
                "kickoff_utc": now + timedelta(days=1),
                "spread_line": 7.0,
                "home_spread_price": -110.0,
                "away_spread_price": -110.0,
                "source": "nflverse_consensus",
            },
            {
                "game_id": "blank",
                "kickoff_utc": now - timedelta(hours=1),
                "spread_line": None,
                "home_spread_price": None,
                "away_spread_price": None,
                "source": None,
            },
        ]
    )
    live = pd.DataFrame(
        [
            {
                "game_id": "next",
                "spread_line": -6.5,
                "home_spread_price": -108,
                "away_spread_price": -112,
                "bookmaker_id": "draftkings",
                "source": "odds_api",
            }
        ]
    )
    out = _apply_live_spreads(upcoming, live, now).set_index("game_id")
    assert out.loc["done", "spread_line"] == 3.5
    assert out.loc["done", "source"] == "nflverse_consensus"
    assert out.loc["next", "spread_line"] == -6.5
    assert out.loc["next", "source"] == "odds_api"
    assert pd.isna(out.loc["blank", "spread_line"])


def test_spread_pick_is_the_side_of_the_number_not_the_winner():
    # The home team is projected to win by 2 and is laying 7.5, so the dog covers.
    pick = choose_spread_pick(
        predicted_margin=2.0,
        home_handicap=-7.5,
        margin_sigma=13.0,
        home_team="Kansas City Chiefs",
        away_team="Buffalo Bills",
    )
    assert pick is not None
    assert pick["ats_side"] == "away"
    assert pick["ats_pick_label"] == "Buffalo Bills +7.5"
    assert pick["ats_cover_margin"] == 5.5

    winner = choose_ats_pick(
        predicted_margin=2.0,
        home_handicap=-7.5,
        p_home_cover=0.54,
        p_away_cover=0.46,
        home_team="Kansas City Chiefs",
        away_team="Buffalo Bills",
    )
    assert winner["pick_label"] == "Kansas City Chiefs to win"


def test_finished_game_grades_the_spread_and_keeps_the_winner():
    row = attach_spread_pick(
        {
            "game_status": "final",
            "home_score": 29,
            "away_score": 28,
            "home_handicap": -6.0,
            "predicted_margin": 4.0,
            "margin_sigma": 13.0,
            "home_team": "Arizona",
            "away_team": "TCU",
            "pick_market": "straight_up",
            "pick_label": "Arizona to win",
            "projection_withheld": False,
        }
    )
    assert row["pick_label"] == "Arizona to win"
    assert row["ats_pick_label"] == "TCU +6"
    assert row["ats_pick_result"] == "correct"


def test_a_final_score_marks_the_moneyline_and_the_spread():
    row = apply_recorded_result(
        {
            "game_status": "scheduled",
            "home_score": None,
            "away_score": None,
            "home_handicap": 8.5,
            "predicted_margin": -6.0,
            "margin_sigma": 13.0,
            "home_team": "Baltimore Ravens",
            "away_team": "Jacksonville Jaguars",
            "favored_side": "away",
            "pick_market": "straight_up",
            "pick_label": "Jacksonville Jaguars to win",
            "projection_withheld": False,
        },
        status="final",
        home_score=10,
        away_score=33,
    )
    assert row["game_status"] == "final"
    assert row["pick_result"] == "correct"
    assert row["ats_pick_label"] == "Baltimore Ravens +8.5"
    assert row["ats_pick_result"] == "miss"


def test_missing_number_has_no_spread_pick():
    assert (
        choose_spread_pick(
            predicted_margin=3.0,
            home_handicap=None,
            margin_sigma=13.0,
            home_team="Home",
            away_team="Away",
        )
        is None
    )


def test_straight_up_pick_uses_the_winner():
    assert (
        grade_displayed_pick(
            status="final",
            home_score=10,
            away_score=17,
            pick_market="straight_up",
            favored_side="away",
            home_handicap=None,
        )
        == "correct"
    )
