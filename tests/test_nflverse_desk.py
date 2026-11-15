from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from spe.analytics.nflverse_opportunities import pbp_context, snap_context
from spe.db.base import as_utc
from spe.db.models import Game, NflverseGameContext, NgsWeek, PlayerGameStat, IngestRun, DepthChartWeek
from spe.ingest.nflverse_desk import store_context, refresh_nflverse_desk
from spe.ingest.nflverse_extra import ingest_ngs_frame, participation_available_at, ingest_depth_frame
from spe.ingest.nflverse_historical import ingest_weekly_stats, ingest_schedules
from spe.models.player_td.desk_opportunity import OpportunityHistory, prepare_history, NFL_VERSION
from spe.models.player_td.nflverse_context import attach_context, free_features, load_context
from test_free_sources import _game
from test_desk_opportunity import history_row, model, td_row


def play(pid=1, **updates):
    return {"game_id": "2024_01_BUF_NYJ", "play_id": pid, "posteam": "BUF", "defteam": "NYJ",
            "play_type": "pass", "receiver_player_id": "00-0034796", "rusher_player_id": None,
            "yardline_100": 5, "air_yards": 6, "drive": 1, "qtr": 1,
            "score_differential": 0, "epa": .2, **updates}


def test_scoring_opportunities_exclude_conversions_penalties_sacks_and_duplicates():
    rows = [play(), play(), play(2, play_type="no_play"), play(3, two_point_attempt=1),
            play(4, sack=1, receiver_player_id=None), play(5, play_type="run", qb_kneel=1),
            play(6, play_type="run", receiver_player_id=None, rusher_player_id="00-0034796", air_yards=None)]
    result = pbp_context(pd.DataFrame(rows), {"00-0034796": "QB"})
    player = next(r["metrics"] for r in result if r["dataset"] == "pbp_player")
    team = next(r["metrics"] for r in result if r["dataset"] == "pbp_team")
    assert player["targets"] == player["inside5_targets"] == player["end_zone_targets_estimated"] == 1
    assert player["inside5_carries"] == 1
    assert team["dropbacks"] == 2  # sacks count as dropbacks, not targets
    assert team["red_zone_drives"] == 1  # three plays are one trip
    assert player["targets_per_dropback"] == .5
    assert "routes" not in player


def test_missing_air_yards_are_unknown_not_zero_end_zone_targets():
    result = pbp_context(pd.DataFrame([play(air_yards=None)]), {})
    player = next(r["metrics"] for r in result if r["dataset"] == "pbp_player")
    assert player["end_zone_targets_estimated"] is None
    assert player["air_yards_coverage"] == 0


def test_snap_ids_are_exact_and_same_name_is_not_a_match():
    players = pd.DataFrame([{"pfr_id": "AlleJo02", "gsis_id": "00-0034796"}])
    rows = pd.DataFrame([{"game_id": "2024_01_BUF_NYJ", "pfr_player_id": "AlleJo02", "team": "BUF", "offense_pct": .8, "offense_snaps": 48},
                        {"game_id": "2024_01_BUF_NYJ", "pfr_player_id": "unknown", "player": "Josh Allen", "team": "BUF", "offense_pct": 1, "offense_snaps": 60}])
    result, unresolved = snap_context(rows, players)
    assert len(result) == unresolved == 1
    assert result[0]["entity_id"] == "nfl:00-0034796"
    assert result[0]["metrics"]["offense_snap_share"] == .8


def test_current_participation_cannot_leak_into_inseason_replay():
    kickoff = as_utc("2026-09-01T18:00:00Z")
    assert participation_available_at(kickoff, 2026) >= as_utc("2027-03-01T00:00:00Z")


def test_empty_or_future_context_never_becomes_observed_zero():
    rows = prepare_history([history_row(week=i) for i in (1, 2, 3)])
    context = [{"game_id": rows[0]["game_id"], "entity_id": "p", "team_id": "home", "dataset": "snap_counts",
                "metrics": {"offense_snap_share": 1}, "effective_at": "2026-10-01T00:00:00Z"}]
    attach_context(rows, context)
    f = OpportunityHistory(rows).features("p", "home", "away", "RB", as_utc("2026-09-28T00:00:00Z"), True)
    assert f["snap_share"] is None
    assert f["snap_known"] == f["pbp_known"] == 0
    assert f["end_zone_targets_estimated"] is None


def test_snap_trend_uses_recent_games_but_never_previous_team():
    rows = prepare_history([history_row(week=i) for i in (1, 2, 3)])
    context = [{"game_id": r["game_id"], "entity_id": "p", "team_id": "home", "dataset": "snap_counts",
                "metrics": {"offense_snap_share": share}} for r, share in zip(rows, (.2, .7, .8))]
    attach_context(rows, context)
    h = OpportunityHistory(rows)
    f = h.features("p", "home", "away", "RB", as_utc("2026-09-28T00:00:00Z"), True)
    assert f["snap_games"] == 3 and f["snap_trend"] > 0
    assert h.features("p", "away", "home", "RB", as_utc("2026-09-28T00:00:00Z"), True) is None


def test_context_upsert_refreshes_corrections_and_preserves_data_on_empty(db):
    _game(db)
    records = pbp_context(pd.DataFrame([play()]), {"00-0034796": "QB"})
    now = as_utc("2024-10-01T00:00:00Z")
    assert store_context(db, records, year=2024, observed_at=now) == 2
    store_context(db, records, year=2024, observed_at=now + timedelta(hours=3))
    unchanged = db.query(NflverseGameContext).filter_by(dataset="pbp_player").one()
    assert as_utc(unchanged.observed_at) == now
    corrected = pbp_context(pd.DataFrame([play(yardline_100=30)]), {"00-0034796": "QB"})
    store_context(db, corrected, year=2024, observed_at=now)
    row = db.query(NflverseGameContext).filter_by(dataset="pbp_player").one()
    assert row.metrics["inside5_targets"] == 0
    assert row.revision == 2
    store_context(db, [], year=2024, observed_at=now)
    assert db.query(NflverseGameContext).count() == 2


def test_context_does_not_ingest_inprogress_or_wrong_team(db):
    _game(db)
    records = pbp_context(pd.DataFrame([play()]), {})
    records[0]["team_id"] = "nfl:MIA"
    assert store_context(db, records, year=2024, observed_at=as_utc("2024-10-01T00:00:00Z")) == 1
    game = db.get(Game, "nfl:2024_01_BUF_NYJ")
    game.status = "live"
    db.flush()
    assert store_context(db, records, year=2024) == 0


def test_weekly_box_corrections_update_team_without_erasing_scoring_area_counts(db):
    _game(db)
    frame = pd.DataFrame([{"season": 2024, "week": 1, "team": "BUF", "player_id": "00-0034796", "rushing_attempts": 10, "rushing_tds": 1}])
    now = datetime(2024, 10, 1, tzinfo=UTC)
    ingest_weekly_stats(db, frame, 2024, observed_at=now)
    row = db.query(PlayerGameStat).one()
    row.gl_carries = 2
    row.quality_status = "pbp_opportunities"
    frame["rushing_attempts"] = 9
    ingest_weekly_stats(db, frame, 2024, observed_at=now)
    assert row.team_id == "nfl:BUF"
    assert row.carries == 9 and row.gl_carries == 2
    assert row.quality_status == "pbp_opportunities"
    assert db.query(PlayerGameStat).count() == 1


def test_ngs_partial_refresh_preserves_other_types_and_empty_source(db):
    _game(db)
    data = pd.DataFrame([{"season": 2024, "week": 1, "player_gsis_id": "00-0034796", "player_display_name": "Josh Allen", "team_abbr": "BUF", "attempts": 30, "rush_attempts": 8}])
    ingest_ngs_frame(db, {"passing": data, "rushing": data}, seasons=[2024])
    ingest_ngs_frame(db, {"passing": data}, seasons=[2024])
    ingest_ngs_frame(db, {"rushing": pd.DataFrame()}, seasons=[2024])
    assert {r.stat_type for r in db.query(NgsWeek)} == {"passing", "rushing"}


def test_refresh_is_throttled_across_worker_restarts(db, monkeypatch):
    now = datetime.now(UTC)
    db.add(IngestRun(provider="nflverse", dataset="desk_context", league_id="nfl", season_year=2026, status="ok", started_at=now))
    db.flush()
    def forbidden(*a, **k):
        raise AssertionError("Downloaded before refresh was due")
    monkeypatch.setattr("spe.ingest.nflverse_desk.load_asset", forbidden)
    assert refresh_nflverse_desk(db, year=2026, now=now)["status"] == "not_due"


def test_load_context_requires_actual_observation_before_live_cutoff(db):
    _game(db)
    store_context(db, pbp_context(pd.DataFrame([play()]), {}), year=2024,
                  observed_at=as_utc("2024-10-01T00:00:00Z"))
    rows = [{"game_id": "nfl:2024_01_BUF_NYJ", "player_id": "nfl:00-0034796", "team_id": "nfl:BUF",
             "home_team_id": "nfl:NYJ", "away_team_id": "nfl:BUF", "kickoff_utc": "2024-09-10T00:15:00Z"}]
    assert not load_context(db, rows, as_utc("2024-09-20T00:00:00Z"))[0]["nflverse_context"]
    assert load_context(db, rows, as_utc("2024-10-02T00:00:00Z"))[0]["nflverse_context"]["player"]


def test_schedule_refresh_marks_final_without_downgrading_from_missing_scores(db):
    _game(db)
    game = db.get(Game, "nfl:2024_01_BUF_NYJ")
    game.status = "scheduled"
    data = {"game_id": "2024_01_BUF_NYJ", "season": 2024, "week": 1, "home_team": "NYJ", "away_team": "BUF", "gameday": "2024-09-09", "gametime": "20:15", "home_score": 13, "away_score": 20}
    ingest_schedules(db, pd.DataFrame([data]), seasons=[2024], observed_at=datetime.now(UTC))
    assert game.status == "final" and game.home_score == 13
    data.update(home_score=None, away_score=None)
    ingest_schedules(db, pd.DataFrame([data]), seasons=[2024], observed_at=datetime.now(UTC))
    assert game.status == "final" and game.home_score == 13


def test_recent_depth_refresh_keeps_older_snapshots(db):
    _game(db)
    row = {"dt": "2024-09-01T10:00:00Z", "team": "BUF", "player_name": "Josh Allen", "gsis_id": "00-0034796", "pos_grp": "Base", "pos_abb": "QB", "pos_slot": 1, "pos_rank": 1}
    ingest_depth_frame(db, pd.DataFrame([row]), year=2024)
    row["dt"] = "2024-09-10T10:00:00Z"
    ingest_depth_frame(db, pd.DataFrame([row]), year=2024)
    ingest_depth_frame(db, pd.DataFrame([row]), year=2024)
    assert db.query(DepthChartWeek).count() == 2


def test_shadow_candidate_cannot_replace_primary_without_explicit_live_flag(db):
    import json
    from spe.config import settings
    from spe.models.player_td.desk_opportunity import load_model
    from spe.models.player_td.nflverse_context import NFL_EXTRA_FEATURES
    old = model()
    newer = {**old, "version": NFL_VERSION, "features": old["features"] + NFL_EXTRA_FEATURES, "live_enabled": False}
    folder = settings.parquet_dir / "models"
    folder.mkdir(parents=True)
    (folder / "nfl_desk_opportunity_v2.json").write_text(json.dumps(old))
    (folder / "nfl_desk_opportunity_v3.json").write_text(json.dumps(newer))
    assert load_model("nfl")["features"] == old["features"]
    assert load_model("nfl", research_candidate=True)["version"] == NFL_VERSION


def test_failed_refresh_keeps_successful_earlier_dataset(db, monkeypatch):
    _game(db)
    import spe.ingest.nflverse_desk as loader
    records = pbp_context(pd.DataFrame([play()]), {})
    now = as_utc("2024-10-01T00:00:00Z")
    monkeypatch.setattr(loader, "ingest_nfl_history", lambda *a, **k: {})
    def fetch(key, *args, **kwargs):
        if key == "pbp":
            return pd.DataFrame([play()])
        raise RuntimeError("source offline")
    monkeypatch.setattr(loader, "load_asset", fetch)
    result = refresh_nflverse_desk(db, year=2024, now=now, force=True)
    assert result["status"] == "degraded"
    assert result["datasets"]["desk_pbp"]["rows"] == 2
    assert db.query(NflverseGameContext).count() == 2


def test_shadow_comparison_is_frozen_for_unselected_players(db):
    from test_desk_opportunity import seed, NOW
    from spe.shadow.desk_ledger import freeze_desk_rows, latest_decisions
    seed(db)
    first, second = td_row("first", .7), td_row("second", .5)
    for row in (first, second):
        row["forecast"]["model_version"] = "desk_opportunity_v2-nfl"
        row["desk_analysis"] = {"eligible": True, "blockers": [], "nflverse_research_probability": .4,
                                "nflverse_research_model": "research-hash"}
    freeze_desk_rows(db, [first, second], NOW)
    saved = latest_decisions(db)[first["game_id"]].payload
    assert len(saved["nflverse_comparison"]) == 2
    assert saved["row"]["player_id"] == "first"


def test_weekly_stats_prefer_canonical_nfl_game_over_scoreboard_alias(db):
    _game(db)
    original = db.get(Game, "nfl:2024_01_BUF_NYJ")
    db.add(Game(game_id="zzz-scoreboard-alias", season_id=original.season_id, league_id="nfl",
                home_team_id=original.home_team_id, away_team_id=original.away_team_id,
                kickoff_utc=original.kickoff_utc, venue_id=original.venue_id, week=1, status="final", data_tier="historical"))
    db.flush()
    from spe.ingest.nflverse_historical import _game_index
    assert _game_index(db, 2024)[(2024, 1, "BUF")] == original.game_id
