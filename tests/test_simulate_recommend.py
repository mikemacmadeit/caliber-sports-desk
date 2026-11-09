from datetime import UTC, datetime

from spe.eval.leakage import LeakageError, assert_event_time
from spe.models.hierarchical import HierarchicalCountModel, PlayerObservation
from spe.recommend.engine import RecommendInput, recommend
from spe.simulate.engine import PlayerRole, simulate_game
from spe.simulate.rules import rules_for


def test_league_rules_are_not_shared():
    assert rules_for("nfl").overtime != rules_for("fbs").overtime


def test_hierarchical_models_are_league_specific():
    nfl = HierarchicalCountModel("nfl", "td")
    fbs = HierarchicalCountModel("fbs", "td")
    rows_nfl = [
        PlayerObservation("p1", "kc", None, "WR", "nfl", 8, 4, 8, 1.0),
        PlayerObservation("p2", "buf", None, "RB", "nfl", 8, 6, 8, 1.0),
    ]
    rows_fbs = [
        PlayerObservation("c1", "osu", "Big Ten", "WR", "fbs", 3, 1, 3, 1.0),
        PlayerObservation("c2", "ala", "SEC", "RB", "fbs", 4, 3, 4, 1.0),
    ]
    nfl.fit(rows_nfl)
    fbs.fit(rows_fbs)
    assert nfl.k_player != fbs.k_player
    pred = fbs.predict_rate("unknown", "mich", "Big Ten", "WR", availability=0.9)
    assert pred.mean > 0
    assert pred.shrinkage == 0.0


def test_simulation_joint_not_independent_product():
    players = [
        PlayerRole("a", "kc", "WR", 1.0, 0.9, 0.4, 6, 70, 2),
        PlayerRole("b", "kc", "TE", 1.0, 0.8, 0.35, 5, 55, 1),
        PlayerRole("c", "buf", "RB", 1.0, 0.7, 0.4, 3, 20, 80),
    ]
    sim = simulate_game("nfl", "kc", "buf", players, draw_count=4000, seed=1)
    joint = sim.joint_td["a|b"]
    naive = sim.player_td_prob["a"] * sim.player_td_prob["b"]
    # Teammate TDs compete; joint should not equal the independent product.
    assert abs(joint - naive) > 0.005
    assert sim.convergence["max_td_mc_se"] < 0.02


def test_recommend_abstains_on_missing_injury():
    now = datetime(2026, 11, 6, 18, tzinfo=UTC)
    result = recommend(
        RecommendInput(
            p=0.5,
            p_low=0.4,
            p_high=0.6,
            american=150,
            price_captured_at=now,
            game_status="scheduled",
            availability_status="missing_report",
            availability_prob=0.9,
            injury_confidence=None,
            source_fresh=True,
            grading_supported=True,
            calibration_n=200,
        ),
        now=now,
    )
    assert result.status == "no_bet"
    assert "NEVER_ASSUME_HEALTHY" in result.reason_codes


def test_recommend_plays_blueprint_example():
    now = datetime(2026, 11, 6, 18, tzinfo=UTC)
    result = recommend(
        RecommendInput(
            p=0.44,
            p_low=0.40,
            p_high=0.48,
            american=180,
            price_captured_at=now,
            game_status="scheduled",
            availability_status="active",
            availability_prob=0.96,
            injury_confidence=0.9,
            source_fresh=True,
            grading_supported=True,
            calibration_n=220,
        ),
        now=now,
    )
    assert result.status == "play"
    assert result.ev is not None and result.ev > 0.2


def test_leakage_rejects_future_observed_at():
    as_of = datetime(2026, 11, 6, tzinfo=UTC)
    future = datetime(2026, 11, 7, tzinfo=UTC)
    try:
        assert_event_time(as_of=as_of, observed_at=future)
        raise AssertionError("expected LeakageError")
    except LeakageError:
        pass
