from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from spe.adapters.base import AdapterError, FetchResult, ProviderAdapter
from spe.config import settings

BASE = "https://api.the-odds-api.com/v4"

SPORT_KEYS = {
    "nfl": "americanfootball_nfl",
    "fbs": "americanfootball_ncaaf",
}

# Mandatory v1 markets — both leagues. Keys from The Odds API docs (Nov 2026).
MARKET_KEYS = {
    "anytime_td": "player_anytime_td",
    "receptions": "player_receptions",
    "receiving_yards": "player_reception_yds",
    "rushing_yards": "player_rush_yds",
}


class OddsApiAdapter(ProviderAdapter):
    provider_id = "odds_api"

    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self.api_key = api_key if api_key is not None else settings.odds_api_key
        self._client = client
        self.last_credits_remaining: int | None = None
        self.last_credits_used: int | None = None
        self.last_credits_last: int | None = None

    def health(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "configured": bool(self.api_key),
            "budget_per_day": settings.odds_credit_budget_per_day,
            "markets": list(MARKET_KEYS.values()),
            "sports": SPORT_KEYS,
            "note": "Credits are not requests; cost scales by market, region, bookmaker, snapshots.",
        }

    def fetch_events(
        self,
        league: str,
        *,
        fixture: list[dict[str, Any]] | None = None,
    ) -> FetchResult:
        sport = SPORT_KEYS[league]
        if fixture is not None:
            return FetchResult(
                records=fixture,
                observed_at=datetime.now(UTC),
                endpoint=f"/sports/{sport}/events",
                raw=fixture,
            )
        return self._get(f"/sports/{sport}/events")

    def fetch_event_odds(
        self,
        league: str,
        event_id: str,
        markets: list[str] | None = None,
        *,
        fixture: dict[str, Any] | None = None,
        remaining_budget: int | None = None,
    ) -> FetchResult:
        sport = SPORT_KEYS[league]
        markets = markets or list(MARKET_KEYS.values())
        if fixture is not None:
            records = self._normalize_event(fixture)
            return FetchResult(
                records=records,
                observed_at=datetime.now(UTC),
                endpoint=f"/sports/{sport}/events/{event_id}/odds",
                raw=fixture,
                credit_cost=self._estimate_credits(len(markets)),
            )
        if remaining_budget is not None and remaining_budget <= 0:
            raise AdapterError("Odds API daily credit budget exhausted")
        params = {
            "regions": settings.odds_regions,
            "markets": ",".join(markets),
            "oddsFormat": "american",
            "bookmakers": ",".join(settings.odds_bookmaker_list),
        }
        result = self._get(f"/sports/{sport}/events/{event_id}/odds", params=params)
        result.records = self._normalize_event(result.raw if isinstance(result.raw, dict) else {})
        # x-requests-used is cumulative. x-requests-last is this call (4 = 4 markets × 1 region).
        result.credit_cost = self.last_credits_last or self._estimate_credits(len(markets))
        return result

    def _estimate_credits(self, n_markets: int) -> int:
        n_regions = max(1, len([r for r in settings.odds_regions.split(",") if r.strip()]))
        return max(1, n_markets * n_regions)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> FetchResult:
        if not self.api_key:
            raise AdapterError("ODDS_API_KEY is not configured")
        query = {"apiKey": self.api_key, **(params or {})}
        client = self._client or httpx.Client(timeout=30.0)
        owned = self._client is None
        try:
            res = client.get(f"{BASE}{path}", params=query)
            res.raise_for_status()
            self.last_credits_remaining = _header_int(res.headers, "x-requests-remaining")
            self.last_credits_used = _header_int(res.headers, "x-requests-used")
            self.last_credits_last = _header_int(res.headers, "x-requests-last")
            payload = res.json()
        except httpx.HTTPError as exc:
            raise AdapterError(f"Odds API request failed: {exc}") from exc
        finally:
            if owned:
                client.close()
        records = payload if isinstance(payload, list) else [payload]
        return FetchResult(
            records=records,
            observed_at=datetime.now(UTC),
            endpoint=path,
            raw=payload,
            credit_cost=self.last_credits_last or 0,
        )

    def _normalize_event(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        event_id = str(raw.get("id") or "")
        commence = raw.get("commence_time")
        for book in raw.get("bookmakers") or []:
            book_id = book.get("key")
            for market in book.get("markets") or []:
                mkey = market.get("key")
                ts = market.get("last_update") or commence
                for outcome in market.get("outcomes") or []:
                    out.append(
                        {
                            "provider": self.provider_id,
                            "provider_record_id": f"{event_id}:{book_id}:{mkey}:{outcome.get('name')}:{outcome.get('description')}:{outcome.get('point')}",
                            "event_id": event_id,
                            "bookmaker": book_id,
                            "market_key": mkey,
                            "name": outcome.get("name"),
                            "description": outcome.get("description"),
                            "price_american": outcome.get("price"),
                            "point": outcome.get("point"),
                            "source_at": ts,
                            "commence_time": commence,
                            "home_team": raw.get("home_team"),
                            "away_team": raw.get("away_team"),
                        }
                    )
        return out


def _header_int(headers: Any, key: str) -> int | None:
    raw = headers.get(key)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
