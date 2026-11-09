"""The ATS page serves a saved board instead of waiting on a full refit."""

from datetime import UTC, datetime, timedelta

from spe.models.ats.board_cache import is_fresh, read_board, write_board


def test_saved_board_round_trips(tmp_path):
    path = tmp_path / "ats_board.json"
    write_board({"as_of": "2026-11-07T18:00:00+00:00", "rows": [{"game_id": "g1"}]}, path)
    loaded = read_board(path)
    assert loaded is not None
    assert loaded["rows"][0]["game_id"] == "g1"
    assert loaded["board_cached_at"]


def test_board_is_fresh_inside_the_window():
    now = datetime(2026, 11, 7, 18, 0, tzinfo=UTC)
    payload = {"board_cached_at": (now - timedelta(minutes=10)).isoformat(), "rows": []}
    assert is_fresh(payload, ttl_sec=15 * 60, now=now)
    stale = {"board_cached_at": (now - timedelta(minutes=20)).isoformat(), "rows": []}
    assert not is_fresh(stale, ttl_sec=15 * 60, now=now)


def test_missing_board_is_empty(tmp_path):
    assert read_board(tmp_path / "missing.json") is None
