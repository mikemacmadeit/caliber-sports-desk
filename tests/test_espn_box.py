from spe.ingest.espn_box import parse_scoring_touchdowns


def test_box_score_counts_rush_rec_and_return_touchdowns():
    payload = {
        "boxscore": {
            "players": [
                {
                    "statistics": [
                        {
                            "name": "rushing",
                            "labels": ["CAR", "YDS", "TD"],
                            "athletes": [{"athlete": {"id": "1", "displayName": "Runner"}, "stats": ["12", "80", "1"]}],
                        },
                        {
                            "name": "receiving",
                            "labels": ["REC", "YDS", "TD"],
                            "athletes": [{"athlete": {"id": "2", "displayName": "Catcher"}, "stats": ["5", "60", "0"]}],
                        },
                        {
                            "name": "kickReturns",
                            "labels": ["NO", "YDS", "TD"],
                            "athletes": [{"athlete": {"id": "2", "displayName": "Catcher"}, "stats": ["2", "40", "1"]}],
                        },
                        {
                            "name": "passing",
                            "labels": ["C/ATT", "YDS", "TD"],
                            "athletes": [{"athlete": {"id": "3", "displayName": "Passer"}, "stats": ["20/30", "250", "3"]}],
                        },
                    ]
                }
            ]
        }
    }
    rows = {row["name"]: row for row in parse_scoring_touchdowns(payload)}
    assert rows["Runner"]["rush_td"] == 1
    assert rows["Catcher"]["rec_td"] == 0
    assert rows["Catcher"]["return_td"] == 1
    assert "Passer" not in rows
