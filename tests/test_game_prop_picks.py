from spe.props.game_picks import build_game_prop_picks


def _row(**overrides):
    row = {
        "game_id": "g1",
        "league_id": "nfl",
        "player_id": "p1",
        "player_name": "Alex",
        "away_abbr": "AAA",
        "home_abbr": "BBB",
        "kickoff_utc": "2026-11-09T23:15:00+00:00",
        "market_id": "anytime_td",
        "side": "yes",
        "line": None,
        "book_price": -110,
        "price_freshness": {"status": "fresh", "is_current_opportunity": True},
        "forecast": {"p_estimate": 0.58, "ev_after_haircut": 0.04, "availability_status": "active"},
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
        _row(player_id="starter", player_name="Starter", book_price=-140, forecast={"p_estimate": 0.62, "ev_after_haircut": 0.03}),
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


def test_a_stale_slate_still_names_a_player():
    rows = [
        _row(price_freshness={"status": "stale", "is_current_opportunity": False}, book_price=-120, forecast={"p_estimate": 0.44, "ev_after_haircut": -0.02}),
    ]
    picks = build_game_prop_picks(rows)
    assert picks[0]["stale"] is True
    assert "The last price is stale." in picks[0]["sentence"]


def test_a_fresh_price_beats_a_stale_one():
    rows = [
        _row(player_id="old", player_name="Old", price_freshness={"status": "stale", "is_current_opportunity": False}, forecast={"p_estimate": 0.7, "ev_after_haircut": 0.2}),
        _row(player_id="live", player_name="Live", forecast={"p_estimate": 0.56, "ev_after_haircut": 0.01}),
    ]
    picks = build_game_prop_picks(rows)
    assert picks[0]["player_name"] == "Live"


def test_names_the_line_when_the_price_is_ahead():
    rows = [
        _row(market_id="receiving_yards", side="over", line=57.5, book_price=-115, forecast={"p_estimate": 0.51, "ev_after_haircut": -0.02}),
    ]
    picks = build_game_prop_picks(rows, {"g1"})
    assert picks[0]["price_ahead"] is True
    assert picks[0]["loud"] is False
    assert picks[0]["sentence"].endswith("The price is ahead of this chance.")


def test_closed_games_are_left_out():
    rows = [_row(game_id="final"), _row(game_id="open")]
    picks = build_game_prop_picks(rows, {"open"})
    assert [pick["game_id"] for pick in picks] == ["open"]
