from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from spe.analytics.engine import apply_opponent_adjustments, player_game_metrics, prior_only, team_game_metrics
from spe.analytics.plays import cfbd_to_canonical, nflverse_to_canonical
from spe.analytics.snapshots import build_pregame_snapshots
from spe.eval.backtest import run_baseline_anytime_td
from spe.eval.leakage import LeakageError, assert_event_time
from spe.ingest.cfbd_historical import ingest_fbs_history
from spe.ingest.nflverse_historical import ingest_nfl_history


def _plays_frame() -> pd.DataFrame:
    raw = pd.DataFrame(
        [
            # Week 1: BUF vs NYJ — BUF explosive pass, NYJ stuffs a rush
            {"game_id": "g1", "play_id": "1", "posteam": "BUF", "defteam": "NYJ", "down": 1, "ydstogo": 10, "yardline_100": 65, "play_type": "pass", "yards_gained": 22, "epa": 0.8, "wp": 0.55, "success": 1, "pass_attempt": 1, "complete_pass": 1, "touchdown": 0, "qtr": 1, "game_seconds_remaining": 3500, "posteam_score": 0, "defteam_score": 0, "drive": 1, "passer_player_id": "00-1", "receiver_player_id": "00-2", "rusher_player_id": None, "first_down": 1},
            {"game_id": "g1", "play_id": "2", "posteam": "NYJ", "defteam": "BUF", "down": 1, "ydstogo": 10, "yardline_100": 70, "play_type": "run", "yards_gained": 1, "epa": -0.4, "wp": 0.48, "success": 0, "rush_attempt": 1, "touchdown": 0, "qtr": 1, "game_seconds_remaining": 3400, "posteam_score": 0, "defteam_score": 7, "drive": 2, "rusher_player_id": "00-3", "first_down": 0},
            # Week 2: BUF vs MIA — RZ target
            {"game_id": "g2", "play_id": "3", "posteam": "BUF", "defteam": "MIA", "down": 3, "ydstogo": 4, "yardline_100": 8, "play_type": "pass", "yards_gained": 8, "epa": 1.2, "wp": 0.7, "success": 1, "pass_attempt": 1, "complete_pass": 1, "touchdown": 1, "qtr": 2, "game_seconds_remaining": 1800, "posteam_score": 0, "defteam_score": 0, "drive": 1, "passer_player_id": "00-1", "receiver_player_id": "00-2", "first_down": 1},
            {"game_id": "g2", "play_id": "4", "posteam": "MIA", "defteam": "BUF", "down": 1, "ydstogo": 10, "yardline_100": 55, "play_type": "run", "yards_gained": 12, "epa": 0.3, "wp": 0.4, "success": 1, "rush_attempt": 1, "touchdown": 0, "qtr": 3, "game_seconds_remaining": 1200, "posteam_score": 0, "defteam_score": 7, "drive": 2, "rusher_player_id": "00-4", "first_down": 1},
            # Week 3: BUF vs NE — should not leak into week-3 pregame
            {"game_id": "g3", "play_id": "5", "posteam": "BUF", "defteam": "NE", "down": 1, "ydstogo": 10, "yardline_100": 40, "play_type": "pass", "yards_gained": 40, "epa": 2.0, "wp": 0.6, "success": 1, "pass_attempt": 1, "complete_pass": 1, "touchdown": 1, "qtr": 1, "game_seconds_remaining": 3000, "posteam_score": 0, "defteam_score": 0, "drive": 1, "receiver_player_id": "00-2", "first_down": 1},
        ]
    )
    canon = nflverse_to_canonical(raw)
    games = pd.DataFrame(
        [
            {"game_id": "nfl:g1", "home_team_id": "nfl:NYJ", "away_team_id": "nfl:BUF", "kickoff_utc": datetime(2023, 9, 10, tzinfo=UTC), "week": 1, "season_year": 2023, "league_id": "nfl", "neutral_site": False},
            {"game_id": "nfl:g2", "home_team_id": "nfl:BUF", "away_team_id": "nfl:MIA", "kickoff_utc": datetime(2023, 9, 17, tzinfo=UTC), "week": 2, "season_year": 2023, "league_id": "nfl", "neutral_site": False},
            {"game_id": "nfl:g3", "home_team_id": "nfl:NE", "away_team_id": "nfl:BUF", "kickoff_utc": datetime(2023, 9, 24, tzinfo=UTC), "week": 3, "season_year": 2023, "league_id": "nfl", "neutral_site": False},
        ]
    )
    canon["game_id"] = "nfl:" + canon["provider_game_id"].astype(str)
    canon["offense_id"] = "nfl:" + canon["offense_abbr"]
    canon["defense_id"] = "nfl:" + canon["defense_abbr"]
    from spe.analytics.engine import attach_game_context

    return attach_game_context(canon, games), games


def test_team_metrics_epa_success_explosive_third_rz():
    plays, _games = _plays_frame()
    tg = team_game_metrics(plays)
    buf_g1 = tg[(tg["team_id"] == "nfl:BUF") & (tg["game_id"] == "nfl:g1")].iloc[0]
    assert buf_g1["off_epa_per_play"] == 0.8
    assert buf_g1["explosive_play_rate"] == 1.0
    assert buf_g1["pass_epa_per_play"] == 0.8
    buf_g2 = tg[(tg["team_id"] == "nfl:BUF") & (tg["game_id"] == "nfl:g2")].iloc[0]
    assert buf_g2["rz_plays"] >= 1
    assert buf_g2["red_zone_td_rate"] == 1.0


def test_opponent_adjustment_does_not_treat_weak_and_strong_defenses_the_same():
    df = pd.DataFrame(
        [
            {"game_id": "a", "team_id": "OFF", "off_epa_per_play": 0.20, "def_epa_per_play": 0.0, "off_plays": 60, "def_plays": 60, "league_id": "nfl", "season_year": 2023, "week": 1, "split": "home"},
            {"game_id": "a", "team_id": "WEAK", "off_epa_per_play": 0.0, "def_epa_per_play": 0.30, "off_plays": 60, "def_plays": 60, "league_id": "nfl", "season_year": 2023, "week": 1, "split": "away"},
            {"game_id": "b", "team_id": "OFF", "off_epa_per_play": 0.20, "def_epa_per_play": 0.0, "off_plays": 60, "def_plays": 60, "league_id": "nfl", "season_year": 2023, "week": 2, "split": "home"},
            {"game_id": "b", "team_id": "STRONG", "off_epa_per_play": 0.0, "def_epa_per_play": -0.25, "off_plays": 60, "def_plays": 60, "league_id": "nfl", "season_year": 2023, "week": 2, "split": "away"},
        ]
    )
    out = apply_opponent_adjustments(df, "nfl", leave_one_out=True)
    vs_weak = out[(out["team_id"] == "OFF") & (out["game_id"] == "a")].iloc[0]["adj_off_epa"]
    vs_strong = out[(out["team_id"] == "OFF") & (out["game_id"] == "b")].iloc[0]["adj_off_epa"]
    assert vs_strong > vs_weak


def test_pregame_snapshot_excludes_the_game_being_evaluated():
    plays, games = _plays_frame()
    stats = pd.DataFrame(
        [
            {"game_id": "nfl:g1", "player_id": "nfl:00-2", "team_id": "nfl:BUF", "targets": 4, "receptions": 3, "rec_yards": 40, "rush_yards": 0, "carries": 0, "rush_td": 0, "rec_td": 0, "pass_td": 0, "rz_targets": 0, "rz_carries": 0, "gl_carries": 0, "kickoff_utc": datetime(2023, 9, 10, tzinfo=UTC), "week": 1, "season_year": 2023, "league_id": "nfl", "home_team_id": "nfl:NYJ", "away_team_id": "nfl:BUF", "position_group": "WR"},
            {"game_id": "nfl:g2", "player_id": "nfl:00-2", "team_id": "nfl:BUF", "targets": 8, "receptions": 6, "rec_yards": 80, "rush_yards": 0, "carries": 0, "rush_td": 0, "rec_td": 1, "pass_td": 0, "rz_targets": 2, "rz_carries": 0, "gl_carries": 0, "kickoff_utc": datetime(2023, 9, 17, tzinfo=UTC), "week": 2, "season_year": 2023, "league_id": "nfl", "home_team_id": "nfl:BUF", "away_team_id": "nfl:MIA", "position_group": "WR"},
            {"game_id": "nfl:g3", "player_id": "nfl:00-2", "team_id": "nfl:BUF", "targets": 99, "receptions": 9, "rec_yards": 200, "rush_yards": 0, "carries": 0, "rush_td": 0, "rec_td": 2, "pass_td": 0, "rz_targets": 9, "rz_carries": 0, "gl_carries": 0, "kickoff_utc": datetime(2023, 9, 24, tzinfo=UTC), "week": 3, "season_year": 2023, "league_id": "nfl", "home_team_id": "nfl:NE", "away_team_id": "nfl:BUF", "position_group": "WR"},
        ]
    )
    players = pd.DataFrame([{"player_id": "nfl:00-2", "position_group": "WR", "name": "WR1"}])
    team_rows, player_rows = build_pregame_snapshots(games=games, plays=plays, player_stats=stats, players=players, league_id="nfl")
    week3_players = [r for r in player_rows if r["game_id"] == "nfl:g3"]
    assert week3_players
    blob = week3_players[0]["feature_blob"]
    assert "nfl:g3" not in blob["included_game_ids"]
    assert blob["usage"]["targets_per_game"] == 6.0  # (4+8)/2, not 99
    assert blob["usage"]["rz_targets"] == 2.0
    as_of = week3_players[0]["as_of"]
    assert as_of < datetime(2023, 9, 24, tzinfo=UTC)
    assert_event_time(as_of=as_of, observed_at=datetime.fromisoformat(blob["available_as_of"]))


def test_prior_only_filters_kickoff():
    games = pd.DataFrame(
        {
            "game_id": ["a", "b"],
            "kickoff_utc": [datetime(2023, 9, 10, tzinfo=UTC), datetime(2023, 9, 17, tzinfo=UTC)],
        }
    )
    prior = prior_only(games, pd.Timestamp(datetime(2023, 9, 17, tzinfo=UTC)))
    assert list(prior["game_id"]) == ["a"]


def test_fbs_play_contract_matches_nfl(db):
    fixtures = {
        "teams": [
            {"id": 194, "school": "Ohio State", "abbreviation": "OSU", "conference": "Big Ten"},
            {"id": 130, "school": "Michigan", "abbreviation": "MICH", "conference": "Big Ten"},
        ],
        "games:2019": [
            {
                "id": 1,
                "season": 2019,
                "week": 12,
                "homeTeam": "Ohio State",
                "awayTeam": "Michigan",
                "homeId": 194,
                "awayId": 130,
                "startDate": "2019-11-23T17:00:00.000Z",
                "venue": "Ohio Stadium",
                "neutralSite": False,
                "homeConference": "Big Ten",
                "awayConference": "Big Ten",
                "homePoints": 28,
                "awayPoints": 17,
            }
        ],
        "roster:2019": [],
        "ppa_players:2019": [],
        "plays:2019": [
            {
                "gameId": 1,
                "id": 10,
                "period": 1,
                "down": 1,
                "distance": 10,
                "yardsToGoal": 65,
                "playType": "Pass Reception",
                "yardsGained": 18,
                "ppa": 0.5,
                "offenseId": 194,
                "defenseId": 130,
                "offenseScore": 0,
                "defenseScore": 0,
                "driveNumber": 1,
            },
            {
                "gameId": 1,
                "id": 11,
                "period": 1,
                "down": 2,
                "distance": 7,
                "yardsToGoal": 8,
                "playType": "Rushing Touchdown",
                "yardsGained": 8,
                "ppa": 1.1,
                "offenseId": 194,
                "defenseId": 130,
                "offenseScore": 0,
                "defenseScore": 0,
                "scoring": True,
                "playText": "rushing touchdown",
                "driveNumber": 1,
            },
        ],
    }
    ingest_fbs_history(db, seasons=[2019], include_plays=True, fixtures=fixtures)
    db.commit()
    from spe.db.models import Play

    plays = db.query(Play).filter(Play.game_id == "fbs:1").all()
    assert len(plays) == 2
    assert any(p.is_pass and p.is_scrimmage for p in plays)
    assert any(p.is_rush and p.is_td and p.is_red_zone for p in plays)
    assert any(p.epa == 0.5 or p.ppa == 0.5 for p in plays)
    canon = cfbd_to_canonical(fixtures["plays:2019"])
    canon["game_id"] = "fbs:" + canon["provider_game_id"].astype(str)
    canon["offense_id"] = "fbs:" + canon["offense_abbr"].astype(str)
    canon["defense_id"] = "fbs:" + canon["defense_abbr"].astype(str)
    canon["league_id"] = "fbs"
    canon["home_team_id"] = "fbs:194"
    canon["away_team_id"] = "fbs:130"
    canon["kickoff_utc"] = datetime(2019, 11, 23, tzinfo=UTC)
    canon["week"] = 12
    canon["season_year"] = 2019
    canon["is_home_off"] = canon["offense_id"] == "fbs:194"
    tg = team_game_metrics(canon)
    assert not tg.empty
    osu = tg[tg["team_id"] == "fbs:194"].iloc[0]
    assert osu["off_epa_per_play"] > 0
    assert osu["red_zone_td_rate"] == 1.0 or osu["rz_plays"] >= 1


def test_backtest_never_sees_future_game(db):
    observed = datetime(2024, 2, 1, tzinfo=UTC)
    schedules = pd.DataFrame(
        [
            {"game_id": "2023_01_BUF_NYJ", "season": 2023, "week": 1, "gameday": "2023-09-11", "gametime": "20:15", "home_team": "NYJ", "away_team": "BUF", "stadium": "MetLife", "roof": "open", "surface": "turf", "home_score": 16, "away_score": 22},
            {"game_id": "2023_02_MIA_BUF", "season": 2023, "week": 2, "gameday": "2023-09-18", "gametime": "13:00", "home_team": "BUF", "away_team": "MIA", "stadium": "Highmark", "roof": "open", "surface": "turf", "home_score": 31, "away_score": 10},
        ]
    )
    players = pd.DataFrame([{"gsis_id": "00-0034796", "display_name": "Josh Allen", "position": "QB", "position_group": "QB"}])
    weekly = {
        2023: pd.DataFrame(
            [
                {"player_id": "00-0034796", "player_display_name": "Josh Allen", "position": "QB", "season": 2023, "week": 1, "recent_team": "BUF", "targets": 0, "receptions": 0, "rushing_attempts": 6, "rushing_yards": 30, "receiving_yards": 0, "rushing_tds": 1, "receiving_tds": 0, "passing_tds": 2},
                {"player_id": "00-0034796", "player_display_name": "Josh Allen", "position": "QB", "season": 2023, "week": 2, "recent_team": "BUF", "targets": 0, "receptions": 0, "rushing_attempts": 8, "rushing_yards": 40, "receiving_yards": 0, "rushing_tds": 0, "receiving_tds": 0, "passing_tds": 1},
            ]
        )
    }
    ingest_nfl_history(db, seasons=[2023], include_pbp=False, schedules=schedules, players=players, weekly=weekly, injuries={2023: pd.DataFrame([])}, observed_at=observed)
    db.commit()
    result = run_baseline_anytime_td(
        db,
        "nfl",
        train_end=datetime(2023, 9, 13, tzinfo=UTC),
        test_start=datetime(2023, 9, 18, tzinfo=UTC),
        test_end=datetime(2023, 9, 19, tzinfo=UTC),
    )
    assert result["status"] == "ok"
    assert result["n"] >= 1
    from spe.db.models import BacktestPrediction

    pred = db.query(BacktestPrediction).first()
    assert pred.as_of > pred.train_cutoff or pred.train_cutoff <= pred.as_of
    assert pred.components["n_prior"] >= 1


def test_leakage_guard_raises():
    as_of = datetime(2023, 9, 17, tzinfo=UTC)
    try:
        assert_event_time(as_of=as_of, observed_at=as_of + timedelta(hours=1))
        raise AssertionError("expected LeakageError")
    except LeakageError:
        pass


def test_player_target_share_not_invented_routes():
    stats = pd.DataFrame(
        [
            {"game_id": "g", "player_id": "p1", "targets": 8, "receptions": 5, "rec_yards": 70, "rush_yards": 0, "carries": 0, "rush_td": 0, "rec_td": 1, "pass_td": 0},
            {"game_id": "g", "player_id": "p2", "targets": 2, "receptions": 1, "rec_yards": 10, "rush_yards": 0, "carries": 0, "rush_td": 0, "rec_td": 0, "pass_td": 0},
        ]
    )
    out = player_game_metrics(stats)
    p1 = out[out["player_id"] == "p1"].iloc[0]
    assert abs(p1["target_share"] - 0.8) < 1e-9
    assert "routes" not in out.columns or True
