from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from spe.adapters.base import AdapterError, FetchResult, ProviderAdapter
from spe.config import settings

POINTS = "https://api.weather.gov/points/{lat},{lon}"
ALERTS = "https://api.weather.gov/alerts/active"

_WIND = {
    "N": 0.0,
    "NNE": 22.5,
    "NE": 45.0,
    "ENE": 67.5,
    "E": 90.0,
    "ESE": 112.5,
    "SE": 135.0,
    "SSE": 157.5,
    "S": 180.0,
    "SSW": 202.5,
    "SW": 225.0,
    "WSW": 247.5,
    "W": 270.0,
    "WNW": 292.5,
    "NW": 315.0,
    "NNW": 337.5,
}


class NwsAdapter(ProviderAdapter):
    provider_id = "nws"

    def __init__(self, user_agent: str | None = None, client: httpx.Client | None = None):
        self.user_agent = user_agent or settings.nws_user_agent
        self._client = client

    def health(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "configured": bool(self.user_agent),
            "license": self.license.license_name,
            "needs_key": False,
        }

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept": "application/geo+json"}

    def fetch_forecast(
        self,
        lat: float,
        lon: float,
        *,
        fixture: dict[str, Any] | None = None,
    ) -> FetchResult:
        observed = datetime.now(UTC)
        if fixture is not None:
            return FetchResult(
                records=[fixture],
                observed_at=observed,
                endpoint="fixture",
                raw=fixture,
            )
        headers = self._headers()
        client = self._client or httpx.Client(timeout=30.0)
        owned = self._client is None
        try:
            points = client.get(POINTS.format(lat=f"{lat:.4f}", lon=f"{lon:.4f}"), headers=headers)
            if points.status_code == 404:
                raise AdapterError("NWS coverage unavailable")
            points.raise_for_status()
            forecast_url = points.json()["properties"]["forecastHourly"]
            forecast = client.get(forecast_url, headers=headers)
            forecast.raise_for_status()
            payload = forecast.json()
        except AdapterError:
            raise
        except (httpx.HTTPError, KeyError) as exc:
            raise AdapterError(f"NWS forecast failed: {exc}") from exc
        finally:
            if owned:
                client.close()
        periods = payload.get("properties", {}).get("periods", [])
        return FetchResult(
            records=periods,
            observed_at=observed,
            endpoint=forecast_url,
            raw=payload,
        )

    def fetch_alerts(self, lat: float, lon: float) -> FetchResult:
        observed = datetime.now(UTC)
        headers = self._headers()
        client = self._client or httpx.Client(timeout=30.0)
        owned = self._client is None
        try:
            res = client.get(
                ALERTS,
                params={"point": f"{lat:.4f},{lon:.4f}"},
                headers=headers,
            )
            if res.status_code == 404:
                return FetchResult(records=[], observed_at=observed, endpoint=ALERTS, raw={})
            res.raise_for_status()
            payload = res.json()
        except httpx.HTTPError as exc:
            raise AdapterError(f"NWS alerts failed: {exc}") from exc
        finally:
            if owned:
                client.close()
        features = payload.get("features") or []
        return FetchResult(
            records=[f.get("properties") or {} for f in features],
            observed_at=observed,
            endpoint=str(res.url),
            raw=payload,
        )

    def fetch_bundle(
        self, lat: float, lon: float
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Hourly periods and active alert properties for one lat/lon. One venue, many kickoffs."""
        headers = self._headers()
        client = self._client or httpx.Client(timeout=30.0)
        owned = self._client is None
        try:
            points = client.get(POINTS.format(lat=f"{lat:.4f}", lon=f"{lon:.4f}"), headers=headers)
            if points.status_code == 404:
                raise AdapterError("NWS coverage unavailable")
            points.raise_for_status()
            forecast_url = points.json()["properties"]["forecastHourly"]
            forecast = client.get(forecast_url, headers=headers)
            forecast.raise_for_status()
            periods = forecast.json().get("properties", {}).get("periods") or []
            alerts_res = client.get(
                ALERTS,
                params={"point": f"{lat:.4f},{lon:.4f}"},
                headers=headers,
            )
            features = []
            if alerts_res.status_code == 200:
                features = alerts_res.json().get("features") or []
            alert_props = [f.get("properties") or {} for f in features]
            return periods, alert_props
        except AdapterError:
            raise
        except (httpx.HTTPError, KeyError) as exc:
            raise AdapterError(f"NWS forecast failed: {exc}") from exc
        finally:
            if owned:
                client.close()

    def conditions_at(self, lat: float, lon: float, kickoff: datetime) -> dict[str, Any] | None:
        """Hourly conditions covering kickoff, plus alerts that overlap the game."""
        periods, alerts = self.fetch_bundle(lat, lon)
        return assemble_conditions(periods, alerts, kickoff, observed_at=datetime.now(UTC))


def assemble_conditions(
    periods: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    kickoff: datetime,
    *,
    observed_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Return one kickoff-hour snapshot. None when the hourly grid does not cover kickoff."""
    kick = _as_utc(kickoff)
    period = _period_covering(periods, kick)
    if period is None:
        return None
    temp = period.get("temperature")
    unit = period.get("temperatureUnit") or "F"
    humidity = _nested_value(period.get("relativeHumidity"))
    prob = _nested_value(period.get("probabilityOfPrecipitation"))
    return {
        "temperature_c": _to_c(temp, unit),
        "wind_mps": _mph_to_mps(_parse_mph(period.get("windSpeed"))),
        "gust_mps": _mph_to_mps(_parse_mph(period.get("windGust"))),
        "wind_dir_deg": _wind_dir(period.get("windDirection")),
        "precip_mm": _precip_mm(period),
        "precip_probability": None if prob is None else float(prob) / 100.0,
        "humidity": humidity,
        "summary": (period.get("shortForecast") or None),
        "alerts": _alerts_for_kickoff(alerts, kick),
        "issued_at": observed_at or datetime.now(UTC),
    }


def _period_covering(periods: list[dict[str, Any]], kickoff: datetime) -> dict[str, Any] | None:
    for period in periods:
        start = _parse_time(period.get("startTime"))
        end = _parse_time(period.get("endTime"))
        if start is None or end is None:
            continue
        if start <= kickoff < end:
            return period
    return None


def _alerts_for_kickoff(alerts: list[dict[str, Any]], kickoff: datetime) -> list[dict[str, Any]]:
    window_start = kickoff - timedelta(hours=6)
    window_end = kickoff + timedelta(hours=4)
    kept: list[dict[str, Any]] = []
    for raw in alerts:
        onset = _parse_time(raw.get("onset") or raw.get("effective"))
        ends = _parse_time(raw.get("ends") or raw.get("expires"))
        if onset and ends and (ends < window_start or onset > window_end):
            continue
        if ends and ends < window_start:
            continue
        event = raw.get("event") or "Alert"
        kept.append(
            {
                "event": str(event)[:80],
                "headline": (raw.get("headline") or "")[:240],
                "severity": raw.get("severity"),
                "urgency": raw.get("urgency"),
                "onset": onset.isoformat() if onset else None,
                "ends": ends.isoformat() if ends else None,
            }
        )
        if len(kept) >= 8:
            break
    return kept


def _parse_mph(value: Any) -> float | None:
    if value is None:
        return None
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", str(value))]
    if not nums:
        return None
    return max(nums)


def _mph_to_mps(mph: float | None) -> float | None:
    if mph is None:
        return None
    return mph * 0.44704


def _to_c(temp: Any, unit: str) -> float | None:
    if temp is None:
        return None
    try:
        value = float(temp)
    except (TypeError, ValueError):
        return None
    if str(unit).upper().startswith("C"):
        return value
    return (value - 32.0) * 5.0 / 9.0


def _wind_dir(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if text in _WIND:
        return _WIND[text]
    try:
        return float(text)
    except ValueError:
        return None


def _nested_value(node: Any) -> float | None:
    if isinstance(node, dict):
        node = node.get("value")
    if node is None:
        return None
    try:
        return float(node)
    except (TypeError, ValueError):
        return None


def _precip_mm(period: dict[str, Any]) -> float | None:
    for key in ("quantitativePrecipitation", "precipAmount"):
        raw = _nested_value(period.get(key))
        if raw is None:
            continue
        unit = ""
        node = period.get(key)
        if isinstance(node, dict):
            unit = str(node.get("unitCode") or "")
        if "inch" in unit or unit.endswith(":in"):
            return raw * 25.4
        return raw
    return None


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
