"""API-Sports. One key, two hosts.

football is association football (API-Football). american-football is NFL/NCAA.
The key stays on the server. Responses are not redistributed.
"""

from __future__ import annotations

from typing import Any

import httpx

from spe.adapters.base import AdapterError, ProviderAdapter
from spe.config import settings

BASES = {
    "football": "https://v3.football.api-sports.io",
    "american-football": "https://v1.american-football.api-sports.io",
}


class ApiSportsAdapter(ProviderAdapter):
    provider_id = "api_sports"

    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self.api_key = settings.api_sports_key if api_key is None else api_key
        self._client = client

    def health(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "configured": bool(self.api_key),
            "license": self.license.license_name,
            "hosts": sorted(BASES),
        }

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        sport: str = "football",
    ) -> dict[str, Any]:
        if not self.api_key:
            raise AdapterError("API_SPORTS_KEY is not configured")
        if sport not in BASES:
            raise AdapterError(f"Unsupported API-Sports host '{sport}'")
        client = self._client or httpx.Client(timeout=30.0)
        owned = self._client is None
        try:
            res = client.get(
                f"{BASES[sport]}{path}",
                params=params,
                headers={"x-apisports-key": self.api_key},
            )
            if res.status_code >= 400:
                raise AdapterError(f"API-Sports HTTP {res.status_code}")
            payload = res.json()
        except httpx.HTTPError as exc:
            raise AdapterError(f"API-Sports request failed: {exc}") from exc
        finally:
            if owned:
                client.close()
        if not isinstance(payload, dict):
            raise AdapterError("API-Sports returned a non-object response")
        errors = payload.get("errors")
        if errors:
            raise AdapterError(f"API-Sports error: {errors}")
        return payload
