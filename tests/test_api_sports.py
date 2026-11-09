import httpx
import pytest

from spe.adapters.api_sports import ApiSportsAdapter
from spe.adapters.base import AdapterError


class _Client:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.url = ""
        self.headers: dict = {}

    def get(self, url: str, params=None, headers=None):
        self.url = url
        self.headers = headers or {}
        return httpx.Response(200, json=self.payload)

    def close(self) -> None:
        return None


def test_api_sports_football_sends_server_key():
    client = _Client({"errors": [], "response": [{"league": {"id": 39, "name": "Premier League"}}]})
    adapter = ApiSportsAdapter(api_key="test-key", client=client)
    payload = adapter.get("/leagues", {"id": 39})
    assert payload["response"][0]["league"]["name"] == "Premier League"
    assert client.url == "https://v3.football.api-sports.io/leagues"
    assert client.headers["x-apisports-key"] == "test-key"


def test_api_sports_american_football_host():
    client = _Client({"errors": [], "response": [{"league": {"id": 1, "name": "NFL"}}]})
    adapter = ApiSportsAdapter(api_key="test-key", client=client)
    adapter.get("/leagues", sport="american-football")
    assert client.url.startswith("https://v1.american-football.api-sports.io/")


def test_api_sports_requires_key():
    adapter = ApiSportsAdapter(api_key="")
    with pytest.raises(AdapterError, match="API_SPORTS_KEY"):
        adapter.get("/status")
