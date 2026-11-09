from datetime import UTC, datetime

from spe.parlay.card import best_same_game_pair, build_parlay_card


def _row(**overrides):
    row = {
        "outcome_id": "o1",
        "game_id": "g1",
        "league_id": "nfl",
        "player_id": "p1",
        "player_name": "Alex",
        "position": "WR",
        "team": "AAA",
        "opponent": "BBB",
        "away_abbr": "AAA",
        "home_abbr": "BBB",
        "kickoff_utc": "2026-11-08T18:00:00",
        "market_id": "anytime_td",
        "side": "yes",
        "line": None,
        "book_price": -110,
        "price_freshness": {"status": "fresh", "is_current_opportunity": True},
        "forecast": {"p_estimate": 0.55, "ev_after_haircut": 0.04},
    }
    forecast = overrides.pop("forecast", None)
    freshness = overrides.pop("price_freshness", None)
    row.update(overrides)
    if forecast:
        row["forecast"] = {**row["forecast"], **forecast}
    if freshness:
        row["price_freshness"] = freshness
    return row


def test_ruled_out_player_stays_off_the_card():
    rows = [
        _row(
            player_name="A.J. Brown",
            forecast={"p_estimate": 0.55, "ev_after_haircut": 0.08, "availability_status": "out"},
        ),
        _row(
            outcome_id="o2",
            game_id="g2",
            player_id="p2",
            player_name="Other",
            forecast={"p_estimate": 0.55, "ev_after_haircut": 0.08},
        ),
    ]
    names = [leg["player_name"] for ticket in build_parlay_card(rows)["tickets"] for leg in ticket["legs"]]
    assert "A.J. Brown" not in names


def test_stale_short_price_and_soft_number_stay_off():
    rows = [
        _row(outcome_id="stale", price_freshness={"status": "stale", "is_current_opportunity": False}),
        _row(outcome_id="short", game_id="g2", player_id="p2", forecast={"p_estimate": 0.62, "ev_after_haircut": -0.02}),
        _row(outcome_id="soft", game_id="g3", player_id="p3", forecast={"p_estimate": 0.91, "ev_after_haircut": 0.40}),
        _row(outcome_id="spread", game_id="g4", player_id="p4", market_id="point_spread", forecast={"p_estimate": 0.6, "ev_after_haircut": 0.05}),
    ]
    card = build_parlay_card(rows)
    assert card["tickets"] == []
    assert card["eligible_legs"] == 0


def test_best_chance_is_the_product_of_two_real_legs():
    rows = [
        _row(outcome_id="a", game_id="g1", player_id="p1", player_name="Alex", book_price=-110, forecast={"p_estimate": 0.60}),
        _row(outcome_id="b", game_id="g2", player_id="p2", player_name="Blake", market_id="receptions", side="over", line=4.5, kickoff_utc="2026-11-08T21:00:00", book_price=100, forecast={"p_estimate": 0.52}),
    ]
    card = build_parlay_card(rows)
    ticket = card["tickets"][0]
    assert ticket["kind"] == "best_chance"
    assert abs(ticket["joint_probability"] - 0.312) < 1e-9
    assert ticket["price_american"] > 0
    assert [ticket["kind"] for ticket in card["tickets"]] == ["best_chance"]


def test_soft_alternate_loses_to_the_line_near_the_projection():
    rows = [
        _row(
            outcome_id="soft",
            market_id="receptions",
            side="over",
            line=2.5,
            book_price=-140,
            forecast={"p_estimate": 0.84, "ev_after_haircut": 0.20},
        ),
        _row(
            outcome_id="real",
            market_id="receptions",
            side="over",
            line=5.5,
            book_price=110,
            forecast={"p_estimate": 0.57, "ev_after_haircut": 0.06},
        ),
        _row(outcome_id="other", game_id="g2", player_id="p2", market_id="rushing_yards", side="over", line=60.5, forecast={"p_estimate": 0.54}),
    ]
    ticket = build_parlay_card(rows)["tickets"][0]
    assert any(leg["outcome_id"] == "real" for leg in ticket["legs"])
    assert all(leg["outcome_id"] != "soft" for leg in ticket["legs"])
    assert all(leg["p"] <= 0.66 for leg in ticket["legs"])


def test_mix_uses_three_markets_and_skips_a_copy_of_the_pair():
    rows = [
        _row(outcome_id="rec", game_id="g1", player_id="p1", market_id="receptions", side="over", line=4.5, book_price=120, forecast={"p_estimate": 0.64}),
        _row(outcome_id="td", game_id="g2", player_id="p2", market_id="anytime_td", forecast={"p_estimate": 0.60}),
        _row(outcome_id="rush", game_id="g3", player_id="p3", market_id="rushing_yards", side="over", line=60.5, forecast={"p_estimate": 0.56}),
        _row(outcome_id="yards", game_id="g4", player_id="p4", market_id="receiving_yards", side="over", line=55.5, book_price=130, forecast={"p_estimate": 0.52}),
    ]
    card = build_parlay_card(rows)
    mix = next(ticket for ticket in card["tickets"] if ticket["kind"] == "mix")
    assert {leg["market_id"] for leg in mix["legs"]} == {"receptions", "anytime_td", "rushing_yards"}
    pair = next(ticket for ticket in card["tickets"] if ticket["kind"] == "best_chance")
    assert {leg["outcome_id"] for leg in pair["legs"]} != {leg["outcome_id"] for leg in mix["legs"]}


def test_long_shot_uses_plus_money():
    rows = [
        _row(outcome_id="a", game_id="g1", player_id="p1", market_id="receptions", side="over", line=4.5, book_price=140, forecast={"p_estimate": 0.48}),
        _row(outcome_id="b", game_id="g2", player_id="p2", market_id="rushing_yards", side="over", line=70.5, book_price=125, forecast={"p_estimate": 0.44}),
        _row(outcome_id="c", game_id="g3", player_id="p3", market_id="receiving_yards", side="over", line=60.5, book_price=150, forecast={"p_estimate": 0.40}),
        _row(outcome_id="anchor", game_id="g4", player_id="p4", forecast={"p_estimate": 0.58}),
        _row(outcome_id="anchor-b", game_id="g5", player_id="p5", market_id="receptions", side="over", line=5.5, forecast={"p_estimate": 0.55}),
    ]
    long_shot = next(ticket for ticket in build_parlay_card(rows)["tickets"] if ticket["kind"] == "long_shot")
    assert len(long_shot["legs"]) >= 3
    assert all(leg["book_price"] > 0 for leg in long_shot["legs"])
    assert long_shot["joint_probability"] < 0.20


def test_lottery_is_three_long_anytime_prices():
    rows = [
        _row(outcome_id="a", game_id="g1", player_id="p1", player_name="A", book_price=300, forecast={"p_estimate": 0.28, "ev_after_haircut": 0.08}),
        _row(outcome_id="b", game_id="g2", player_id="p2", player_name="B", book_price=350, forecast={"p_estimate": 0.24, "ev_after_haircut": 0.06}),
        _row(outcome_id="c", game_id="g3", player_id="p3", player_name="C", book_price=400, forecast={"p_estimate": 0.32, "ev_after_haircut": 0.10}),
        _row(outcome_id="same", game_id="g1", player_id="p4", player_name="D", book_price=2000, forecast={"p_estimate": 0.20, "ev_after_haircut": 0.40}),
        _row(outcome_id="anchor", game_id="g4", player_id="p5", forecast={"p_estimate": 0.57}),
        _row(outcome_id="anchor-b", game_id="g5", player_id="p6", market_id="receptions", side="over", line=4.5, forecast={"p_estimate": 0.54}),
    ]
    lottery = next(ticket for ticket in build_parlay_card(rows)["tickets"] if ticket["kind"] == "lottery")
    assert len(lottery["legs"]) == 3
    assert len({leg["game_id"] for leg in lottery["legs"]}) == 3
    assert {leg["player_id"] for leg in lottery["legs"]} == {"p1", "p2", "p3"}
    assert lottery["joint_probability"] < 0.05


def test_a_chance_far_above_the_price_stays_off():
    rows = [
        _row(outcome_id="wild", book_price=3000, forecast={"p_estimate": 0.26, "ev_after_haircut": 4.0}),
        _row(outcome_id="ok", game_id="g2", player_id="p2", market_id="receptions", side="over", line=4.5, forecast={"p_estimate": 0.55}),
        _row(outcome_id="ok-b", game_id="g3", player_id="p3", market_id="rushing_yards", side="over", line=50.5, forecast={"p_estimate": 0.54}),
    ]
    legs = [leg for ticket in build_parlay_card(rows)["tickets"] for leg in ticket["legs"]]
    assert all(leg["outcome_id"] != "wild" for leg in legs)


def test_skill_player_beats_a_quarterback_rush_on_the_same_game():
    rows = [
        _row(
            outcome_id="qb",
            player_id="qb",
            player_name="Passer",
            position="QB",
            market_id="rushing_yards",
            side="over",
            line=15.5,
            forecast={"p_estimate": 0.58},
        ),
        _row(
            outcome_id="wr",
            player_id="wr",
            player_name="Wide",
            position="WR",
            market_id="receiving_yards",
            side="over",
            line=55.5,
            forecast={"p_estimate": 0.56},
        ),
        _row(outcome_id="other", game_id="g2", player_id="p2", market_id="receptions", side="over", line=4.5, forecast={"p_estimate": 0.55}),
    ]
    ticket = next(item for item in build_parlay_card(rows)["tickets"] if item["kind"] == "best_chance")
    assert any(leg["outcome_id"] == "wr" for leg in ticket["legs"])
    assert all(leg["outcome_id"] != "qb" for leg in ticket["legs"])


def test_half_catch_stays_off_the_card():
    rows = [
        _row(
            outcome_id="half",
            market_id="receptions",
            side="over",
            line=0.5,
            book_price=-220,
            forecast={"p_estimate": 0.62, "ev_after_haircut": 0.04},
        ),
        _row(
            outcome_id="real",
            market_id="receptions",
            side="over",
            line=3.5,
            book_price=-115,
            forecast={"p_estimate": 0.58, "ev_after_haircut": 0.04},
        ),
        _row(outcome_id="other", game_id="g2", player_id="p2", market_id="rushing_yards", side="over", line=40.5, forecast={"p_estimate": 0.54}),
    ]
    ticket = next(item for item in build_parlay_card(rows)["tickets"] if item["kind"] == "best_chance")
    assert any(leg["outcome_id"] == "real" for leg in ticket["legs"])
    assert all(leg["outcome_id"] != "half" for leg in ticket["legs"])


def test_same_game_pair_uses_the_joint_and_skips_a_soft_quarterback():
    rows = [
        _row(outcome_id="a", game_id="g1", player_id="wr", player_name="Wide", position="WR", forecast={"p_estimate": 0.61}),
        _row(outcome_id="b", game_id="g1", player_id="rb", player_name="Back", position="RB", forecast={"p_estimate": 0.57}),
        _row(outcome_id="qb", game_id="g1", player_id="qb", player_name="Passer", position="QB", forecast={"p_estimate": 0.90}),
        _row(outcome_id="c", game_id="g2", player_id="p2", market_id="receptions", side="over", line=4.5, forecast={"p_estimate": 0.52}),
    ]
    assert best_same_game_pair(rows) == ("g1", "wr", "rb")
    without = build_parlay_card(rows)
    assert all(ticket["kind"] != "same_game" for ticket in without["tickets"])
    with_joint = build_parlay_card(rows, joints={("g1", "rb", "wr"): 0.22})
    same = next(ticket for ticket in with_joint["tickets"] if ticket["kind"] == "same_game")
    assert same["joint_probability"] == 0.22
    assert same["price_american"] is None
    assert {leg["player_id"] for leg in same["legs"]} == {"wr", "rb"}


def test_anytime_tickets_include_a_six_player_long_shot_and_skip_an_out():
    rows = []
    for index in range(6):
        rows.append(
            _row(
                outcome_id=f"star{index}",
                game_id=f"g{index}",
                player_id=f"star{index}",
                player_name=f"Star {index}",
                book_price=-150,
                forecast={"p_estimate": 0.50 - index * 0.02, "ev_after_haircut": -0.04},
            )
        )
        rows.append(
            _row(
                outcome_id=f"long{index}",
                game_id=f"g{index}",
                player_id=f"long{index}",
                player_name=f"Long {index}",
                book_price=220,
                forecast={"p_estimate": 0.28 - index * 0.01, "ev_after_haircut": -0.02},
            )
        )
    rows.append(
        _row(
            outcome_id="out",
            game_id="g9",
            player_id="out",
            player_name="A.J. Brown",
            book_price=180,
            forecast={"p_estimate": 0.40, "availability_status": "out"},
        )
    )
    rows.append(
        _row(
            outcome_id="wild",
            game_id="g8",
            player_id="wild",
            player_name="Wild",
            book_price=3000,
            forecast={"p_estimate": 0.26},
        )
    )
    card = build_parlay_card(rows)
    pair = next(item for item in card["tickets"] if item["kind"] == "anytime")
    trio = next(item for item in card["tickets"] if item["kind"] == "anytime_three")
    six = next(item for item in card["tickets"] if item["kind"] == "anytime_long")
    assert [leg["player_name"] for leg in pair["legs"]] == ["Star 0", "Star 1"]
    assert len(trio["legs"]) == 3
    assert len(six["legs"]) == 6
    assert {leg["player_name"] for leg in six["legs"]} == {f"Long {index}" for index in range(6)}
    assert abs(six["joint_probability"] - (0.28 * 0.27 * 0.26 * 0.25 * 0.24 * 0.23)) < 1e-9
    names = [leg["player_name"] for ticket in card["tickets"] for leg in ticket["legs"]]
    assert "A.J. Brown" not in names
    assert "Wild" not in names


def test_a_recent_price_joins_anytime_after_the_fresh_window():
    now = datetime(2026, 11, 8, 16, 0, tzinfo=UTC)
    rows = [
        _row(
            outcome_id="a",
            game_id="g1",
            player_id="p1",
            player_name="Later",
            kickoff_utc="2026-11-08T18:00:00+00:00",
            book_price=-140,
            price_freshness={"status": "stale", "is_current_opportunity": False, "age_minutes": 55},
            forecast={"p_estimate": 0.62, "ev_after_haircut": -0.02},
        ),
        _row(
            outcome_id="b",
            game_id="g2",
            player_id="p2",
            player_name="Also Later",
            kickoff_utc="2026-11-08T21:00:00+00:00",
            book_price=150,
            price_freshness={"status": "stale", "is_current_opportunity": False, "age_minutes": 55},
            forecast={"p_estimate": 0.40, "ev_after_haircut": -0.02},
        ),
        _row(
            outcome_id="started",
            game_id="g3",
            player_id="p3",
            player_name="Already Going",
            kickoff_utc="2026-11-08T14:30:00+00:00",
            book_price=-120,
            price_freshness={"status": "stale", "is_current_opportunity": False, "age_minutes": 55},
            forecast={"p_estimate": 0.70},
        ),
        _row(
            outcome_id="old",
            game_id="g4",
            player_id="p4",
            player_name="Old Price",
            kickoff_utc="2026-11-08T21:30:00+00:00",
            book_price=-110,
            price_freshness={"status": "stale", "is_current_opportunity": False, "age_minutes": 400},
            forecast={"p_estimate": 0.55},
        ),
    ]
    names = [
        leg["player_name"]
        for ticket in build_parlay_card(rows, now=now)["tickets"]
        if ticket["kind"] == "anytime"
        for leg in ticket["legs"]
    ]
    assert names == ["Later", "Also Later"]


def test_spread_tickets_use_the_model_side_and_skip_a_started_game():
    now = datetime(2026, 11, 8, 16, 0, tzinfo=UTC)
    spreads = [
        _spread("nfl:2026_09_CIN_ATL", "2026-11-08T14:30:00+00:00", "Cincinnati Bengals", "Atlanta Falcons"),
        _spread("nfl:2026_09_DEN_CAR", "2026-11-08T18:00:00+00:00", "Denver Broncos", "Carolina Panthers", chance=0.54),
        _spread("nfl:2026_09_DAL_IND", "2026-11-08T18:05:00+00:00", "Dallas Cowboys", "Indianapolis Colts", chance=0.55),
        _spread("nfl:2026_09_NYJ_KC", "2026-11-08T18:10:00+00:00", "New York Jets", "Kansas City Chiefs", chance=0.56, withheld=True),
        _spread("nfl:2026_09_DET_MIA", "2026-11-08T21:25:00+00:00", "Detroit Lions", "Miami Dolphins", chance=0.52),
        _spread("nfl:2026_09_CLE_NO", "2026-11-08T21:30:00+00:00", "Cleveland Browns", "New Orleans Saints", chance=0.53),
        _spread("nfl:2026_09_NYG_PHI", "2026-11-08T21:35:00+00:00", "New York Giants", "Philadelphia Eagles", chance=0.51),
    ]
    card = build_parlay_card([], spreads=spreads, now=now)
    two = next(item for item in card["tickets"] if item["kind"] == "spread")
    three = next(item for item in card["tickets"] if item["kind"] == "spread_three")
    assert [leg["player_name"] for leg in two["legs"]] == ["Denver Broncos", "Dallas Cowboys"]
    assert two["legs"][0]["line"] == -3.5
    assert two["legs"][0]["team"] == "DEN"
    assert abs(two["joint_probability"] - 0.54 * 0.55) < 1e-9
    assert [leg["team"] for leg in three["legs"]] == ["DET", "CLE", "NYG"]
    assert all("Kansas City" not in leg["player_name"] for ticket in card["tickets"] for leg in ticket["legs"])
    assert all("Cincinnati" not in leg["player_name"] for ticket in card["tickets"] for leg in ticket["legs"])


def _spread(game_id, kickoff, away, home, *, chance=0.54, withheld=False):
    return {
        "game_id": game_id,
        "league_id": "nfl",
        "game_status": "scheduled",
        "kickoff_utc": kickoff,
        "favored_side": "away",
        "favored_side_line": -3.5,
        "p_away_cover": chance,
        "p_home_cover": 0.43,
        "away_spread_price": -110,
        "home_spread_price": -110,
        "away_team": away,
        "home_team": home,
        "projection_withheld": withheld,
    }
