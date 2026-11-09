"""Availability audit for sources already stored. Does not fetch new data.

Open-Meteo previous-day values are the archive's one-day-lead field.
This database does not have an issue timestamp from that API.
The stored issued_at is the valid hour minus 24 hours, which is an inference.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.licensing import LICENSES

OPEN_METEO_INFERRED_LEAD = timedelta(hours=24)


def issue_time_is_inferred(issued_at, forecast_for, *, lead: timedelta = OPEN_METEO_INFERRED_LEAD) -> bool:
    """True when the stored issue time is exactly the valid time minus a fixed lead."""
    if issued_at is None or forecast_for is None:
        return False
    try:
        delta = abs((forecast_for - issued_at).total_seconds() - lead.total_seconds())
    except TypeError:
        return False
    return delta < 60


def audit_sources(session: Session) -> dict[str, Any]:
    conn = session.connection()
    weather = conn.execute(
        text(
            """
            SELECT w.provider, w.quality_status, count(*) AS rows,
                   sum(CASE WHEN abs((julianday(w.issued_at) - julianday(w.forecast_for)) * 24 + 24) < 0.02 THEN 1 ELSE 0 END) AS inferred_24h,
                   sum(CASE WHEN w.issued_at < g.kickoff_utc THEN 1 ELSE 0 END) AS issued_before_kickoff,
                   sum(CASE WHEN w.temperature_c IS NULL THEN 1 ELSE 0 END) AS missing_temperature,
                   sum(CASE WHEN w.wind_mps IS NULL THEN 1 ELSE 0 END) AS missing_wind,
                   min(w.issued_at) AS issued_min,
                   max(w.issued_at) AS issued_max
            FROM weather_snapshots w
            JOIN games g ON g.game_id = w.game_id
            GROUP BY w.provider, w.quality_status
            """
        )
    ).mappings().all()
    news = conn.execute(
        text(
            """
            SELECT league_id, count(*) AS rows,
                   sum(CASE WHEN verified_injury THEN 1 ELSE 0 END) AS verified_injury_rows,
                   min(seen_at) AS seen_min, max(seen_at) AS seen_max
            FROM news_articles
            GROUP BY league_id
            """
        )
    ).mappings().all()
    ngs = conn.execute(
        text(
            """
            SELECT count(*) AS rows,
                   sum(CASE WHEN week = 0 OR quality_status = 'season_aggregate_not_pregame' THEN 1 ELSE 0 END) AS week0,
                   min(season_year) AS season_min, max(season_year) AS season_max
            FROM ngs_weeks
            """
        )
    ).mappings().one()
    usage = conn.execute(
        text(
            """
            SELECT
              count(*) AS rows,
              sum(json_extract(metrics, '$.offense_snap_share_roll3') IS NOT NULL) AS snap_roll3,
              sum(json_extract(metrics, '$.offense_snap_share_delta_3v10') IS NOT NULL) AS snap_delta,
              sum(json_extract(metrics, '$.depth_rank_prior_week') IS NOT NULL) AS depth_rank,
              sum(json_extract(metrics, '$.starter_prior_week') IS NOT NULL) AS starter,
              sum(json_extract(metrics, '$.roster_status_prior_week') IS NOT NULL) AS roster_status,
              sum(json_extract(metrics, '$.ngs_avg_separation') IS NOT NULL) AS separation,
              sum(json_extract(metrics, '$.ngs_avg_cushion') IS NOT NULL) AS cushion,
              sum(json_extract(metrics, '$.ngs_avg_yac_above_expectation') IS NOT NULL) AS yac_oe,
              sum(json_extract(metrics, '$.ngs_rush_yards_over_expected') IS NOT NULL) AS ryoe
            FROM player_analytics
            WHERE league_id = 'nfl' AND grain = 'usage_pregame'
            """
        )
    ).mappings().one()
    return {
        "weather": [dict(row) for row in weather],
        "news": [dict(row) for row in news],
        "ngs_weeks": dict(ngs),
        "nfl_usage_pregame": dict(usage),
        "decisions": {
            "open_meteo": "rejected_inferred_issue_time",
            "noaa_cdo": "rejected_after_kickoff_observation",
            "gdelt": "rejected_unverified_unlinked_news",
            "nws_historical": "unavailable_live_forecasts_are_not_a_backtest",
            "ngs_week0": "rejected_season_aggregate",
            "participation_same_game": "rejected_available_only_after_kickoff",
        },
        "licenses": {
            key: {
                "license": LICENSES[key].license_name,
                "commercial_redisplay": LICENSES[key].commercial_redisplay,
                "notes": LICENSES[key].notes,
            }
            for key in ("nflverse", "open_meteo", "noaa_cdo", "gdelt", "nws")
        },
    }
