"""Live weekend shadow ops — dry run and helpers."""

from spe.shadow.dry_run import run_settlement_dry_run
from spe.shadow.live_weekend import family_of
from spe.shadow.report import _family


def test_family_detection_matches():
    assert family_of("m5-nfl-anytime_td") == "m5"
    assert family_of("atd_sim_v1-fbs-anytime_td") == "atd_sim_v1"
    assert _family("m6-nfl-anytime_td") == "m6"
    assert _family("blend_v1_c_recent-fbs-anytime_td") == "blend_v1_c_recent"
    assert _family("m5_c_recent-fbs-anytime_td") == "m5_c_recent"
    assert _family("player_td_v1-fbs-anytime_td") == "player_td_v1"
    assert _family("m5_c_recent-fbs-anytime_td") != "m5"


def test_settlement_dry_run_isolated():
    out = run_settlement_dry_run()
    assert out["manufactured_production_settlements"] is False
    assert out["all_pass"] is True
    assert out["cases"]["missing_stats"]["pass"] is True
    assert out["cases"]["scored_td"]["pass"] is True
    assert out["cases"]["explicit_zero"]["pass"] is True
