"""Props pages serve a saved board instead of rebuilding on every click."""

from datetime import UTC, datetime, timedelta

from spe.props.board_cache import entry_key, is_fresh, read_entry, write_entry


def test_saved_props_entry_round_trips(tmp_path):
    path = tmp_path / "props_board.json"
    write_entry("anytime_td", "nfl", "2026-11-15", [{"player_name": "Jahmyr Gibbs"}], path)
    loaded = read_entry("anytime_td", "nfl", "2026-11-15", path)
    assert loaded is not None
    assert loaded["rows"][0]["player_name"] == "Jahmyr Gibbs"
    assert loaded["board_cached_at"]
    assert entry_key("anytime_td", "nfl", "2026-11-15") == "anytime_td|nfl|2026-11-15"


def test_props_entry_is_fresh_inside_the_window():
    now = datetime(2026, 11, 7, 18, 0, tzinfo=UTC)
    payload = {"board_cached_at": (now - timedelta(minutes=10)).isoformat(), "rows": []}
    assert is_fresh(payload, ttl_sec=15 * 60, now=now)
    stale = {"board_cached_at": (now - timedelta(minutes=20)).isoformat(), "rows": []}
    assert not is_fresh(stale, ttl_sec=15 * 60, now=now)


def test_missing_props_entry_is_empty(tmp_path):
    assert read_entry("anytime_td", "nfl", "2026-11-15", tmp_path / "missing.json") is None
