"""College anytime chances follow recent carries and targets."""

from spe.models.player_td.fbs_tonight import chances_from_logs, score_players


def _log(player, team, pos, rank, carries, targets, rush_td=0, rec_td=0, opponent="opp"):
    return {
        "player_id": player,
        "team_id": team,
        "pos": pos,
        "game_rank": rank,
        "carries": carries,
        "targets": targets,
        "rush_td": rush_td,
        "rec_td": rec_td,
        "opponent_id": opponent,
    }


def test_lead_back_outranks_the_receiver_who_only_has_a_stored_prior():
    form = chances_from_logs(
        [
            _log("back", "t1", "RB", 1, 22, 3, rush_td=1),
            _log("back", "t1", "RB", 2, 18, 2, rush_td=1),
            _log("back", "t1", "RB", 3, 20, 4),
            _log("back", "t1", "RB", 4, 16, 2, rush_td=1),
            _log("wide", "t1", "WR", 1, 0, 7, rec_td=1),
            _log("wide", "t1", "WR", 2, 1, 6),
            _log("wide", "t1", "WR", 3, 0, 8),
            _log("wide", "t1", "WR", 4, 0, 5),
        ]
    )
    offense = form["offenses"]["t1"]
    chances = score_players(
        offense["players"],
        team_rush_rate=offense["rush_rate"],
        team_rec_rate=offense["rec_rate"],
    )
    assert chances["back"] > chances["wide"]
    assert offense["by_player"]["back"]["rank"] == 1


def test_a_player_with_no_recent_work_is_left_off_the_card():
    form = chances_from_logs([_log("ghost", "t1", "WR", 1, 0, 0)])
    assert "t1" not in form["offenses"]
