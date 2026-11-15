from datetime import UTC, datetime, timedelta

from spe.props.game_picks import build_game_prop_picks, select_desk_props


def _row(**overrides):
    row = {
        "game_id": "g1",
        "league_id": "nfl",
        "player_id": "p1",
        "player_name": "Alex",
        "away_abbr": "AAA",
        "home_abbr": "BBB",
        "kickoff_utc": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "outcome_id": "o1", "bookmaker": "Book", "data_tier": "upcoming",
        "market_id": "anytime_td",
        "side": "yes",
        "line": None,
        "book_price": -110,
        "price_freshness": {"status": "fresh", "is_current_opportunity": True, "observed_at": datetime.now(UTC).isoformat()},
        "forecast": {"p_estimate": 0.58, "ev_after_haircut": 0.04, "availability_status": "active", "model_version": "m1", "prediction_as_of": datetime.now(UTC).isoformat(), "data_cutoff": (datetime.now(UTC)-timedelta(minutes=1)).isoformat()},
    }
    forecast = overrides.pop("forecast", None)
    freshness = overrides.pop("price_freshness", None)
    row.update(overrides)
    if forecast:
        row["forecast"] = {**row["forecast"], **forecast}
    if freshness:
        row["price_freshness"] = freshness
    return row


def test_one_anytime_call_per_game_prefers_the_real_price():
    rows = [
        _row(player_id="backup", player_name="Backup", book_price=3000, forecast={"p_estimate": 0.26, "ev_after_haircut": 0.2}),
        _row(player_id="starter", player_name="Starter", book_price=-140, forecast={"p_estimate": 0.62, "ev_after_haircut": 0.06}),
    ]
    picks = build_game_prop_picks(rows)
    assert len(picks) == 1
    assert picks[0]["player_name"] == "Starter"
    assert picks[0]["loud"] is True
    assert "Starter anytime touchdown, about 62 times in 100, pays -140." == picks[0]["sentence"]


def test_soft_lines_and_catch_floors_stay_off():
    rows = [
        _row(player_id="soft", market_id="rushing_yards", side="over", line=10.5, forecast={"p_estimate": 0.91, "ev_after_haircut": 0.2}),
        _row(player_id="floor", market_id="receptions", side="over", line=0.5, forecast={"p_estimate": 0.6, "ev_after_haircut": 0.05}),
    ]
    assert build_game_prop_picks(rows) == []


def test_a_stale_slate_has_no_recommendation():
    assert build_game_prop_picks([_row(price_freshness={"status": "stale", "is_current_opportunity": False})]) == []


def test_a_fresh_price_beats_a_stale_one():
    rows = [
        _row(player_id="old", player_name="Old", price_freshness={"status": "stale", "is_current_opportunity": False}, forecast={"p_estimate": 0.7, "ev_after_haircut": 0.2}),
        _row(player_id="live", player_name="Live", forecast={"p_estimate": 0.56, "ev_after_haircut": 0.05}),
    ]
    picks = build_game_prop_picks(rows)
    assert picks[0]["player_name"] == "Live"


def test_negative_value_is_not_recommended():
    assert build_game_prop_picks([_row(forecast={"ev_after_haircut": -0.02})]) == []


def test_desk_props_take_the_most_confident_side_and_skip_touchdowns():
    rows = [
        _row(market_id="anytime_td", player_name="Scorer", model_lean=True, forecast={"p_estimate": 0.9}),
        _row(
            market_id="receptions",
            side="over",
            line=3.5,
            player_id="soft",
            player_name="Soft",
            outcome_id="soft",
            model_lean=True,
            forecast={"p_estimate": 0.84},
        ),
        _row(
            market_id="rushing_yards",
            side="over",
            line=60.5,
            player_id="rusher",
            player_name="Rusher",
            outcome_id="rush",
            game_id="g2",
            model_lean=True,
            forecast={"p_estimate": 0.61},
        ),
    ]
    picks = select_desk_props(rows)
    chosen = {(pick["game_id"], pick["player_name"]) for pick in picks}
    assert chosen == {("g1", "Soft"), ("g2", "Rusher")}


def test_desk_props_leave_a_ninety_percent_line_for_the_next_chance():
    rows = [
        _row(
            market_id="rushing_yards",
            side="under",
            line=126.5,
            player_id="lock",
            player_name="Lock",
            outcome_id="lock",
            model_lean=True,
            forecast={"p_estimate": 0.96},
        ),
        _row(
            market_id="receiving_yards",
            side="over",
            line=52.5,
            player_id="next",
            player_name="Next",
            outcome_id="next",
            model_lean=True,
            forecast={"p_estimate": 0.74},
        ),
    ]
    picks = select_desk_props(rows)
    assert len(picks) == 1
    assert picks[0]["player_name"] == "Next"


def test_desk_props_take_the_clear_favorite_and_skip_a_close_game():
    rows = [
        _row(
            market_id="receiving_yards",
            side="over",
            line=40.5,
            player_name="Dog",
            player_id="dog",
            outcome_id="dog",
            model_lean=True,
            team_spread=10.5,
            forecast={"p_estimate": 0.86},
        ),
        _row(
            market_id="rushing_yards",
            side="under",
            line=70.5,
            player_name="Favorite",
            player_id="fav",
            outcome_id="fav",
            model_lean=True,
            team_spread=-14,
            forecast={"p_estimate": 0.72},
        ),
        _row(
            game_id="g2",
            market_id="receptions",
            side="under",
            line=3.5,
            player_name="TossUp",
            player_id="toss",
            outcome_id="toss",
            model_lean=True,
            team_spread=-3,
            forecast={"p_estimate": 0.8},
        ),
    ]
    picks = select_desk_props(rows)
    assert [pick["player_name"] for pick in picks] == ["Favorite"]


def test_desk_props_keep_one_call_per_game():
    rows = [
        _row(
            market_id="receiving_yards",
            side="over",
            line=40.5,
            player_id="a",
            player_name="Closer",
            outcome_id="a",
            model_lean=True,
            forecast={"p_estimate": 0.57},
        ),
        _row(
            market_id="receiving_yards",
            side="over",
            line=55.5,
            player_id="b",
            player_name="Other",
            outcome_id="b",
            model_lean=True,
            forecast={"p_estimate": 0.52},
        ),
    ]
    picks = select_desk_props(rows)
    assert len(picks) == 1
    assert picks[0]["player_name"] == "Closer"


def test_closed_games_are_left_out():
    rows = [_row(game_id="final"), _row(game_id="open")]
    picks = build_game_prop_picks(rows, {"open"})
    assert [pick["game_id"] for pick in picks] == ["open"]
