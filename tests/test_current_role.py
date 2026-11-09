"""A backup does not keep a starter's old scoring rate on the live board."""

from spe.models.player_td.current_role import apply_current_role

DEPTH = {
    ("WAS", "RB", "rachaad white"): 1,
    ("WAS", "RB", "austin ekeler"): 2,
}
TOUCHES = {
    ("WAS", "RB", "rachaad white"): 26.0,
    ("WAS", "RB", "austin ekeler"): 11.0,
}


def _payload(chance: float) -> dict:
    return {"p_estimate": chance, "confidence_interval": (chance - 0.05, chance + 0.05), "reason_codes": []}


def test_backup_recent_work_cuts_the_old_scoring_rate():
    starter = apply_current_role(
        _payload(0.31),
        player_name="Rachaad White",
        team_abbr="WAS",
        position="RB",
        depth=DEPTH,
        touches=TOUCHES,
    )
    backup = apply_current_role(
        _payload(0.46),
        player_name="Austin Ekeler",
        team_abbr="WSH",
        position="RB",
        depth=DEPTH,
        touches=TOUCHES,
    )
    assert starter["p_estimate"] >= 0.31
    assert backup["p_estimate"] < starter["p_estimate"]
    assert "CURRENT_ROLE" in backup["reason_codes"]


def test_wind_cuts_a_receiver_and_a_dome_does_not():
    row = _payload(0.40)
    windy = apply_current_role(
        row,
        player_name="A.J. Brown",
        team_abbr="NE",
        position="WR",
        depth={("NE", "WR", "a.j. brown"): 1},
        touches={},
        weather={"wind_mps": 10.0, "precip_probability": 0.1, "indoors": False},
    )
    dome = apply_current_role(
        row,
        player_name="A.J. Brown",
        team_abbr="NE",
        position="WR",
        depth={("NE", "WR", "a.j. brown"): 1},
        touches={},
        weather={"wind_mps": 10.0, "precip_probability": 0.1, "indoors": True},
    )
    assert windy["p_estimate"] < 0.40
    assert "WEATHER" in windy["reason_codes"]
    assert dome["p_estimate"] == 0.40


def test_soft_run_defense_lifts_a_back_only_a_little():
    adjusted = apply_current_role(
        _payload(0.40),
        player_name="Rachaad White",
        team_abbr="WAS",
        position="RB",
        depth={("WAS", "RB", "rachaad white"): 1},
        touches={},
        opponent="NYG",
        defense={"NYG": {"rush": 8.0, "pass": 4.0, "league_rush": 4.0, "league_pass": 4.0}},
    )
    assert 0.40 < adjusted["p_estimate"] <= 0.40 * 1.12
    assert "MATCHUP" in adjusted["reason_codes"]
