"""Desk shortlists: point edge on the number, and a price the chance beats."""

from spe.desk.best_bets import desk_price_cards, price_shortlist, prop_desk_picks, spread_shortlist


def _spread(**kwargs):
    base = {
        "game_id": "g1",
        "league_id": "fbs",
        "kickoff_utc": "2026-11-14T23:00:00+00:00",
        "game_status": "scheduled",
        "home_team_id": "h",
        "away_team_id": "a",
        "home_team": "Home",
        "away_team": "Away",
        "ats_pick_label": "Away +3",
        "ats_cover_margin": 1.4,
    }
    base.update(kwargs)
    return base


def test_spread_shortlist_ranks_the_larger_edge():
    rows = [
        _spread(game_id="small", ats_cover_margin=1.1, ats_pick_label="Small +1"),
        _spread(
            game_id="big",
            home_team_id="h2",
            away_team_id="a2",
            ats_cover_margin=3.2,
            ats_pick_label="Big +7",
        ),
        _spread(
            game_id="flat",
            home_team_id="h3",
            away_team_id="a3",
            ats_cover_margin=0.4,
            ats_pick_label="Flat +1",
        ),
    ]
    picked = spread_shortlist(rows, date="2026-11-14")
    assert [row["game_id"] for row in picked] == ["big", "small"]
    assert picked[0]["edge_points"] == 3.2


def test_a_cover_is_not_graded_as_a_straight_up_loss():
    from spe.desk.day_results import _grade_spread

    class Finished:
        status = "final"
        home_score = 28
        away_score = 19

    covered = {"ats_side": "away", "home_handicap": -12.5, "pick_market": "straight_up", "ats_pick_result": "miss"}
    _grade_spread(covered, Finished())
    assert covered["ats_pick_result"] == "correct"

    missed = {"ats_side": "home", "home_handicap": -9.5, "pick_market": "straight_up"}
    _grade_spread(missed, type("Game", (), {"status": "final", "home_score": 37, "away_score": 35})())
    assert missed["ats_pick_result"] == "miss"


def test_spread_shortlist_keeps_every_pick_on_the_day():
    rows = [
        _spread(game_id=f"g{index}", home_team_id=f"h{index}", away_team_id=f"a{index}", ats_cover_margin=1 + index)
        for index in range(8)
    ]
    picked = spread_shortlist(rows, date="2026-11-14")
    assert len(picked) == 8


def test_a_finished_spread_stays_on_the_day_and_keeps_its_grade():
    picked = spread_shortlist(
        [
            _spread(
                game_status="final",
                home_score=31,
                away_score=10,
                ats_side="home",
                home_handicap=-9.5,
            )
        ],
        date="2026-11-14",
    )
    assert picked[0]["game_id"] == "g1"
    assert picked[0]["ats_pick_result"] == "correct"


def test_price_shortlist_keeps_a_stale_number_that_the_chance_beats():
    rows = [
        {
            "game_id": "g1",
            "player_name": "A",
            "book_price": 150,
            "forecast": {"p_estimate": 0.55, "availability_status": "active"},
            "price_freshness": {"status": "stale", "is_current_opportunity": False},
        },
        {
            "game_id": "g1",
            "player_name": "B",
            "book_price": -200,
            "forecast": {"p_estimate": 0.62, "availability_status": "active"},
        },
        {
            "game_id": "g2",
            "player_name": "Out",
            "book_price": 300,
            "forecast": {"p_estimate": 0.7, "availability_status": "out"},
        },
    ]
    picked = price_shortlist(rows)
    assert [row["player_name"] for row in picked] == ["A"]


def test_the_card_keeps_one_prop_per_game():
    rows = [
        {
            "game_id": "g1",
            "player_id": "best",
            "market_id": "receiving_yards",
            "player_name": "Best",
            "model_lean": True,
            "book_price": -110,
            "side": "over",
            "line": 40.5,
            "pick": "Best over 40.5 receiving yards",
            "forecast": {"p_estimate": 0.64, "availability_status": "active", "notes": "drop me"},
            "offers": [{"book": "huge"}],
        },
        {
            "game_id": "g1",
            "player_id": "soft",
            "market_id": "receptions",
            "player_name": "Soft",
            "model_lean": True,
            "book_price": -110,
            "side": "over",
            "line": 5.5,
            "forecast": {"p_estimate": 0.56, "availability_status": "active"},
        },
        {
            "game_id": "g2",
            "player_id": "other",
            "market_id": "rushing_yards",
            "player_name": "Other",
            "model_lean": True,
            "book_price": -110,
            "side": "over",
            "line": 20.5,
            "forecast": {"p_estimate": 0.60, "availability_status": "active"},
        },
    ]
    cards = desk_price_cards(rows)
    assert {(card["game_id"], card["player_name"]) for card in cards} == {("g1", "Best"), ("g2", "Other")}
    assert cards[0]["forecast"] == {"p_estimate": 0.64}
    assert "offers" not in cards[0]


def test_prop_desk_picks_are_counting_markets_not_touchdowns():
    rows = [
        {
            "game_id": "g1",
            "market_id": "anytime_td",
            "player_name": "Scorer",
            "model_lean": True,
            "book_price": 150,
            "forecast": {"p_estimate": 0.55, "availability_status": "active"},
        },
        {
            "game_id": "g1",
            "market_id": "receptions",
            "player_name": "Catcher",
            "model_lean": True,
            "book_price": -120,
            "side": "over",
            "line": 4.5,
            "forecast": {"p_estimate": 0.58, "availability_status": "active"},
        },
        {
            "game_id": "g1",
            "market_id": "receptions",
            "player_name": "Other side",
            "model_lean": False,
            "book_price": 110,
            "side": "under",
            "forecast": {"p_estimate": 0.42, "availability_status": "active"},
        },
        {
            "game_id": "g1",
            "market_id": "rushing_yards",
            "player_name": "Soft",
            "model_lean": True,
            "book_price": -400,
            "side": "over",
            "line": 80.5,
            "forecast": {"p_estimate": 0.91, "availability_status": "active"},
        },
    ]
    picked = prop_desk_picks(rows)
    assert [row["player_name"] for row in picked] == ["Catcher", "Soft"]


def test_price_shortlist_drops_a_long_price():
    rows = [
        {
            "game_id": "g9",
            "player_name": "Long",
            "book_price": 2000,
            "forecast": {"p_estimate": 0.23, "availability_status": "active"},
        }
    ]
    assert price_shortlist(rows) == []
