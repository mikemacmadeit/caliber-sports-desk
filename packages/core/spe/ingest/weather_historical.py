"""Historical weather coverage.

NWS is live forecast only. nflverse schedule temp/wind is observed/gamebook, not a pregame forecast.
Open-Meteo historical forecast-as-of may need a Professional license.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from spe.config import settings
from spe.db.models import Game, WeatherSnapshot
from spe.ingest.store import upsert_coverage


def record_weather_coverage(session: Session) -> dict[str, Any]:
    observed = (
        session.query(func.count(WeatherSnapshot.id))
        .filter(WeatherSnapshot.quality_status == "observed_not_forecast")
        .scalar()
        or 0
    )
    nws_rows = (
        session.query(func.count(WeatherSnapshot.id))
        .filter(WeatherSnapshot.quality_status == "nws_forecast")
        .scalar()
        or 0
    )
    upsert_coverage(
        session,
        provider="nflverse",
        dataset="observed_game_weather",
        league_id="nfl",
        season_year=0,
        row_count=int(observed),
        status="available" if observed else "empty",
        notes="Observed kickoff weather from the schedule. Using it as if known before kickoff is leakage.",
    )
    upsert_coverage(
        session,
        provider="nws",
        dataset="live_forecast",
        league_id="nfl",
        season_year=0,
        row_count=int(nws_rows),
        status="available" if nws_rows else "live_only",
        notes="NWS hourly forecast, wind, precipitation chance, and alerts. It is not a historical archive.",
    )
    upsert_coverage(
        session,
        provider="nws",
        dataset="live_forecast",
        league_id="fbs",
        season_year=0,
        row_count=int(nws_rows),
        status="available" if nws_rows else "live_only",
        notes="NWS coverage varies by campus venue. Missing a forecast is not fair-weather.",
    )
    def _forecast_rows(league_id: str) -> int:
        return (
            session.query(func.count(WeatherSnapshot.id))
            .join(Game, Game.game_id == WeatherSnapshot.game_id)
            .filter(WeatherSnapshot.provider == "open_meteo", Game.league_id == league_id)
            .scalar()
            or 0
        )

    om_rows = _forecast_rows("nfl")
    fbs_rows = _forecast_rows("fbs")
    if om_rows:
        om_status = "available_noncommercial"
    elif settings.open_meteo_enabled:
        om_status = "not_ingested"
    else:
        om_status = "disabled"
    om_note = (
        "previous_day1 is a 24-hour lead forecast on the free noncommercial tier. "
        "Commercial use needs a paid plan. Observed weather is a different row and is not relabeled."
    )
    upsert_coverage(
        session,
        provider="open_meteo",
        dataset="historical_forecast",
        league_id="nfl",
        season_year=0,
        row_count=int(om_rows),
        status=om_status,
        notes=om_note,
    )
    upsert_coverage(
        session,
        provider="open_meteo",
        dataset="historical_forecast",
        league_id="fbs",
        season_year=0,
        row_count=int(fbs_rows),
        status=om_status if not fbs_rows else "available_noncommercial",
        notes="FBS forecasts require venue coordinates. Campus sites without a coordinate stay missing.",
    )
    session.flush()
    return {
        "observed_nfl_weather_rows": int(observed),
        "nws_forecast_rows": int(nws_rows),
        "historical_forecast_status": om_status,
    }
