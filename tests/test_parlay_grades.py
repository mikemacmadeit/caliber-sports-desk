from spe.db.models import Game, Player, PlayerGameStat
from test_publication_pipeline import NOW, seed

from app.queries import grade_parlay_tickets


def test_finished_parlay_legs_use_the_same_win_and_loss_marks(db):
    finished = seed(db)
    finished.status = "final"
    finished.home_score = 24
    finished.away_score = 17
    db.add(Player(player_id="scorer", name="Scorer", position_group="WR", position="WR"))
    db.add(Player(player_id="blank", name="Blank", position_group="RB", position="RB"))
    db.add(
        Game(
            game_id="g2",
            season_id="season",
            league_id="nfl",
            home_team_id="a",
            away_team_id="b",
            venue_id="v",
            kickoff_utc=NOW,
            data_tier="upcoming",
            status="final",
            home_score=10,
            away_score=20,
        )
    )
    db.add(
        Game(
            game_id="g3",
            season_id="season",
            league_id="nfl",
            home_team_id="a",
            away_team_id="b",
            venue_id="v",
            kickoff_utc=NOW,
            data_tier="upcoming",
            status="scheduled",
        )
    )
    db.flush()
    db.add(
        PlayerGameStat(
            game_id="g",
            player_id="scorer",
            rush_td=0,
            rec_td=1,
            receptions=6,
            provider="test",
            provider_record_id="scorer",
            observed_at=NOW,
            effective_at=NOW,
        )
    )
    db.add(
        PlayerGameStat(
            game_id="g2",
            player_id="blank",
            rush_td=0,
            rec_td=0,
            provider="test",
            provider_record_id="blank",
            observed_at=NOW,
            effective_at=NOW,
        )
    )
    db.commit()

    tickets = [
        {
            "id": "lost",
            "legs": [
                {"game_id": "g", "player_id": "scorer", "market_id": "anytime_td", "side": "yes"},
                {"game_id": "g2", "player_id": "blank", "market_id": "anytime_td", "side": "yes"},
            ],
        },
        {
            "id": "won",
            "legs": [
                {"game_id": "g", "player_id": "scorer", "market_id": "receptions", "side": "over", "line": 4.5},
                {"game_id": "g", "market_id": "point_spread", "side": "home", "line": -3.5},
            ],
        },
        {
            "id": "push",
            "legs": [
                {"game_id": "g", "player_id": "scorer", "market_id": "anytime_td", "side": "yes"},
                {"game_id": "g", "market_id": "point_spread", "side": "home", "line": -7},
            ],
        },
        {
            "id": "open",
            "legs": [
                {"game_id": "g3", "player_id": "scorer", "market_id": "anytime_td", "side": "yes"},
                {"game_id": "g", "player_id": "scorer", "market_id": "anytime_td", "side": "yes"},
            ],
        },
    ]
    grade_parlay_tickets(db, tickets)

    assert [leg["result"] for leg in tickets[0]["legs"]] == ["win", "loss"]
    assert tickets[0]["result"] == "loss"
    assert [leg["result"] for leg in tickets[1]["legs"]] == ["win", "win"]
    assert tickets[1]["legs"][0]["actual"] == 6
    assert tickets[1]["result"] == "win"
    assert [leg["result"] for leg in tickets[2]["legs"]] == ["win", "push"]
    assert tickets[2]["result"] == "win"
    assert tickets[3]["legs"][0]["result"] == "pending"
    assert tickets[3]["result"] == "pending"
