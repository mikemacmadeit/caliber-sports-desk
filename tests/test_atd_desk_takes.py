"""Desk picks one player per game via multifactor score — not max % alone."""

from __future__ import annotations

from spe.shadow.atd_desk_takes import desk_pick_score, frozen_desk_row, select_desk_takes


def _row(
    *,
    game_id: str,
    player: str,
    p: float,
    price: int,
    ev_hc: float,
    fresh: bool = True,
    avail: str = "active",
) -> dict:
    freshness = {
        "status": "fresh" if fresh else "stale",
        "is_current_opportunity": fresh,
    }
    return {
        "game_id": game_id,
        "player_id": player,
        "player_name": player,
        "book_price": price,
        "price_freshness": freshness,
        "forecast": {
            "p_estimate": p,
            "ev_after_haircut": ev_hc,
            "availability_status": avail,
            "price_freshness": freshness,
        },
    }


def test_selects_one_per_game():
    rows = [
        _row(game_id="g1", player="a", p=0.40, price=180, ev_hc=0.02),
        _row(game_id="g1", player="b", p=0.32, price=220, ev_hc=0.10),
        _row(game_id="g2", player="c", p=0.35, price=210, ev_hc=0.06),
    ]
    takes = select_desk_takes(rows)
    assert len(takes) == 2
    assert {t["player_name"] for t in takes} == {"a", "c"}


def test_a_short_price_does_not_remove_the_likely_scorer():
    """The pick is who scores. A short number stays a price question, not a veto."""
    high_p = _row(game_id="g1", player="star", p=0.48, price=-200, ev_hc=-0.02)
    value = _row(game_id="g1", player="value", p=0.36, price=180, ev_hc=0.11)
    takes = select_desk_takes([high_p, value])
    assert len(takes) == 1
    assert takes[0]["player_name"] == "star"


def test_out_players_never_selected():
    rows = [
        _row(game_id="g1", player="out", p=0.55, price=120, ev_hc=0.15, avail="out"),
        _row(game_id="g1", player="in", p=0.36, price=180, ev_hc=0.08),
    ]
    takes = select_desk_takes(rows)
    assert len(takes) == 1
    assert takes[0]["player_name"] == "in"


def test_a_longshot_does_not_replace_the_scorer():
    favorite = _row(game_id="g1", player="back", p=0.48, price=-150, ev_hc=-0.02)
    longshot = _row(game_id="g1", player="flyer", p=0.08, price=2500, ev_hc=0.40)
    takes = select_desk_takes([favorite, longshot])
    assert len(takes) == 1
    assert takes[0]["player_name"] == "back"


def test_no_pick_when_nobody_has_a_real_chance():
    rows = [
        _row(game_id="g1", player="scratch", p=0.02, price=2500, ev_hc=0.1),
        _row(game_id="g1", player="camp", p=0.03, price=1800, ev_hc=0.05),
    ]
    assert select_desk_takes(rows) == []


def test_frozen_row_picks_the_highest_chance():
    star = _frozen("g1", "star", 0.48, -200)
    value = _frozen("g1", "value", 0.36, 180)
    takes = select_desk_takes([frozen_desk_row(star), frozen_desk_row(value)])
    assert takes[0]["player_name"] == "star"


def _frozen(game_id: str, name: str, p: float, price: int):
    return type(
        "Pred",
        (),
        {
            "game_id": game_id,
            "player_id": name,
            "player_name": name,
            "forecast_probability": p,
            "price_american": price,
            "forecast_as_of": None,
            "shadow_id": name,
            "components": {"player_name": name},
        },
    )()


def test_stale_price_still_gets_a_pick():
    """A stale number can still be the pick when our chance beats it."""
    rows = [
        _row(game_id="g1", player="a", p=0.42, price=160, ev_hc=0.01, fresh=False),
        _row(game_id="g1", player="b", p=0.31, price=220, ev_hc=0.09, fresh=False),
    ]
    takes = select_desk_takes(rows)
    assert len(takes) == 1
    assert takes[0]["game_id"] == "g1"
