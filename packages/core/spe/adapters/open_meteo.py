"""Open-Meteo Previous Runs forecasts.

previous_day1 is the value the model issued 24 hours before the valid time.
That offset is the issue time. It is not an observation and not a live bulletin.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from spe.adapters.base import AdapterError, ProviderAdapter
from spe.config import settings

PREVIOUS = "https://previous-runs-api.open-meteo.com/v1/forecast"
HOURLY = (
    "temperature_2m_previous_day1",
    "wind_speed_10m_previous_day1",
    "wind_gusts_10m_previous_day1",
    "precipitation_previous_day1",
    "wind_direction_10m_previous_day1",
    "relative_humidity_2m_previous_day1",
)


class OpenMeteoAdapter(ProviderAdapter):
    provider_id = "open_meteo"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client

    def health(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "configured": settings.open_meteo_enabled,
            "commercial_use": False,
            "license": self.license.license_name,
            "lead": "previous_day1",
            "note": "Disabled unless OPEN_METEO_ENABLED=true. Commercial use is not licensed.",
        }

    def previous_day(
        self,
        lat: float,
        lon: float,
        start: date,
        end: date,
    ) -> dict[str, Any]:
        if not settings.open_meteo_enabled:
            raise AdapterError("Open-Meteo is disabled. Commercial use needs a paid license.")
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": ",".join(HOURLY),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "timezone": "UTC",
            "temperature_unit": "celsius",
            "wind_speed_unit": "ms",
            "precipitation_unit": "mm",
        }
        client = self._client or httpx.Client(timeout=60.0, follow_redirects=True)
        owned = self._client is None
        try:
            res = client.get(PREVIOUS, params=params, headers={"User-Agent": "CaliberResearch/0.2"})
            if res.status_code >= 400:
                raise AdapterError(f"Open-Meteo HTTP {res.status_code}: {res.text[:180]}")
            return res.json()
        except httpx.HTTPError as exc:
            raise AdapterError(f"Open-Meteo request failed: {exc}") from exc
        finally:
            if owned:
                client.close()
