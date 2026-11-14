"""Desk chooses a supported scorer only at a current, qualifying research price."""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
import pytest

from spe.shadow.atd_desk_takes import frozen_desk_row, select_desk_takes


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
    now = datetime.now(UTC)
    freshness = {
        "status": "fresh" if fresh else "stale",
        "is_current_opportunity": fresh,
        "observed_at": now.isoformat() if fresh else (now - timedelta(hours=3)).isoformat(),
    }
    return {
        "game_id": game_id,
        "player_id": player,
        "player_name": player,
        "outcome_id": player,
        "bookmaker": "test",
        "kickoff_utc": (now + timedelta(hours=1)).isoformat(),
        "usage": {"gl_carries": 1.0, "carries": 5.0},
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
        _row(game_id="g2", player="c", p=0.35, price=250, ev_hc=0.06),
    ]
    takes = select_desk_takes(rows)
    assert len(takes) == 2
    assert {t["player_name"] for t in takes} == {"a", "c"}


def test_a_short_price_removes_a_scorer_without_estimated_value():
    high_p = _row(game_id="g1", player="star", p=0.48, price=-200, ev_hc=-0.02)
    value = _row(game_id="g1", player="value", p=0.36, price=250, ev_hc=0.11)
    takes = select_desk_takes([high_p, value])
    assert len(takes) == 1
    assert takes[0]["player_name"] == "value"


def test_out_players_never_selected():
    rows = [
        _row(game_id="g1", player="out", p=0.55, price=120, ev_hc=0.15, avail="out"),
        _row(game_id="g1", player="in", p=0.36, price=250, ev_hc=0.08),
    ]
    takes = select_desk_takes(rows)
    assert len(takes) == 1
    assert takes[0]["player_name"] == "in"


def test_a_longshot_does_not_replace_the_scorer():
    favorite = _row(game_id="g1", player="back", p=0.48, price=150, ev_hc=0.02)
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


def test_old_frozen_chances_without_verified_workload_do_not_create_new_calls():
    star = _frozen("g1", "star", 0.48, -200)
    value = _frozen("g1", "value", 0.36, 180)
    takes = select_desk_takes([frozen_desk_row(star), frozen_desk_row(value)])
    assert takes == []


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


def test_unsettled_flag_moves_to_the_board_call(db):
    from datetime import UTC, datetime, timedelta

    from spe.db.models import Game, League, Season, ShadowPrediction, Sport, Team, Venue
    from spe.shadow.atd_desk_takes import assign_unsettled_board_call, is_desk_take

    kick = datetime(2026, 11, 10, 1, 15, tzinfo=UTC)
    db.add(Sport(sport_id="football", name="Football"))
    db.flush()
    db.add(League(league_id="nfl", sport_id="football", name="NFL", classification="pro"))
    db.flush()
    db.add(
        Season(
            season_id="nfl-2026",
            league_id="nfl",
            year=2026,
            start_at=kick - timedelta(days=30),
            end_at=kick + timedelta(days=60),
        )
    )
    db.add(Venue(venue_id="v", name="Field", lat=0.0, lon=0.0, roof_type="open", surface="grass"))
    db.add(Team(team_id="nfl:KC", league_id="nfl", name="Chiefs", abbreviation="KC"))
    db.add(Team(team_id="nfl:BAL", league_id="nfl", name="Ravens", abbreviation="BAL"))
    db.flush()
    called = kick - timedelta(hours=1)
    frozen = kick - timedelta(days=2)
    db.add(
        Game(
            game_id="g-buf",
            season_id="nfl-2026",
            league_id="nfl",
            week=9,
            kickoff_utc=kick,
            home_team_id="nfl:KC",
            away_team_id="nfl:BAL",
            venue_id="v",
            status="final",
            data_tier="historical",
        )
    )
    db.flush()

    def shadow(shadow_id: str, player_id: str, desk: bool) -> None:
        db.add(
            ShadowPrediction(
                shadow_id=shadow_id,
                player_id=player_id,
                game_id="g-buf",
                league_id="nfl",
                market_id="anytime_td",
                outcome_id=f"out-{shadow_id}",
                bookmaker_id="draftkings",
                line=None,
                side="yes",
                price_american=-110,
                odds_tick_id=None,
                forecast_probability=0.4,
                market_implied_raw=0.52,
                market_implied_novig=None,
                expected_value=None,
                model_version_id="player_td_v1-nfl-anytime_td",
                forecast_as_of=frozen,
                data_quality_status="ok",
                abstain_reasons=[],
                recommendation_status="no_bet",
                components={"desk_take": "game_scorer"} if desk else {"player_name": player_id},
                research_only=True,
                play_enabled=False,
                created_at=frozen,
            )
        )

    shadow("jones", "jones", True)
    shadow("cook", "cook", False)
    db.flush()
    moved = assign_unsettled_board_call(
        db,
        game_id="g-buf",
        player_id="cook",
        called_at=called,
        board_p=0.61,
    )
    assert moved["status"] == "ok"
    assert moved["shadow_id"] == "cook"
    assert moved["cleared"] == ["jones"]
    db.flush()
    db.expire_all()
    assert is_desk_take(db.get(ShadowPrediction, "cook"))
    assert not is_desk_take(db.get(ShadowPrediction, "jones"))
    assert db.get(ShadowPrediction, "cook").components["board_p"] == 0.61


def test_stale_prices_do_not_create_value_picks():
    rows = [
        _row(game_id="g1", player="a", p=0.42, price=160, ev_hc=0.01, fresh=False),
        _row(game_id="g1", player="b", p=0.31, price=220, ev_hc=0.09, fresh=False),
    ]
    takes = select_desk_takes(rows)
    assert takes == []


@pytest.mark.parametrize("price", [None, 0, 99, float("nan"), float("inf")])
def test_invalid_or_missing_price_cannot_qualify(price):
    r = _row(game_id="g", player="p", p=.6, price=price, ev_hc=.1)
    assert select_desk_takes([r]) == []


def test_scoring_work_can_qualify_without_a_recent_touchdown():
    r = _row(game_id="g", player="p", p=.6, price=150, ev_hc=.1)
    for usage in ({"gl_carries": .7, "rush_td": 0}, {"carries": 8, "rush_td": 0},
                  {"receptions": 4, "rec_td": 0}):
        r["usage"] = usage
        assert len(select_desk_takes([r])) == 1
    r["usage"] = {"carries": .3, "rush_td": 1, "receptions": .2}
    assert select_desk_takes([r]) == []


def test_cached_fresh_flag_does_not_extend_a_quote():
    r = _row(game_id="g", player="p", p=.6, price=150, ev_hc=.1)
    r["price_freshness"]["observed_at"] = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    assert select_desk_takes([r]) == []
