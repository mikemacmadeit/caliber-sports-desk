"""Live National Weather Service forecasts for stadiums.

NWS is a forecast feed, not a historical archive. Observed nflverse temp/wind
stays tagged observed_not_forecast and is never rewritten into a pregame forecast.
Games outside the US point grid are skipped rather than filled.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, joinedload

from spe.adapters.base import AdapterError
from spe.adapters.nws import NwsAdapter, assemble_conditions
from spe.db.base import as_utc
from spe.db.models import Game, Venue, WeatherSnapshot
from spe.ingest.stadiums import lookup_stadium
from spe.ingest.store import upsert_coverage

BOARD_DAYS = 8
PROVIDER = "nws"


def board_tier(status: str, kickoff: datetime, now: datetime) -> str:
    kick = as_utc(kickoff)
    moment = as_utc(now)
    if status == "final" or kick < moment - timedelta(hours=6):
        return "historical"
    if kick <= moment + timedelta(days=BOARD_DAYS):
        return "upcoming"
    return "season"


def refresh_board_tiers(session: Session, *, now: datetime | None = None) -> int:
    """Keep the live board to the next week. Demo and final games stay put."""
    moment = now or datetime.now(UTC)
    changed = 0
    games = session.query(Game).filter(Game.data_tier.in_(("upcoming", "season"))).all()
    for game in games:
        tier = board_tier(game.status, game.kickoff_utc, moment)
        if game.data_tier != tier:
            game.data_tier = tier
            changed += 1
    return changed


def backfill_venue_coordinates(session: Session) -> int:
    updated = 0
    for venue in session.query(Venue).all():
        if abs(venue.lat) > 0.01 or abs(venue.lon) > 0.01:
            continue
        site = lookup_stadium(None, venue.name)
        if site is None:
            continue
        venue.lat = site.lat
        venue.lon = site.lon
        venue.altitude_m = site.altitude_m
        venue.timezone = site.timezone
        updated += 1
    return updated


def _commit_when_free(session: Session, attempts: int = 90) -> None:
    """Write when the price pull lets go. A failed try does not keep the lock."""
    from sqlalchemy.exc import OperationalError

    for _ in range(attempts):
        pending = list(session.new)
        try:
            session.commit()
            return
        except OperationalError:
            session.rollback()
            for obj in pending:
                session.add(obj)
            time.sleep(2)
    session.commit()


def ingest_nws_forecasts(
    session: Session,
    *,
    now: datetime | None = None,
    horizon_hours: int = BOARD_DAYS * 24,
    adapter: NwsAdapter | None = None,
    pause_s: float = 0.2,
    min_interval: timedelta = timedelta(minutes=90),
) -> dict[str, Any]:
    moment = now or datetime.now(UTC)
    from spe.ingest.open_meteo_history import apply_venue_coordinates

    placed = apply_venue_coordinates(session)
    located = int(placed.get("nfl_stadium_table", 0)) + int(placed.get("cfbd_venues", 0))
    refresh_board_tiers(session, now=moment)
    horizon = moment + timedelta(hours=horizon_hours)
    recent = moment - timedelta(hours=6)
    games = (
        session.query(Game)
        .options(joinedload(Game.venue))
        .filter(Game.status != "final")
        .all()
    )
    # Drop the read before the weather calls. A long read blocks the price pull.
    _commit_when_free(session)
    client = adapter or NwsAdapter()
    cache: dict[tuple[float, float], tuple[list[dict[str, Any]], list[dict[str, Any]]] | str] = {}
    pending: list[WeatherSnapshot] = []
    upserted = alerts = 0
    skipped_coords = skipped_coverage = skipped_horizon = skipped_fresh = 0
    pause = pause_s if adapter is None else 0.0
    observed = datetime.now(UTC)

    for game in games:
        kick = as_utc(game.kickoff_utc)
        if kick < recent or kick > horizon:
            continue
        if game.data_tier == "historical":
            continue
        venue = game.venue
        if venue is None or (abs(venue.lat) < 0.01 and abs(venue.lon) < 0.01):
            skipped_coords += 1
            continue
        if _forecast_is_fresh(session, game.game_id, moment, min_interval):
            skipped_fresh += 1
            session.rollback()
            continue
        # End the freshness read before the network call so prices can keep writing.
        game_id = game.game_id
        venue_id = venue.venue_id
        roof = _roof(venue)
        lat = float(venue.lat)
        lon = float(venue.lon)
        session.rollback()
        key = (round(lat, 4), round(lon, 4))
        if key not in cache:
            if cache:
                time.sleep(pause)
            try:
                cache[key] = client.fetch_bundle(lat, lon)
            except AdapterError as exc:
                cache[key] = "outside" if "coverage unavailable" in str(exc) else "error"
        cached = cache[key]
        if cached == "outside":
            skipped_coverage += 1
            continue
        if cached == "error":
            skipped_horizon += 1
            continue
        periods, alert_props = cached
        parsed = assemble_conditions(periods, alert_props, kick, observed_at=observed)
        if parsed is None:
            skipped_horizon += 1
            continue
        issued = parsed["issued_at"]
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=UTC)
        pending.append(
            WeatherSnapshot(
                venue_id=venue_id,
                game_id=game_id,
                forecast_for=kick,
                issued_at=issued,
                temperature_c=parsed["temperature_c"],
                wind_mps=parsed["wind_mps"],
                gust_mps=parsed["gust_mps"],
                wind_dir_deg=parsed["wind_dir_deg"],
                precip_mm=parsed["precip_mm"],
                precip_probability=parsed["precip_probability"],
                humidity=parsed["humidity"],
                summary=((parsed.get("summary") or "")[:160] or None),
                alerts=parsed.get("alerts") or [],
                roof_status=roof,
                provider=PROVIDER,
                provider_record_id=f"nws:{game_id}:{issued.strftime('%Y%m%d%H%M')}"[:160],
                observed_at=issued,
                effective_at=issued,
                quality_status="nws_forecast",
            )
        )
        upserted += 1
        alerts += len(parsed.get("alerts") or [])

    for row in pending:
        session.add(row)
    for league_id in ("nfl", "fbs"):
        upsert_coverage(
            session,
            provider=PROVIDER,
            dataset="live_forecast",
            league_id=league_id,
            season_year=0,
            row_count=upserted,
            status="available" if upserted else "live_only",
            notes=(
                "NWS hourly forecast, wind, precipitation chance, and active alerts "
                "for stadium coordinates. No key. Not a historical archive. "
                f"Outside coverage skipped={skipped_coverage}. No coordinates skipped={skipped_coords}."
            ),
        )
    _commit_when_free(session)
    return {
        "forecasts_upserted": upserted,
        "alerts": alerts,
        "venues_located": located,
        "skipped_no_coordinates": skipped_coords,
        "skipped_outside_coverage": skipped_coverage,
        "skipped_beyond_horizon": skipped_horizon,
        "skipped_fresh": skipped_fresh,
    }


def _forecast_is_fresh(session: Session, game_id: str, now: datetime, interval: timedelta) -> bool:
    if interval.total_seconds() <= 0:
        return False
    row = (
        session.query(WeatherSnapshot)
        .filter(
            WeatherSnapshot.game_id == game_id,
            WeatherSnapshot.quality_status == "nws_forecast",
        )
        .order_by(WeatherSnapshot.issued_at.desc())
        .first()
    )
    if row is None:
        return False
    issued = as_utc(row.issued_at)
    return issued is not None and (as_utc(now) - issued) < interval


def _roof(venue: Venue) -> str:
    roof = (venue.roof_type or "open").lower()
    if roof in {"dome", "closed"}:
        return "closed"
    if "retract" in roof:
        return "retractable"
    return "open"
