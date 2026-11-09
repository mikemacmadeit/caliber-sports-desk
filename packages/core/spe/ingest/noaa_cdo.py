"""NOAA Climate Data Online daily observations for stadiums.

These are station observations. They become available at the next local midnight
after the observation day, so they are not a kickoff forecast. No token means no rows.
A station farther than 40 km is left missing.
"""

from __future__ import annotations

import json
import math
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from spe.adapters.base import AdapterError
from spe.adapters.noaa_cdo import NoaaCdoAdapter
from spe.config import settings
from spe.db.base import as_utc
from spe.db.models import Game, Venue, WeatherSnapshot
from spe.ingest.store import upsert_coverage

PROVIDER = "noaa_cdo"
DATATYPES = {
    "TMAX": "tmax_c",
    "TMIN": "tmin_c",
    "PRCP": "precip_mm",
    "AWND": "wind_mps",
    "WSF2": "gust_mps",
}
MAX_STATION_KM = 40.0


def ingest_noaa_observations(
    session: Session,
    *,
    adapter: NoaaCdoAdapter | None = None,
    station_fixtures: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    client = adapter or NoaaCdoAdapter()
    if not settings.noaa_cdo_token and station_fixtures is None:
        probe = client.probe_without_token()
        for league in ("nfl", "fbs"):
            upsert_coverage(
                session,
                provider=PROVIDER,
                dataset="daily_observations",
                league_id=league,
                season_year=0,
                row_count=0,
                status="needs_key",
                notes=f"NOAA_CDO_TOKEN is empty. Live datasets probe: {probe}",
            )
        session.flush()
        return {"status": "needs_key", "rows": 0, "probe": probe}

    if station_fixtures is not None:
        return _store_fixtures(session, station_fixtures)
    return _pull_near_venues(session, client)


def _store_fixtures(session: Session, fixtures: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Store already-fetched GHCND rows. Values are converted from NOAA tenths, never filled."""
    observed = datetime.now(UTC)
    rows = []
    for game_id, observations in fixtures.items():
        game = session.get(Game, game_id)
        if game is None:
            continue
        venue = session.get(Venue, game.venue_id)
        if venue is None:
            continue
        by_type = {str(item.get("datatype")): item for item in observations}
        day = _local_day(game.kickoff_utc, venue.timezone)
        if day is None:
            continue
        issued = _available_at(day, venue.timezone)
        tmax = _tenths_c(by_type.get("TMAX"))
        tmin = _tenths_c(by_type.get("TMIN"))
        precip = _tenths_mm(by_type.get("PRCP"))
        wind = _tenths_mps(by_type.get("AWND"))
        gust = _tenths_mps(by_type.get("WSF2"))
        if all(value is None for value in (tmax, tmin, precip, wind, gust)):
            continue
        temperature = None if tmax is None or tmin is None else (tmax + tmin) / 2
        rows.append(
            {
                "id": str(uuid4()),
                "venue_id": venue.venue_id,
                "game_id": game.game_id,
                "forecast_for": datetime(day.year, day.month, day.day, tzinfo=UTC),
                "issued_at": issued,
                "temperature_c": temperature,
                "wind_mps": wind,
                "gust_mps": gust,
                "precip_mm": precip,
                "roof_status": venue.roof_type or "open",
                "provider": PROVIDER,
                "provider_record_id": f"ghcnd:{game.game_id}"[:160],
                "observed_at": observed,
                "effective_at": issued,
                "ingested_at": observed,
                "revision": 1,
                "quality_status": "daily_observation_not_forecast",
            }
        )
    if rows:
        session.execute(WeatherSnapshot.__table__.insert(), rows)
    for league in ("nfl", "fbs"):
        upsert_coverage(
            session,
            provider=PROVIDER,
            dataset="daily_observations",
            league_id=league,
            season_year=0,
            row_count=len(rows) if league == "nfl" else 0,
            status="available" if rows and league == "nfl" else "empty",
            notes=(
                "GHCND daily TMAX/TMIN/PRCP/AWND/WSF2. issued_at is the next UTC midnight, "
                "after the observation day. Not a kickoff forecast. "
                f"Datatypes requested when a token and station exist: {', '.join(DATATYPES)}."
            ),
        )
    session.flush()
    return {"status": "available" if rows else "empty", "rows": len(rows)}


def _tenths_c(item: dict[str, Any] | None) -> float | None:
    if not item or item.get("value") is None:
        return None
    return float(item["value"]) / 10.0


def _tenths_mm(item: dict[str, Any] | None) -> float | None:
    if not item or item.get("value") is None:
        return None
    return float(item["value"]) / 10.0


def _tenths_mps(item: dict[str, Any] | None) -> float | None:
    """NOAA wind datatypes are tenths of meters per second for AWND/WSF2 in GHCND metric."""
    if not item or item.get("value") is None:
        return None
    return float(item["value"]) / 10.0


def fetch_station_daily(
    adapter: NoaaCdoAdapter,
    station_id: str,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    payload = adapter.get(
        "/data",
        {
            "datasetid": "GHCND",
            "stationid": station_id,
            "startdate": start,
            "enddate": end,
            "datatypeid": list(DATATYPES),
            "units": "metric",
            "limit": 1000,
        },
    )
    results = payload.get("results") or []
    if not isinstance(results, list):
        raise AdapterError("NOAA CDO data payload was not a list")
    return results


def _local_day(kickoff: datetime, timezone_name: str | None):
    if not timezone_name:
        return None
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        return None
    return as_utc(kickoff).astimezone(zone).date()


def _available_at(day, timezone_name: str | None) -> datetime:
    try:
        zone = ZoneInfo(timezone_name) if timezone_name else UTC
    except Exception:
        zone = UTC
    return (datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(days=1)).astimezone(UTC)


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _choose_station(results: list[dict[str, Any]], lat: float, lon: float) -> tuple[str, float] | None:
    scored: list[tuple[float, str]] = []
    for rec in results:
        station_id = str(rec.get("id") or "")
        slat = rec.get("latitude")
        slon = rec.get("longitude")
        if not station_id or slat is None or slon is None:
            continue
        scored.append((_km(lat, lon, float(slat), float(slon)), station_id))
    scored.sort()
    airports = [item for item in scored if ":USW" in item[1] and item[0] <= MAX_STATION_KM]
    if airports:
        return airports[0][1], airports[0][0]
    near = [item for item in scored if item[0] <= MAX_STATION_KM]
    return (near[0][1], near[0][0]) if near else None


def _cache_file(name: str) -> Path:
    path = Path(settings.parquet_dir).resolve().parent / "cache" / "noaa" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _station_for(adapter: NoaaCdoAdapter, lat: float, lon: float) -> tuple[str, float] | None:
    key = f"station_{lat:.3f}_{lon:.3f}.json"
    cache = _cache_file(key)
    if cache.exists():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if not saved:
            return None
        return str(saved["id"]), float(saved["km"])
    delta = 0.45
    payload = adapter.get(
        "/stations",
        {
            "datasetid": "GHCND",
            "datatypeid": "TMAX",
            "extent": f"{lat - delta},{lon - delta},{lat + delta},{lon + delta}",
            "limit": 1000,
            "sortfield": "datacoverage",
            "sortorder": "desc",
        },
    )
    chosen = _choose_station(list(payload.get("results") or []), lat, lon)
    cache.write_text(
        json.dumps(None if chosen is None else {"id": chosen[0], "km": round(chosen[1], 2)}),
        encoding="utf-8",
    )
    time.sleep(0.25)
    return chosen


def _year_rows(adapter: NoaaCdoAdapter, station_id: str, year: int) -> list[dict[str, Any]]:
    cache = _cache_file(f"{station_id.replace(':', '')}_{year}.json")
    if cache.exists():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        return saved if isinstance(saved, list) else []
    rows: list[dict[str, Any]] = []
    offset = 1
    while True:
        payload = adapter.get(
            "/data",
            {
                "datasetid": "GHCND",
                "stationid": station_id,
                "startdate": f"{year}-01-01",
                "enddate": f"{year}-12-31",
                "datatypeid": list(DATATYPES),
                "units": "metric",
                "limit": 1000,
                "offset": offset,
            },
        )
        batch = payload.get("results") or []
        if not isinstance(batch, list):
            raise AdapterError("NOAA CDO data payload was not a list")
        rows.extend(batch)
        count = int(((payload.get("metadata") or {}).get("resultset") or {}).get("count") or 0)
        offset += len(batch)
        if not batch or offset > count:
            break
        time.sleep(0.25)
    cache.write_text(json.dumps(rows), encoding="utf-8")
    time.sleep(0.25)
    return rows


def _by_day(rows: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for item in rows:
        day = str(item.get("date") or "")[:10]
        datatype = str(item.get("datatype") or "")
        if len(day) != 10 or datatype not in DATATYPES or day in out and datatype in out[day]:
            continue
        out.setdefault(day, {})[datatype] = item
    return out


def _pull_near_venues(session: Session, adapter: NoaaCdoAdapter) -> dict[str, Any]:
    observed = datetime.now(UTC)
    venues = {venue.venue_id: venue for venue in session.query(Venue)}
    games = session.query(Game).filter(Game.data_tier == "historical").all()
    grouped: dict[str, list[Game]] = {}
    for game in games:
        grouped.setdefault(game.venue_id, []).append(game)

    by_station: dict[str, list[tuple[Game, Venue, float]]] = {}
    missing_coords = skipped_dome = missing_station = missing_timezone = 0
    errors: list[str] = []
    for venue_id, venue_games in grouped.items():
        venue = venues.get(venue_id)
        if venue is None or abs(venue.lat) < 0.01 and abs(venue.lon) < 0.01:
            missing_coords += len(venue_games)
            continue
        if (venue.roof_type or "") == "dome":
            skipped_dome += len(venue_games)
            continue
        try:
            chosen = _station_for(adapter, float(venue.lat), float(venue.lon))
        except AdapterError as exc:
            errors.append(f"{venue_id}: {exc}"[:180])
            missing_station += len(venue_games)
            continue
        if chosen is None:
            missing_station += len(venue_games)
            continue
        station_id, distance_km = chosen
        for game in venue_games:
            if _local_day(game.kickoff_utc, venue.timezone) is None:
                missing_timezone += 1
                continue
            by_station.setdefault(station_id, []).append((game, venue, distance_km))

    written = 0
    missed_day = 0
    for station_id, attached in by_station.items():
        years = sorted({_local_day(game.kickoff_utc, venue.timezone).year for game, venue, _distance in attached})
        days: dict[str, dict[str, dict[str, Any]]] = {}
        try:
            for year in years:
                days.update(_by_day(_year_rows(adapter, station_id, year)))
        except AdapterError as exc:
            errors.append(f"{station_id}: {exc}"[:180])
            continue
        game_ids = [game.game_id for game, _venue, _distance in attached]
        for start in range(0, len(game_ids), 400):
            session.query(WeatherSnapshot).filter(
                WeatherSnapshot.provider == PROVIDER,
                WeatherSnapshot.game_id.in_(game_ids[start : start + 400]),
            ).delete(synchronize_session=False)
        batch: list[dict[str, Any]] = []
        for game, venue, distance_km in attached:
            day = _local_day(game.kickoff_utc, venue.timezone)
            by_type = days.get(day.isoformat()) if day else None
            if not by_type:
                missed_day += 1
                continue
            tmax = _tenths_c(by_type.get("TMAX"))
            tmin = _tenths_c(by_type.get("TMIN"))
            precip = _tenths_mm(by_type.get("PRCP"))
            wind = _tenths_mps(by_type.get("AWND"))
            gust = _tenths_mps(by_type.get("WSF2"))
            if all(value is None for value in (tmax, tmin, precip, wind, gust)):
                missed_day += 1
                continue
            batch.append(
                {
                    "id": str(uuid4()),
                    "venue_id": venue.venue_id,
                    "game_id": game.game_id,
                    "forecast_for": datetime(day.year, day.month, day.day, tzinfo=UTC),
                    "issued_at": _available_at(day, venue.timezone),
                    "temperature_c": None if tmax is None or tmin is None else (tmax + tmin) / 2,
                    "wind_mps": wind,
                    "gust_mps": gust,
                    "precip_mm": precip,
                    "summary": f"{station_id} {distance_km:.1f}km"[:160],
                    "roof_status": venue.roof_type or "open",
                    "provider": PROVIDER,
                    "provider_record_id": f"ghcnd:{station_id}:{game.game_id}"[:160],
                    "observed_at": observed,
                    "effective_at": _available_at(day, venue.timezone),
                    "ingested_at": observed,
                    "revision": 1,
                    "quality_status": "daily_observation_not_forecast",
                }
            )
        if batch:
            session.bulk_insert_mappings(WeatherSnapshot, batch, render_nulls=True)
            written += len(batch)
        session.commit()
        print(f"noaa {station_id}: games={len(attached)} stored={len(batch)}", flush=True)

    notes = (
        "GHCND daily TMAX/TMIN/PRCP/AWND/WSF2 from the nearest station within 40 km. "
        "Airport stations are preferred. Dome games are not matched to outdoor stations. "
        "issued_at is the next local midnight, after the observation day. Not a kickoff forecast. "
        f"missing_coordinates={missing_coords} skipped_dome={skipped_dome} "
        f"missing_station={missing_station} missing_timezone={missing_timezone} "
        f"missed_day={missed_day} errors={len(errors)}."
    )
    for league in ("nfl", "fbs"):
        count = (
            session.query(WeatherSnapshot)
            .join(Game, Game.game_id == WeatherSnapshot.game_id)
            .filter(WeatherSnapshot.provider == PROVIDER, Game.league_id == league)
            .count()
        )
        upsert_coverage(
            session,
            provider=PROVIDER,
            dataset="daily_observations",
            league_id=league,
            season_year=0,
            row_count=count,
            status="available" if count else "empty",
            notes=notes,
        )
    session.flush()
    return {
        "status": "available" if written else "empty",
        "rows": written,
        "stations": len(by_station),
        "missing_coordinates": missing_coords,
        "skipped_dome": skipped_dome,
        "missing_station": missing_station,
        "missed_day": missed_day,
        "errors": errors[:8],
    }
