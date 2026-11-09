from datetime import UTC, datetime

from spe.models.prop_projection import (
    PlayedGame,
    mean_before,
    model_lean,
    over_probability,
    pick_sentence,
    projection_mean,
    side_probability,
)


def _game(day: int, rush: float) -> PlayedGame:
    return PlayedGame(
        kickoff=datetime(2026, 10, day, tzinfo=UTC),
        receptions=3,
        rec_yards=40,
        rush_yards=rush,
    )


def test_pick_sentence_names_side_line_and_market():
    assert pick_sentence("Jahmyr Gibbs", "under", 95.5, "rushing_yards") == "Jahmyr Gibbs under 95.5 rushing yards"
    assert pick_sentence("Ja'Marr Chase", "yes", None, "anytime_td") == "Ja'Marr Chase anytime touchdown"


def test_recent_mean_ignores_the_game_itself_and_later_games():
    games = [
        _game(20, 150),
        _game(11, 100),
        _game(5, 50),
    ]
    # Newest first, matching the loader.
    games = sorted(games, key=lambda game: game.kickoff, reverse=True)
    kick = datetime(2026, 10, 11, tzinfo=UTC)
    mean, n = mean_before(games, kick, "rush_yards", window=16)
    assert n == 1
    assert mean == 50


def test_eight_played_games_drop_the_stale_model_mean():
    mean, source = projection_mean(90.0, 8, 42.0)
    assert source == "recent"
    assert mean == 90.0


def test_thin_sample_stays_near_the_model():
    mean, source = projection_mean(100.0, 2, 40.0)
    assert source == "recent"
    assert abs(mean - (2 / 6 * 100 + 4 / 6 * 40)) < 1e-9


def test_star_rusher_is_not_a_two_percent_over():
    # 90 yards recent, 95.5 line, residual scale from the yards model.
    p_over = over_probability("rushing_yards", 90.3, 95.5, 27.45)
    p_under, _, _ = side_probability(p_over, "under")
    assert 0.3 < p_over < 0.5
    assert p_under > 0.5
    assert model_lean(p_under) is True
    assert model_lean(p_over) is False
