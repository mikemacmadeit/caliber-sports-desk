"""Desk picks one player per game: scoring touches, and only ahead of the price."""

from __future__ import annotations

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
    gl: float = 0,
    rz: float = 0,
    rush_td: float = 0,
    rec_td: float = 0,
    carries: float = 0,
    targets: float = 0,
    spread: float | None = None,
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
        "team_spread": spread,
        "price_freshness": freshness,
        "usage": {
            "gl_carries": gl,
            "rz_targets": rz,
            "rush_td": rush_td,
            "rec_td": rec_td,
            "carries": carries,
            "targets": targets,
        },
        "forecast": {
            "p_estimate": p,
            "ev_after_haircut": ev_hc,
            "availability_status": avail,
            "price_freshness": freshness,
        },
    }


def test_selects_one_per_game():
    rows = [
        _row(game_id="g1", player="a", p=0.46, price=150, ev_hc=0.02, rush_td=0.7, carries=10),
        _row(game_id="g1", player="b", p=0.32, price=220, ev_hc=0.10, targets=1.3, rec_td=0.17),
        _row(game_id="g2", player="c", p=0.44, price=160, ev_hc=0.06, rush_td=0.6, carries=8),
    ]
    takes = select_desk_takes(rows)
    assert len(takes) == 2
    assert {t["player_name"] for t in takes} == {"a", "c"}


def test_a_short_favorite_loses_to_the_price_that_has_not_paid():
    """-200 already prices the star above our chance. The plus number is the pick."""
    high_p = _row(game_id="g1", player="star", p=0.55, price=-220, ev_hc=-0.02, rush_td=0.8, carries=14)
    value = _row(game_id="g1", player="value", p=0.42, price=170, ev_hc=0.11, rush_td=0.6, carries=8)
    takes = select_desk_takes([high_p, value])
    assert len(takes) == 1
    assert takes[0]["player_name"] == "value"


def test_out_players_never_selected():
    rows = [
        _row(game_id="g1", player="out", p=0.55, price=120, ev_hc=0.15, avail="out", rush_td=0.8, carries=12),
        _row(game_id="g1", player="in", p=0.46, price=150, ev_hc=0.08, rush_td=0.6, carries=8),
    ]
    takes = select_desk_takes(rows)
    assert len(takes) == 1
    assert takes[0]["player_name"] == "in"


def test_a_longshot_does_not_replace_the_scorer():
    favorite = _row(game_id="g1", player="back", p=0.50, price=-150, ev_hc=-0.02)
    longshot = _row(game_id="g1", player="flyer", p=0.08, price=2500, ev_hc=0.40)
    assert select_desk_takes([favorite, longshot]) == []


def test_goal_line_work_beats_a_bigger_price_gap():
    chalk_gap = _row(game_id="g1", player="flyer", p=0.30, price=400, ev_hc=0.12)
    goal = _row(game_id="g1", player="back", p=0.46, price=140, ev_hc=0.04, gl=1.5)
    takes = select_desk_takes([chalk_gap, goal])
    assert len(takes) == 1
    assert takes[0]["player_name"] == "back"


def test_a_four_touchdown_dog_does_not_keep_his_season_rate():
    """BC +29.5 cannot stay a 56% scorer. Notre Dame at -300 is already shorter than our number."""
    dog = _row(game_id="g1", player="bc", p=0.56, price=280, ev_hc=0.2, rush_td=0.8, carries=11, spread=29.5)
    favorite = _row(game_id="g1", player="nd", p=0.61, price=-300, ev_hc=-0.05, rush_td=1.3, carries=13, spread=-29.5)
    assert select_desk_takes([dog, favorite]) == []


def test_a_long_price_without_scoring_work_is_not_a_pick():
    """The favorite is fairly priced. The plus-money name with one target a game is not the replacement."""
    favorite = _row(game_id="g1", player="back", p=0.60, price=-165, ev_hc=-0.02, rush_td=0.5, carries=12)
    longshot = _row(game_id="g1", player="walker", p=0.33, price=900, ev_hc=0.20, targets=1.3, rec_td=0.17)
    assert select_desk_takes([favorite, longshot]) == []


def test_no_pick_when_nobody_has_a_real_chance():
    rows = [
        _row(game_id="g1", player="scratch", p=0.02, price=2500, ev_hc=0.1),
        _row(game_id="g1", player="camp", p=0.03, price=1800, ev_hc=0.05),
    ]
    assert select_desk_takes(rows) == []


def test_frozen_row_without_scoring_work_is_not_a_pick():
    star = _frozen("g1", "star", 0.48, -200)
    value = _frozen("g1", "value", 0.44, 160)
    assert select_desk_takes([frozen_desk_row(star), frozen_desk_row(value)]) == []


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


def test_stale_price_still_gets_a_pick():
    """A stale number can still be the pick when our chance beats it."""
    rows = [
        _row(game_id="g1", player="a", p=0.45, price=160, ev_hc=0.01, fresh=False, rush_td=0.6, carries=9),
        _row(game_id="g1", player="b", p=0.31, price=220, ev_hc=0.09, fresh=False, targets=1.2, rec_td=0.1),
    ]
    takes = select_desk_takes(rows)
    assert len(takes) == 1
    assert takes[0]["game_id"] == "g1"
