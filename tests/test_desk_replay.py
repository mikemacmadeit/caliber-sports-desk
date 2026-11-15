from datetime import timedelta
import pytest

from spe.db.models import DeskDecision, Player, PlayerGameStat, PublishedStat, ShadowPrediction
from spe.models.player_td.desk_replay import grade, replay_day, select_replay_candidate
from test_desk_combined import combined_model
from test_desk_opportunity import history_row, td_row as base_td_row
from test_publication_pipeline import NOW, seed


def test_requested_candidate_is_date_scoped_and_hash_pinned(db, monkeypatch):
    import json
    import spe.models.player_td.desk_replay as replay
    from spe.config import settings
    path = settings.parquet_dir / "models" / "desk_replay_overrides.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"2026-11-14":{"fbs":{"artifact_sha256":"candidate"}}}))
    monkeypatch.setattr(replay, "load_combined_model", lambda league, research_candidate=False, version=None:
        {"artifact_sha256":"candidate" if research_candidate else "active", "live_enabled":not research_candidate, "version":version})
    assert replay.load_replay_model("2026-11-14", "fbs")["user_requested_experiment"]
    assert not replay.load_replay_model("2026-11-14", "fbs")["live_enabled"]
    assert replay.load_replay_model("2026-11-15", "fbs")["artifact_sha256"] == "active"
    assert replay.load_replay_model("2026-11-14", "nfl")["artifact_sha256"] == "active"
    path.write_text(json.dumps({"2026-11-14":{"fbs":{"artifact_sha256":"candidate","version":"desk_combined_v5"}}}))
    assert replay.load_replay_model("2026-11-14", "fbs")["version"] == "desk_combined_v5"
    path.write_text(json.dumps({"2026-11-14":{"fbs":{"artifact_sha256":"different"}}}))
    assert replay.load_replay_model("2026-11-14", "fbs") is None


def test_endpoint_retries_concurrent_snapshot_insert(monkeypatch):
    from datetime import date
    from sqlalchemy.exc import IntegrityError
    from app.main import combined_replay
    import app.queries as queries
    import spe.models.player_td.desk_replay as replay
    import spe.models.player_td.desk_replay_results as results
    class Session:
        commits = 0
        rollbacks = 0
        def commit(self):
            self.commits += 1
            if self.commits == 1:
                raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed: board_snapshots.key"))
        def rollback(self):
            self.rollbacks += 1
    calls = []
    monkeypatch.setattr(replay, "replay_day", lambda *a, **kw: calls.append(a) or {"rows":[]})
    monkeypatch.setattr('spe.models.player_td.scorer_refresh.ensure_refresh', lambda *a: 'ready')
    monkeypatch.setattr(queries, "attach_game_marks", lambda *a: None)
    monkeypatch.setattr(results, "ensure_results", lambda *a: "ready")
    session = Session()
    assert combined_replay(date(2026,11,14), "all", session)["results_refresh_status"] == "ready"
    assert session.commits == 2 and session.rollbacks == 1 and len(calls) == 2


def td_row(*args, **kwargs):
    return {**base_td_row(*args, **kwargs), "market_context": {"usable": True}, "team_implied_points": 28}


def test_selection_uses_probability_and_recorded_price_not_game_results():
    favorite, value = td_row("favorite", .6), td_row("value", .4)
    favorite["book_price"], value["book_price"] = -300, 280
    assert select_replay_candidate([favorite, value])["player_id"] == "favorite"
    value["result"] = "blank"
    favorite["result"] = "scored"
    assert select_replay_candidate([favorite, value])["player_id"] == "favorite"
    value["book_price"] = -300
    picked = select_replay_candidate([favorite, value])
    assert picked["player_id"] == "favorite"
    assert picked["replay_selection"] == "balanced_scorer"


def test_close_affordable_alternative_and_low_chance_pass():
    leader, alternate, longshot = td_row("ND leader", .596), td_row("ND alternative", .499), td_row("BC", .405)
    leader["book_price"], alternate["book_price"], longshot["book_price"] = -300, -135, 280
    assert select_replay_candidate([leader, alternate, longshot])["player_id"] == "ND alternative"
    assert select_replay_candidate([td_row(chance=.399)]) is None
    from spe.shadow.atd_desk_takes import select_desk_takes
    for row in (leader, alternate, longshot):
        row["desk_analysis"] = {"eligible": True, "combination_status": "active"}
    assert select_desk_takes([leader, alternate, longshot])[0]["player_id"] == "ND alternative"


def test_grade_requires_final_and_observed_box_and_ignores_passing(db):
    game = seed(db)
    db.add(Player(player_id="p", name="Quarterback", position_group="QB", position="QB"))
    db.flush()
    db.add(PlayerGameStat(game_id=game.game_id, player_id="p", rush_td=0, rec_td=0, pass_td=5,
        provider="cfbd", provider_record_id="box", observed_at=NOW, effective_at=NOW))
    db.flush()
    assert grade(db, game, "p") == "pending"
    game.status = "final"
    assert grade(db, game, "missing") == "awaiting_box"
    assert grade(db, game, "p") == "blank"
    db.add(PublishedStat(key="g|p|anytime_td", game_id="g", player_id="p", market_id="anytime_td",
        actual=1, source="espn.summary:test", observed_at=NOW))
    db.flush()
    assert grade(db, game, "p") == "scored"


def test_replay_keeps_finished_pick_and_never_rewrites_original_ledger(db, monkeypatch):
    import spe.models.player_td.desk_replay as replay
    game = seed(db)
    game.kickoff_utc = NOW - timedelta(hours=5)
    db.add(Player(player_id="p", name="Runner", position_group="RB", position="RB"))
    db.flush()
    history = [history_row(week=i, team_id="a", home_team_id="a", away_team_id="b",
        kickoff_utc=(NOW-timedelta(days=(4-i)*6)).isoformat()) for i in (1,2,3)]
    monkeypatch.setattr(replay, "load_history", lambda *a: history)
    monkeypatch.setattr(replay, "load_combined_model", lambda *a: combined_model())
    monkeypatch.setattr(replay, "context_for_game", lambda *a, **k: {"usable": True, "home_implied_points":28, "away_implied_points":28})
    from spe.db.models import Market, Outcome, OddsTick, Bookmaker
    db.add(Market(market_id="anytime_td",sport="football",market_type="player_yesno",period="game",stat="td",grading_rule="yes"))
    db.add(Bookmaker(bookmaker_id="book",name="Book"))
    db.flush()
    db.add(Outcome(outcome_id="quote",game_id="g",player_id="p",market_id="anytime_td",side="yes",description="Runner"))
    db.flush()
    db.add(OddsTick(outcome_id="quote",bookmaker_id="book",price_american=200,captured_at=NOW-timedelta(hours=6),source_at=NOW-timedelta(hours=6),
        observed_at=NOW-timedelta(hours=6),effective_at=NOW-timedelta(hours=6),provider="test",provider_record_id="quote"))
    db.flush()
    result = replay_day(db, "2026-11-15", now=NOW)
    assert len(result["rows"]) == 1
    chosen = result["rows"][0]
    assert chosen["retrospective"]
    assert chosen["result"] == "pending"
    assert chosen["forecast"]["data_cutoff"] < game.kickoff_utc.isoformat()
    # A later final and stats update can grade, but cannot replace the cached pick.
    game.status = "final"
    db.add(PublishedStat(key="g|p|anytime_td", game_id="g", player_id="p", market_id="anytime_td",
        actual=0, source="espn.summary:test", observed_at=NOW))
    db.flush()
    monkeypatch.setattr(replay, "load_history", lambda *a: (_ for _ in ()).throw(AssertionError("must not refit a completed pick")))
    again = replay_day(db, "2026-11-15", now=NOW)
    assert again["rows"][0]["player_id"] == chosen["player_id"]
    assert again["rows"][0]["forecast"] == chosen["forecast"]
    assert again["losses"] == 1
    assert db.query(DeskDecision).count() == 0
    assert db.query(ShadowPrediction).count() == 0
    assert replay_day(db, "2026-11-14", now=NOW)["rows"] == []


def test_replay_source_refresh_does_not_invent_missing_player_zero(db, monkeypatch):
    from spe.db.models import BoardSnapshot
    import spe.models.player_td.desk_replay_results as results
    seed(db)
    from spe.models.player_td.desk_replay import CACHE_PREFIX
    db.add(BoardSnapshot(key=CACHE_PREFIX + "espn:nfl:123:hash", updated_at=NOW,
        payload={"row": {"game_id": "espn:nfl:123", "player_id":"p", "player_name":"Missing Runner", "league_id":"nfl", "kickoff_utc":NOW.isoformat()}}))
    db.flush()
    monkeypatch.setattr(results, "upsert_scoreboard_events", lambda *a: None)
    event = {"id":"123", "status":{"type":{"completed":True}}}
    box = {"header":{"competitions":[{"status":{"type":{"completed":True}}}]}, "boxscore":{"players":[]}}
    report = results.refresh_results(db, "2026-11-15", fetch=lambda *a:[event], summary=lambda *a:box)
    assert report["graded_players"] == 0
    assert report["missing_boxes"] == ["espn:nfl:123:p"]
    assert db.query(PublishedStat).count() == 0


@pytest.mark.parametrize("alternative_present", [True, False])
def test_replay_source_refresh_grades_saved_alternative_independently(db, monkeypatch, alternative_present):
    import copy
    from spe.db.models import BoardSnapshot
    import spe.models.player_td.desk_replay_results as results
    class Clock(results.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(results, "datetime", Clock)
    game = seed(db)
    game.kickoff_utc = NOW - timedelta(hours=5)
    game.status = "final"
    for pid, name in (("p", "Main Runner"), ("alt", "Alternative Receiver")):
        db.add(Player(player_id=pid, name=name, position_group="WR", position="WR"))
    row = {"game_id":"g", "player_id":"p", "player_name":"Main Runner", "league_id":"nfl",
        "kickoff_utc":game.kickoff_utc.isoformat(), "decision":{
            "most_likely":{"player_id":"p", "name":"Main Runner"},
            "value_candidate":{"player_id":"alt", "name":"Alternative Receiver", "price_supported":True},
            "alternatives":[{"player_id":"comparison", "name":"Not A Pick"}]}}
    original = copy.deepcopy(row)
    saved = BoardSnapshot(key="scorer-desk-v1:g", updated_at=NOW, payload={"row":row})
    db.add(saved)
    db.flush()
    monkeypatch.setattr(results, "upsert_scoreboard_events", lambda *a: None)
    monkeypatch.setattr("spe.ingest.espn_box._espn_event_id", lambda *a: "123")
    event = {"id":"123", "status":{"type":{"completed":True}}}
    athletes = [{"athlete":{"id":"p", "displayName":"Main Runner"}, "stats":["0"]}]
    if alternative_present:
        athletes.append({"athlete":{"id":"alt", "displayName":"Alternative Receiver"}, "stats":["1"]})
    box = {"header":{"competitions":[{"status":{"type":{"completed":True}}}]},
        "boxscore":{"players":[{"statistics":[{"name":"receiving", "labels":["TD"], "athletes":athletes}]}]}}
    report = results.refresh_results(db, "2026-11-15", fetch=lambda *a:[event], summary=lambda *a:box)
    assert report["graded_players"] == (2 if alternative_present else 1)
    assert report["missing_boxes"] == ([] if alternative_present else ["g:alt"])
    assert grade(db, game, "p") == "blank"
    assert grade(db, game, "alt") == ("scored" if alternative_present else "awaiting_box")
    assert db.get(PublishedStat, "g|comparison|anytime_td") is None
    assert saved.payload["row"] == original
