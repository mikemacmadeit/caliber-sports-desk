"""Tonight's anytime chance follows the current job, not a career rate."""

from spe.models.player_td.current_role import _person, apply_current_role
from spe.models.player_td.tonight import load_tonight_tables, score_players, tables_ready


def test_suffix_on_the_depth_chart_still_marks_the_backup():
    depth = {("MIN", "RB", "aaron jones"): 2, ("MIN", "RB", "jordan mason"): 1}
    touches = {("MIN", "RB", "aaron jones"): 10.0, ("MIN", "RB", "jordan mason"): 24.0}
    backup = apply_current_role(
        {"p_estimate": 0.49, "confidence_interval": (0.4, 0.58), "reason_codes": []},
        player_name="Aaron Jones",
        team_abbr="MIN",
        position="RB",
        depth=depth,
        touches=touches,
    )
    assert _person("Aaron Jones Sr.") == "aaron jones"
    assert backup["p_estimate"] < 0.49
    assert backup["current_role"]["depth_rank"] == 2


def test_lead_back_outranks_the_famous_backup():
    chances = score_players(
        [
            {
                "person": "mason",
                "pos": "RB",
                "rank": 1,
                "carries": 18,
                "targets": 2,
                "rush_tds": 0.2,
                "rec_tds": 0,
                "games": 1.6,
            },
            {
                "person": "jones",
                "pos": "RB",
                "rank": 2,
                "carries": 8,
                "targets": 2,
                "rush_tds": 0.4,
                "rec_tds": 0,
                "games": 1.6,
            },
            {
                "person": "jefferson",
                "pos": "WR",
                "rank": 1,
                "carries": 0,
                "targets": 16,
                "rush_tds": 0,
                "rec_tds": 1.0,
                "games": 1.6,
            },
        ],
        team_rush_rate=1.0,
        team_rec_rate=1.4,
    )
    assert chances["mason"] > chances["jones"]
    assert chances["jefferson"] > chances["jones"]


def test_a_player_with_no_work_and_no_role_gets_nothing():
    chances = score_players(
        [
            {
                "person": "cook",
                "pos": "RB",
                "rank": 1,
                "carries": 20,
                "targets": 3,
                "rush_tds": 1.2,
                "rec_tds": 0,
                "games": 1.5,
            },
            {
                "person": "camp",
                "pos": "WR",
                "rank": 6,
                "carries": 0,
                "targets": 0.2,
                "rush_tds": 0,
                "rec_tds": 0,
                "games": 0.2,
            },
        ],
        team_rush_rate=1.1,
        team_rec_rate=1.3,
    )
    assert chances["cook"] > 0.2
    assert chances["camp"] < 0.05


def test_a_later_week_is_left_out_of_an_earlier_call():
    if not tables_ready():
        return
    load_tonight_tables.cache_clear()
    early = load_tonight_tables(1)
    later = load_tonight_tables(8)
    early_games = sum(line.games for line in early[1].values())
    later_games = sum(line.games for line in later[1].values())
    assert later_games > early_games
