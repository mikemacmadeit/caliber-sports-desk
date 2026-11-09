import json
from pathlib import Path

from spe.adapters.cfbd import CfbdAdapter
from spe.adapters.nflverse import NflverseAdapter
from spe.adapters.odds_api import OddsApiAdapter
from spe.licensing import license_for

FIXTURES = Path(__file__).parent / "fixtures"


def test_cfbd_fixture_normalizes_and_records_license():
    raw = json.loads((FIXTURES / "cfbd_games.json").read_text(encoding="utf-8"))
    adapter = CfbdAdapter(api_key="")
    result = adapter.fetch("games", fixture=raw)
    assert result.records[0]["classification"] == "fbs"
    assert result.records[0]["home_team"] == "Ohio State"
    assert result.records[0]["provider"] == "cfbd"
    assert adapter.license.commercial_redisplay == "requires_contract"


def test_odds_api_normalizes_player_markets():
    raw = json.loads((FIXTURES / "odds_event.json").read_text(encoding="utf-8"))
    adapter = OddsApiAdapter(api_key="")
    result = adapter.fetch_event_odds("nfl", "evt-nfl-1", fixture=raw)
    keys = {r["market_key"] for r in result.records}
    assert "player_anytime_td" in keys
    assert "player_receptions" in keys
    rice = next(r for r in result.records if r["description"] == "Rashee Rice")
    assert rice["price_american"] == 180


def test_nflverse_url_and_ftn_avoidance():
    adapter = NflverseAdapter()
    url = adapter.asset_url("pbp", 2025)
    assert "play_by_play_2025.parquet" in url
    assert "ftn_charting" in adapter.license.avoid_features
    assert license_for("nflverse").attribution_required is True


def test_nflverse_schedule_fixture():
    adapter = NflverseAdapter()
    result = adapter.fetch(
        "schedules",
        fixture=[{"game_id": "2026_10_BUF_KC", "season": 2026, "week": 10, "home_team": "KC", "away_team": "BUF"}],
    )
    assert result.records[0]["classification"] == "nfl"
    assert result.records[0]["provider_record_id"] == "2026_10_BUF_KC"
