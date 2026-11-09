from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from spe.adapters.base import AdapterError, FetchResult, ProviderAdapter
from spe.config import settings

BASE = "https://api.collegefootballdata.com"

# Endpoints used in Phase 1. Classification is always FBS for this product.
SUPPORTED = {
    "games": "/games",
    "teams": "/teams",
    "roster": "/roster",
    "plays": "/plays",
    "ppa_players": "/ppa/players/games",
    "ppa_players_season": "/ppa/players/season",
    "stats_player": "/stats/player/season",
    "player_usage": "/player/usage",
    "games_players": "/games/players",
    "venues": "/venues",
    "lines": "/lines",
    "rankings": "/rankings",
    "talent": "/talent",
    "player_portal": "/player/portal",
}
# classification=fbs is valid on schedule/play feeds. Roster/PPA/usage reject or ignore it.
CLASSIFICATION_ENDPOINTS = {"games", "teams", "plays", "games_players", "lines", "rankings"}


class CfbdAdapter(ProviderAdapter):
    provider_id = "cfbd"

    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self.api_key = api_key if api_key is not None else settings.cfbd_api_key
        self._client = client
        self.calls_made = 0
        self.last_status: int | None = None
        self.last_headers: dict[str, str] = {}
        self.call_log: list[dict[str, Any]] = []

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise AdapterError("CFBD_API_KEY is not configured")
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    def health(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "configured": bool(self.api_key),
            "license": self.license.commercial_redisplay,
            "base": BASE,
            "calls_made": self.calls_made,
            "session_budget": settings.cfbd_session_call_budget,
            "note": "Start on the free key and measure calls. Do not assume a paid tier is required.",
        }

    def fetch(
        self,
        endpoint_key: str,
        params: dict[str, Any] | None = None,
        *,
        fixture: list[dict[str, Any]] | None = None,
    ) -> FetchResult:
        if endpoint_key not in SUPPORTED:
            raise AdapterError(f"Unsupported CFBD endpoint '{endpoint_key}'")
        observed = datetime.now(UTC)
        if fixture is not None:
            return FetchResult(
                records=[self._normalize_game(r) if endpoint_key == "games" else r for r in fixture],
                observed_at=observed,
                endpoint=SUPPORTED[endpoint_key],
                raw=fixture,
            )
        if self.calls_made >= settings.cfbd_session_call_budget:
            raise AdapterError(f"CFBD session call budget exhausted ({settings.cfbd_session_call_budget})")
        path = SUPPORTED[endpoint_key]
        query = dict(params or {})
        if endpoint_key in CLASSIFICATION_ENDPOINTS:
            query.setdefault("classification", "fbs")
        from spe.ingest.http import request_with_retry

        timeout = 180.0 if endpoint_key in {"games_players", "roster", "ppa_players"} else 90.0
        res = request_with_retry(
            "GET",
            f"{BASE}{path}",
            headers=self._headers(),
            params=query,
            timeout=timeout,
            client=self._client,
        )
        self.calls_made += 1
        self.last_status = res.status_code
        self.last_headers = {k.lower(): v for k, v in res.headers.items()}
        self.call_log.append({"endpoint": path, "params": query, "status": res.status_code})
        if res.status_code == 429:
            raise AdapterError("CFBD rate limited (429). Free-tier usage measured; stop and record coverage.")
        if res.status_code >= 400:
            raise AdapterError(f"CFBD request failed: HTTP {res.status_code}")
        payload = res.json()
        records = payload if isinstance(payload, list) else [payload]
        if endpoint_key == "games":
            records = [self._normalize_game(r) for r in records]
        return FetchResult(
            records=records,
            observed_at=observed,
            endpoint=path,
            raw=payload,
        )

    def _normalize_game(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "provider_record_id": str(raw.get("id") or raw.get("gameId") or ""),
            "season": raw.get("season"),
            "week": raw.get("week"),
            "season_type": raw.get("seasonType") or raw.get("season_type"),
            "home_team": raw.get("homeTeam") or raw.get("home_team"),
            "away_team": raw.get("awayTeam") or raw.get("away_team"),
            "home_id": raw.get("homeId") or raw.get("home_id"),
            "away_id": raw.get("awayId") or raw.get("away_id"),
            "kickoff": raw.get("startDate") or raw.get("start_date"),
            "venue": raw.get("venue"),
            "neutral_site": bool(raw.get("neutralSite") or raw.get("neutral_site")),
            "home_conference": raw.get("homeConference") or raw.get("home_conference"),
            "away_conference": raw.get("awayConference") or raw.get("away_conference"),
            "classification": "fbs",
            "raw": raw,
        }
