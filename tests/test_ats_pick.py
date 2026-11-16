"""ATS pick naming: the side of the number, not a hidden probability."""

from datetime import UTC, datetime, timedelta

import pandas as pd

from spe.models.ats.board_cache import keep_finished_spreads
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


def test_moneyline_switches_when_the_favorite_has_fewer_wins_and_we_like_the_dog():
    tulane = choose_ats_pick(
        predicted_margin=1.1,
        home_handicap=-2.5,
        p_home_cover=0.53,
        p_away_cover=0.47,
        home_team="Rice",
        away_team="Tulane",
        raw_margin=-7.0,
        market_margin=2.5,
        home_record="1-9",
        away_record="5-5",
    )
    assert tulane["pick_label"] == "Tulane to win"
    assert tulane["favored_side"] == "away"
    assert tulane["pick_basis"] == "record"

    # Our guess agrees with the favorite, so a worse record does not move the moneyline.
    saints = choose_ats_pick(
        predicted_margin=1.2,
        home_handicap=-3.5,
        p_home_cover=0.54,
        p_away_cover=0.46,
        home_team="New Orleans Saints",
        away_team="Cleveland Browns",
        raw_margin=2.0,
        market_margin=3.5,
        home_record="3-5",
        away_record="5-4",
    )
    assert saints["pick_label"] == "New Orleans Saints to win"
    assert saints["pick_basis"] == "blended"


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
            {"game_id": "old", "kickoff_utc": now - timedelta(days=20), "status": "final", "margin": 3},
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


def test_close_games_take_our_side_inside_the_band_that_covered():
    nfl = choose_spread_pick(
        predicted_margin=10.0,
        home_handicap=-6.0,
        margin_sigma=13.0,
        home_team="Kansas City Chiefs",
        away_team="Buffalo Bills",
        league_id="nfl",
    )
    assert nfl is not None
    assert nfl["ats_basis"] == "score"
    assert nfl["ats_side"] == "home"
    assert nfl["ats_pick_label"] == "Kansas City Chiefs -6"

    college = choose_spread_pick(
        predicted_margin=4.0,
        home_handicap=-2.0,
        margin_sigma=14.0,
        home_team="Iowa",
        away_team="Wisconsin",
        league_id="fbs",
    )
    assert college is not None
    assert college["ats_basis"] == "score"
    assert college["ats_side"] == "home"

    # College disagreements from a field goal to a touchdown stayed even.
    assert (
        choose_spread_pick(
            predicted_margin=9.0,
            home_handicap=-4.0,
            margin_sigma=14.0,
            home_team="Iowa",
            away_team="Wisconsin",
            league_id="fbs",
        )
        is None
    )


def test_spread_pick_is_the_side_of_the_number_not_the_winner():
    # Without a league, a gap under a touchdown is not a spread pick.
    assert (
        choose_spread_pick(
            predicted_margin=2.0,
            home_handicap=-7.5,
            margin_sigma=13.0,
            home_team="Kansas City Chiefs",
            away_team="Buffalo Bills",
        )
        is None
    )
    # The model has the home team winning by 20 against a 6-point number.
    # That gap is a touchdown or more, so the pick is the number's side.
    pick = choose_spread_pick(
        predicted_margin=20.0,
        home_handicap=-6.0,
        margin_sigma=13.0,
        home_team="Kansas City Chiefs",
        away_team="Buffalo Bills",
    )
    assert pick is not None
    assert pick["ats_side"] == "away"
    assert pick["ats_pick_label"] == "Buffalo Bills +6"
    assert pick["ats_cover_margin"] == 14.0
    assert pick["ats_basis"] == "number"


def test_a_fewer_win_favorite_stays_on_our_side():
    rice = choose_spread_pick(
        predicted_margin=1.1,
        home_handicap=-2.5,
        margin_sigma=14.0,
        home_team="Rice",
        away_team="Tulane",
        model_vs_market=-9.5,
        league_id="fbs",
        home_record="1-9",
        away_record="5-5",
    )
    assert rice is not None
    assert rice["ats_basis"] == "record"
    assert rice["ats_side"] == "away"
    assert rice["ats_pick_label"] == "Tulane +2.5"

    kansas = choose_spread_pick(
        predicted_margin=3.1,
        home_handicap=-5.5,
        margin_sigma=14.0,
        home_team="Kansas",
        away_team="UCF Knights",
        model_vs_market=-15.7,
        league_id="fbs",
        home_record="4-6",
        away_record="5-5",
    )
    assert kansas is not None
    assert kansas["ats_side"] == "away"
    assert kansas["ats_pick_label"] == "UCF Knights +5.5"

    # The same gap still follows the number when that favorite has more wins.
    kept = choose_spread_pick(
        predicted_margin=1.1,
        home_handicap=-2.5,
        margin_sigma=14.0,
        home_team="Rice",
        away_team="Tulane",
        model_vs_market=-9.5,
        league_id="fbs",
        home_record="6-4",
        away_record="5-5",
    )
    assert kept is not None
    assert kept["ats_basis"] == "number"
    assert kept["ats_pick_label"] == "Rice -2.5"


def test_a_locked_fewer_win_favorite_flips_to_our_side():
    row = attach_spread_pick(
        {
            "game_status": "final",
            "home_score": 17,
            "away_score": 20,
            "home_handicap": -2.5,
            "predicted_margin": 1.1,
            "margin_sigma": 14.0,
            "home_team": "Rice",
            "away_team": "Tulane",
            "home_record": "1-9",
            "away_record": "5-5",
            "model_vs_market": -9.5,
            "league_id": "fbs",
            "ats_side": "home",
            "ats_team": "Rice",
            "ats_line": -2.5,
            "ats_pick_label": "Rice -2.5",
            "ats_basis": "number",
            "ats_locked": True,
        }
    )
    assert row["ats_pick_label"] == "Tulane +2.5"
    assert row["ats_pick_result"] == "correct"


def test_our_side_stays_when_that_team_has_fewer_wins():
    row = attach_spread_pick(
        {
            "game_status": "final",
            "home_score": 23,
            "away_score": 22,
            "home_handicap": -3.5,
            "predicted_margin": 4.0,
            "margin_sigma": 13.0,
            "home_team": "New Orleans Saints",
            "away_team": "Cleveland Browns",
            "home_record": "3-5",
            "away_record": "5-4",
            "model_vs_market": 8.0,
            "league_id": "nfl",
            "ats_side": "home",
            "ats_team": "New Orleans Saints",
            "ats_line": -3.5,
            "ats_pick_label": "New Orleans Saints -3.5",
            "ats_basis": "score",
            "ats_locked": True,
        }
    )
    assert row["ats_pick_label"] == "New Orleans Saints -3.5"
    assert row["ats_basis"] == "score"


def test_a_rebuild_flips_a_fewer_win_favorite_and_keeps_the_other_finals():
    previous = {
        "rows": [
            {
                "game_id": "rice",
                "game_status": "final",
                "home_team": "Rice",
                "away_team": "Tulane",
                "home_record": "1-9",
                "away_record": "5-5",
                "home_score": 17,
                "away_score": 20,
                "home_handicap": -2.5,
                "ats_side": "home",
                "ats_team": "Rice",
                "ats_line": -2.5,
                "ats_pick_label": "Rice -2.5",
                "ats_basis": "number",
            },
            {
                "game_id": "charlotte",
                "game_status": "final",
                "home_team": "Charlotte",
                "away_team": "East Carolina",
                "home_record": "4-6",
                "away_record": "5-5",
                "home_score": 16,
                "away_score": 17,
                "ats_side": "home",
                "ats_team": "Charlotte",
                "ats_line": 11.5,
                "ats_pick_label": "Charlotte +11.5",
                "ats_basis": "score",
            },
        ]
    }
    rebuilt = {
        "rows": [
            {
                "game_id": "rice",
                "game_status": "final",
                "home_score": 17,
                "away_score": 20,
                "home_handicap": -2.5,
                "ats_side": "away",
                "ats_team": "Tulane",
                "ats_line": 2.5,
                "ats_pick_label": "Tulane +2.5",
                "ats_basis": "record",
            },
            {
                "game_id": "charlotte",
                "game_status": "final",
                "home_score": 16,
                "away_score": 17,
                "home_handicap": 5.0,
                "ats_side": None,
                "ats_pick_label": None,
            },
        ]
    }
    kept = keep_finished_spreads(previous, rebuilt)
    rice, charlotte = kept["rows"]
    assert rice["ats_pick_label"] == "Tulane +2.5"
    assert rice["ats_pick_result"] == "correct"
    assert charlotte["ats_pick_label"] == "Charlotte +11.5"
    assert charlotte["ats_pick_result"] == "correct"


def test_a_touchdown_gap_takes_the_numbers_side():
    # The raw model likes Boise by 10 more than a 10.5-point number, so the pick is the dog.
    pick = choose_spread_pick(
        predicted_margin=12.0,
        home_handicap=-10.5,
        margin_sigma=14.0,
        home_team="Boise State",
        away_team="Oregon State",
        model_vs_market=10.0,
        league_id="fbs",
    )
    assert pick is not None
    assert pick["ats_side"] == "away"
    assert pick["ats_pick_label"] == "Oregon State +10.5"
    assert pick["ats_basis"] == "number"

    winner = choose_ats_pick(
        predicted_margin=2.0,
        home_handicap=-7.5,
        p_home_cover=0.54,
        p_away_cover=0.46,
        home_team="Kansas City Chiefs",
        away_team="Buffalo Bills",
    )
    assert winner["pick_label"] == "Kansas City Chiefs to win"


def test_a_finished_spread_stays_when_the_gap_moves_out_of_the_band():
    row = attach_spread_pick(
        {
            "game_status": "final",
            "home_score": 16,
            "away_score": 17,
            "home_handicap": 11.5,
            "predicted_margin": -10.0,
            "margin_sigma": 14.0,
            "home_team": "Charlotte",
            "away_team": "East Carolina",
            "model_vs_market": 5.0,
            "ats_side": "home",
            "ats_team": "Charlotte",
            "ats_line": 11.5,
            "ats_pick_label": "Charlotte +11.5",
            "ats_cover_margin": 1.7,
            "ats_basis": "score",
            "pick_market": "straight_up",
            "projection_withheld": False,
        }
    )
    assert row["ats_pick_label"] == "Charlotte +11.5"
    assert row["ats_pick_result"] == "correct"
    assert row["ats_locked"] is True


def test_a_rebuild_keeps_the_spread_that_was_already_on_the_card():
    previous = {
        "rows": [
            {
                "game_id": "charlotte",
                "game_status": "scheduled",
                "ats_side": "home",
                "ats_pick_label": "Charlotte +11.5",
                "ats_team": "Charlotte",
                "ats_line": 11.5,
                "ats_basis": "score",
            }
        ]
    }
    rebuilt = {
        "rows": [
            {
                "game_id": "charlotte",
                "game_status": "final",
                "home_score": 16,
                "away_score": 17,
                "home_handicap": 5.0,
                "ats_side": None,
                "ats_pick_label": None,
                "ats_pick_result": None,
            }
        ]
    }
    kept = keep_finished_spreads(previous, rebuilt)
    assert kept["rows"][0]["ats_pick_label"] == "Charlotte +11.5"
    assert kept["rows"][0]["ats_locked"] is True
    assert kept["rows"][0]["ats_pick_result"] == "correct"
    assert kept["rows"][0]["home_handicap"] == 11.5


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
            "model_vs_market": 8.0,
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
            "model_vs_market": -9.0,
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


def test_a_rookie_quarterbacks_first_start_is_faded():
    raiders = attach_spread_pick(
        {
            "kickoff_utc": "2026-11-15T21:25:00+00:00",
            "game_status": "final",
            "home_score": 13,
            "away_score": 21,
            "home_handicap": 6.5,
            "predicted_margin": -5.6,
            "home_team": "Las Vegas Raiders",
            "away_team": "Seattle Seahawks",
            "model_vs_market": 5.9,
            "league_id": "nfl",
            "favored_side": "away",
            "pick_team": "Seattle Seahawks",
            "pick_label": "Seattle Seahawks to win",
            "pick_market": "straight_up",
            "ats_side": "home",
            "ats_team": "Las Vegas Raiders",
            "ats_line": 6.5,
            "ats_pick_label": "Las Vegas Raiders +6.5",
            "ats_basis": "number",
            "ats_locked": True,
            "projection_withheld": False,
        }
    )
    assert raiders["pick_label"] == "Seattle Seahawks to win"
    assert raiders["ats_pick_label"] == "Seattle Seahawks -6.5"
    assert raiders["ats_basis"] == "rookie_qb"
    assert raiders["ats_pick_result"] == "correct"

    cardinals = attach_spread_pick(
        {
            "kickoff_utc": "2026-11-15T21:05:00+00:00",
            "game_status": "final",
            "home_score": 6,
            "away_score": 34,
            "home_handicap": 9.5,
            "predicted_margin": -8.7,
            "home_team": "Arizona Cardinals",
            "away_team": "Los Angeles Rams",
            "model_vs_market": 5.2,
            "league_id": "nfl",
            "favored_side": "away",
            "pick_team": "Los Angeles Rams",
            "pick_label": "Los Angeles Rams to win",
            "pick_market": "straight_up",
            "ats_side": "home",
            "ats_team": "Arizona Cardinals",
            "ats_line": 9.5,
            "ats_pick_label": "Arizona Cardinals +9.5",
            "ats_basis": "number",
            "ats_locked": True,
            "projection_withheld": False,
        }
    )
    assert cardinals["ats_pick_label"] == "Los Angeles Rams -9.5"
    assert cardinals["ats_pick_result"] == "correct"
    assert cardinals["pick_label"] == "Los Angeles Rams to win"


def test_a_short_dog_lays_the_points_with_the_favorite():
    patriots = attach_spread_pick(
        {
            "game_status": "final",
            "home_score": 35,
            "away_score": 10,
            "home_handicap": -3.0,
            "predicted_margin": 2.8,
            "home_team": "Detroit Lions",
            "away_team": "New England Patriots",
            "model_vs_market": -1.5,
            "league_id": "nfl",
            "favored_side": "home",
            "pick_team": "Detroit Lions",
            "pick_label": "Detroit Lions to win",
            "pick_market": "straight_up",
            "ats_side": "away",
            "ats_team": "New England Patriots",
            "ats_line": 3.0,
            "ats_pick_label": "New England Patriots +3",
            "ats_basis": "score",
            "ats_locked": True,
            "projection_withheld": False,
        }
    )
    assert patriots["ats_pick_label"] == "Detroit Lions -3"
    assert patriots["ats_pick_result"] == "correct"
    assert patriots["pick_label"] == "Detroit Lions to win"

    packers = attach_spread_pick(
        {
            "game_status": "final",
            "home_score": 10,
            "away_score": 30,
            "home_handicap": -2.5,
            "home_team": "Green Bay Packers",
            "away_team": "Minnesota Vikings",
            "ats_side": "home",
            "ats_team": "Green Bay Packers",
            "ats_line": -2.5,
            "ats_pick_label": "Green Bay Packers -2.5",
            "ats_basis": "score",
            "ats_locked": True,
            "pick_market": "straight_up",
            "projection_withheld": False,
        }
    )
    assert packers["ats_pick_label"] == "Green Bay Packers -2.5"
    assert packers["ats_pick_result"] == "miss"


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
