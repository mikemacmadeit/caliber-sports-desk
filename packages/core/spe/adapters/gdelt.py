"""GDELT DOC 2.0 article list. Metadata only; not an injury wire."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from spe.adapters.base import AdapterError, FetchResult, ProviderAdapter

DOC = "https://api.gdeltproject.org/api/v2/doc/doc"


class GdeltAdapter(ProviderAdapter):
    provider_id = "gdelt"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client

    def health(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "configured": True,
            "license": self.license.license_name,
            "note": "Articles are unverified. seendate is the only availability time.",
        }

    def artlist(
        self,
        query: str,
        *,
        start: str,
        end: str,
        max_records: int = 75,
        fixture: list[dict[str, Any]] | None = None,
    ) -> FetchResult:
        observed = datetime.now(UTC)
        if fixture is not None:
            return FetchResult(records=fixture, observed_at=observed, endpoint="fixture", raw=fixture)
        params = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": max_records,
            "startdatetime": start,
            "enddatetime": end,
            "sort": "datedesc",
        }
        client = self._client or httpx.Client(timeout=45.0, follow_redirects=True)
        owned = self._client is None
        try:
            res = client.get(DOC, params=params, headers={"User-Agent": "CaliberResearch/0.2"})
        except httpx.HTTPError as exc:
            raise AdapterError(f"GDELT request failed: {exc}") from exc
        finally:
            if owned:
                client.close()
        text = res.text.lstrip()
        if res.status_code >= 400 or not text.startswith("{"):
            raise AdapterError(f"GDELT HTTP {res.status_code}: {text[:160]}")
        payload = res.json()
        articles = payload.get("articles") if isinstance(payload, dict) else None
        return FetchResult(
            records=list(articles or []),
            observed_at=observed,
            endpoint=DOC,
            raw=payload,
        )
