"""Unit / integration tests for Phase 4 correlated Anytime TD simulation."""

from spe.simulate.correlated_atd.engine import simulate_correlated_atd
from spe.simulate.correlated_atd.validate import run_mechanics_suite, synthetic_fixture


def test_mechanics_suite_passes():
    out = run_mechanics_suite(draws=3_000, seed=3)
    assert out["checks"]["all_passed"] is True
    assert out["checks"]["team_equals_allocated_plus_other"] is True
    assert out["checks"]["reproducible_fixed_seed"] is True


def test_qb_receiving_weight_zero_and_probs_bounded():
    opps, ctx, home, away = synthetic_fixture()
    for o in opps:
        if o.position_group == "QB":
            assert o.rec_weight == 0.0
    result = simulate_correlated_atd("nfl", "g1", home, away, opps, ctx, draw_count=2_000, seed=9)
    for p in result.players:
        assert 0.0 <= p.p_anytime_td <= 1.0
        assert p.mean_tds >= 0.0
        if p.position_group == "QB":
            assert p.p_rec_td == 0.0


def test_teammate_joint_differs_from_independent_product():
    opps, ctx, home, away = synthetic_fixture()
    result = simulate_correlated_atd("nfl", "g1", home, away, opps, ctx, draw_count=5_000, seed=1)
    rb = next(p for p in result.players if p.player_id == "rb1")
    wr = next(p for p in result.players if p.player_id == "wr1")
    joint = result.joint_same_team.get("rb1|wr1") or result.joint_td["rb1|wr1"]
    naive = rb.p_anytime_td * wr.p_anytime_td
    assert abs(joint - naive) > 0.002
