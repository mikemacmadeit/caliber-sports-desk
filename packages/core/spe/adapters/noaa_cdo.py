"""NOAA Climate Data Online v2. Daily station observations, not forecasts."""

from __future__ import annotations

from typing import Any

import httpx

from spe.adapters.base import AdapterError, ProviderAdapter
from spe.config import settings

BASE = "https://www.ncei.noaa.gov/cdo-web/api/v2"


class NoaaCdoAdapter(ProviderAdapter):
    provider_id = "noaa_cdo"

    def __init__(self, token: str | None = None, client: httpx.Client | None = None):
        self.token = settings.noaa_cdo_token if token is None else token
        self._client = client

    def health(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "configured": bool(self.token),
            "license": self.license.license_name,
            "note": "GHCND daily observations. A missing token writes nothing.",
        }

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.token:
            raise AdapterError("NOAA_CDO_TOKEN is not configured")
        client = self._client or httpx.Client(timeout=45.0, follow_redirects=True)
        owned = self._client is None
        try:
            res = client.get(
                f"{BASE}{path}",
                params=params,
                headers={"token": self.token, "User-Agent": "CaliberResearch/0.2"},
            )
            if res.status_code >= 400:
                raise AdapterError(f"NOAA CDO HTTP {res.status_code}: {res.text[:180]}")
            payload = res.json()
        except httpx.HTTPError as exc:
            raise AdapterError(f"NOAA CDO request failed: {exc}") from exc
        finally:
            if owned:
                client.close()
        return payload if isinstance(payload, dict) else {"results": payload}

    def probe_without_token(self) -> str:
        """Confirm the live gate. Does not invent observations."""
        client = self._client or httpx.Client(timeout=30.0, follow_redirects=True)
        owned = self._client is None
        try:
            res = client.get(f"{BASE}/datasets", params={"limit": 1}, headers={"User-Agent": "CaliberResearch/0.2"})
            return f"HTTP {res.status_code}: {res.text[:180]}"
        except httpx.HTTPError as exc:
            return f"probe failed: {exc}"
        finally:
            if owned:
                client.close()
