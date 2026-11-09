"""Historical kickoff forecasts from Open-Meteo's previous-day1 archive.

The issue time is the valid hour minus 24 hours. That is the API's fixed lead,
not a National Weather Service bulletin time. Games before 2024-01-01 are left
missing. Observed nflverse weather is not rewritten.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from spe.adapters.base import AdapterError
from spe.adapters.cfbd import CfbdAdapter
from spe.adapters.open_meteo import OpenMeteoAdapter
from spe.config import settings
from spe.db.base import as_utc
from spe.db.models import Game, Team, Venue, WeatherSnapshot
from spe.ingest.nws_live import backfill_venue_coordinates
from spe.ingest.stadiums import _norm
from spe.ingest.store import upsert_coverage
from spe.ingest.values import as_float, as_str

PROVIDER = "open_meteo"
ARCHIVE_START = date(2024, 1, 1)
LEAD = timedelta(hours=24)
FIELDS = {
    "temperature_2m_previous_day1": "temperature_c",
    "wind_speed_10m_previous_day1": "wind_mps",
    "wind_gusts_10m_previous_day1": "gust_mps",
    "precipitation_previous_day1": "precip_mm",
    "wind_direction_10m_previous_day1": "wind_dir_deg",
    "relative_humidity_2m_previous_day1": "humidity",
}


def _cache_path() -> Path:
    path = Path(settings.parquet_dir).resolve().parent / "cache" / "cfbd_venues.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ESPN scoreboard names that no longer match the CFBD venue name.
_VENUE_ALIASES: dict[str, tuple[str, str | None]] = {
    "gies memorial stadium": ("memorial stadium champaign il", None),
    "casino del sol stadium": ("arizona stadium", "tucson"),
    "our lady of lourdes stadium": ("cajun field", None),
    "galaxy stadium": ("jones at t stadium", None),
}

# Longer hints first so Arizona State does not inherit Arizona's city.
_HOME_CITIES: tuple[tuple[str, str], ...] = (
    ("arizona state", "tempe"),
    ("washington state", "pullman"),
    ("texas tech", "lubbock"),
    ("northwestern", "evanston"),
    ("kennesaw", "kennesaw"),
    ("missouri", "columbia"),
    ("washington", "seattle"),
    ("illinois", "champaign"),
    ("arizona", "tucson"),
    ("kansas", "lawrence"),
    ("louisiana", "lafayette"),
)


def apply_venue_coordinates(
    session: Session,
    venue_records: list[dict[str, Any]] | None = None,
    *,
    refresh: bool = False,
) -> dict[str, int]:
    """Fill venues that are still at 0,0. NFL sites use the stadium table. FBS uses CFBD."""
    nfl = backfill_venue_coordinates(session)
    records = _venue_records(venue_records, refresh=refresh)
    indexed = _index_venues(records or [])
    fbs = surfaces = 0
    for venue in session.query(Venue).all():
        chosen = _choose_venue_records(indexed, venue.name, _home_names(session, venue.venue_id))
        if not chosen:
            continue
        if abs(venue.lat) < 0.01 and abs(venue.lon) < 0.01:
            rec = chosen[0]
            venue.lat = float(rec["latitude"])
            venue.lon = float(rec["longitude"])
            elevation = as_float(rec.get("elevation"))
            if elevation is not None:
                venue.altitude_m = elevation
            if rec.get("timezone"):
                venue.timezone = str(rec["timezone"])[:64]
            fbs += 1
        if any(rec.get("dome") is True for rec in chosen):
            venue.roof_type = "dome"
        grasses = {rec.get("grass") for rec in chosen if rec.get("grass") is not None}
        if (venue.surface or "unknown") == "unknown" and len(grasses) == 1:
            venue.surface = "grass" if grasses.pop() else "turf"
            surfaces += 1
    session.flush()
    return {"nfl_stadium_table": nfl, "cfbd_venues": fbs, "surfaces": surfaces}


def _venue_records(venue_records: list[dict[str, Any]] | None, *, refresh: bool) -> list[dict[str, Any]] | None:
    if venue_records is not None:
        return venue_records
    if not settings.cfbd_api_key:
        cache = _cache_path()
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        return None
    cache = _cache_path()
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))
    fetched = CfbdAdapter().fetch("venues")
    cache.write_text(json.dumps(fetched.records), encoding="utf-8")
    return fetched.records


def _index_venues(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    indexed: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        name = as_str(rec.get("name"))
        if not name or as_float(rec.get("latitude")) is None or as_float(rec.get("longitude")) is None:
            continue
        indexed.setdefault(_norm(name), []).append(rec)
    return indexed


def _home_names(session: Session, venue_id: str) -> set[str]:
    rows = (
        session.query(Team.name)
        .join(Game, Game.home_team_id == Team.team_id)
        .filter(Game.venue_id == venue_id)
        .distinct()
        .all()
    )
    return {_norm(name) for (name,) in rows}


def _choose_venue_records(
    indexed: dict[str, list[dict[str, Any]]],
    venue_name: str,
    home_names: set[str],
) -> list[dict[str, Any]]:
    key = _norm(venue_name)
    alias = _VENUE_ALIASES.get(key)
    if alias:
        target, city = alias
        rows = list(indexed.get(target, []))
        if city:
            rows = [rec for rec in rows if _norm(rec.get("city")) == city]
    elif key in indexed:
        rows = list(indexed[key])
    else:
        rows = []
        for cfbd_key, recs in indexed.items():
            if len(cfbd_key) >= 12 and cfbd_key in key:
                rows.extend(recs)
    return _narrow_venues(rows, home_names)


def _narrow_venues(records: list[dict[str, Any]], home_names: set[str]) -> list[dict[str, Any]]:
    if len(records) <= 1:
        return records
    cities: list[str] = []
    for token in home_names:
        for hint, city in _HOME_CITIES:
            if hint in token:
                cities.append(city)
                break
    unique = set(cities)
    if len(unique) == 1:
        city = next(iter(unique))
        narrowed = [rec for rec in records if _norm(rec.get("city")) == city]
        if narrowed:
            records = narrowed
    if len(records) <= 1:
        return records
    coords = {(round(float(rec["latitude"]), 2), round(float(rec["longitude"]), 2)) for rec in records}
    if len(coords) == 1:
        return records[:1]
    return []


def _series(payload: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    out: dict[str, dict[str, float | None]] = {}
    for index, stamp in enumerate(times):
        values = {}
        for source, target in FIELDS.items():
            column = hourly.get(source) or []
            raw = column[index] if index < len(column) else None
            values[target] = as_float(raw)
        out[str(stamp)[:16]] = values
    return out


def _chunks(start: date, end: date, span: int = 120):
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + timedelta(days=span))
        yield cursor, stop
        cursor = stop + timedelta(days=1)


def forecast_row(
    game: Game,
    venue: Venue,
    series: dict[str, dict[str, float | None]],
    *,
    observed_at: datetime,
) -> dict[str, Any] | None:
    kick = as_utc(game.kickoff_utc).replace(minute=0, second=0, microsecond=0)
    if kick.date() < ARCHIVE_START:
        return None
    values = series.get(kick.strftime("%Y-%m-%dT%H:%M"))
    if not values or all(value is None for value in values.values()):
        return None
    issued = kick - LEAD
    if issued >= kick:
        return None
    return {
        "id": str(uuid4()),
        "venue_id": venue.venue_id,
        "game_id": game.game_id,
        "forecast_for": kick,
        "issued_at": issued,
        "temperature_c": values.get("temperature_c"),
        "wind_mps": values.get("wind_mps"),
        "gust_mps": values.get("gust_mps"),
        "wind_dir_deg": values.get("wind_dir_deg"),
        "precip_mm": values.get("precip_mm"),
        "humidity": values.get("humidity"),
        "roof_status": venue.roof_type or "open",
        "provider": PROVIDER,
        "provider_record_id": f"prev24:{game.game_id}"[:160],
        "observed_at": observed_at,
        "effective_at": issued,
        "ingested_at": observed_at,
        "revision": 1,
        "quality_status": "forecast_lead_24h",
    }


def ingest_historical_forecasts(
    session: Session,
    *,
    venue_records: list[dict[str, Any]] | None = None,
    adapter: OpenMeteoAdapter | None = None,
    refresh_venues: bool = False,
) -> dict[str, Any]:
    if not settings.open_meteo_enabled:
        for league in ("nfl", "fbs"):
            upsert_coverage(
                session,
                provider=PROVIDER,
                dataset="historical_forecast",
                league_id=league,
                season_year=0,
                row_count=0,
                status="disabled",
                notes="OPEN_METEO_ENABLED is false. Free use is noncommercial CC BY 4.0. Commercial use needs a paid plan.",
            )
        session.flush()
        return {"status": "disabled", "rows": 0}

    located = apply_venue_coordinates(session, venue_records, refresh=refresh_venues)
    observed = datetime.now(UTC)
    games = (
        session.query(Game)
        .filter(Game.data_tier == "historical", Game.league_id.in_(("nfl", "fbs")))
        .all()
    )
    venues = {venue.venue_id: venue for venue in session.query(Venue)}
    eligible = []
    missing_coords = 0
    before_archive = 0
    for game in games:
        kick = as_utc(game.kickoff_utc)
        if kick.date() < ARCHIVE_START:
            before_archive += 1
            continue
        venue = venues.get(game.venue_id)
        if venue is None or abs(venue.lat) < 0.01 and abs(venue.lon) < 0.01:
            missing_coords += 1
            continue
        eligible.append(game)

    by_point: dict[tuple[float, float], list[Game]] = {}
    for game in eligible:
        venue = venues[game.venue_id]
        by_point.setdefault((round(venue.lat, 3), round(venue.lon, 3)), []).append(game)

    client = adapter or OpenMeteoAdapter()
    written = 0
    missed_hour = 0
    errors: list[str] = []
    game_ids = [game.game_id for game in eligible]
    for start in range(0, len(game_ids), 400):
        session.query(WeatherSnapshot).filter(
            WeatherSnapshot.provider == PROVIDER,
            WeatherSnapshot.game_id.in_(game_ids[start : start + 400]),
        ).delete(synchronize_session=False)
    session.flush()

    for (lat, lon), point_games in by_point.items():
        kicks = [as_utc(game.kickoff_utc).date() for game in point_games]
        series: dict[str, dict[str, float | None]] = {}
        try:
            for start, end in _chunks(min(kicks), max(kicks)):
                payload = client.previous_day(lat, lon, start, end)
                series.update(_series(payload))
        except AdapterError as exc:
            errors.append(str(exc)[:180])
            continue
        rows = []
        for game in point_games:
            row = forecast_row(game, venues[game.venue_id], series, observed_at=observed)
            if row is None:
                missed_hour += 1
                continue
            rows.append(row)
        if rows:
            session.execute(WeatherSnapshot.__table__.insert(), rows)
            written += len(rows)
            session.flush()

    notes = (
        f"previous_day1 lead. issued_at = kickoff hour minus 24h. "
        f"Archive starts {ARCHIVE_START.isoformat()}. before_archive={before_archive} "
        f"missing_coordinates={missing_coords} missed_hour={missed_hour} errors={len(errors)}. "
        "Commercial use is not licensed. Sub-hour kickoff is matched to the containing UTC hour, not interpolated."
    )
    for league in ("nfl", "fbs"):
        count = sum(1 for game in eligible if game.league_id == league) if False else None
        league_rows = (
            session.query(WeatherSnapshot)
            .join(Game, Game.game_id == WeatherSnapshot.game_id)
            .filter(WeatherSnapshot.provider == PROVIDER, Game.league_id == league)
            .count()
        )
        upsert_coverage(
            session,
            provider=PROVIDER,
            dataset="historical_forecast",
            league_id=league,
            season_year=0,
            row_count=league_rows,
            status="available_noncommercial" if league_rows else "partial",
            notes=notes,
        )
        _ = count
    session.flush()
    return {
        "status": "available_noncommercial",
        "rows": written,
        "located": located,
        "before_archive": before_archive,
        "missing_coordinates": missing_coords,
        "missed_hour": missed_hour,
        "errors": errors[:8],
    }
