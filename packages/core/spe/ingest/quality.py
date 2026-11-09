"""Field-level coverage for the free-data integrations. Counts only stored rows."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from spe.db.models import (
    DepthChartWeek,
    NewsArticle,
    NgsWeek,
    ParticipationPlay,
    ParticipationPlayerGame,
    PlayerAnalytics,
    RosterWeek,
    TeamAnalytics,
    WeatherSnapshot,
)
from spe.licensing import LICENSES


def _span(session: Session, model, column) -> dict[str, Any]:
    low, high, count = session.query(func.min(column), func.max(column), func.count()).one()
    return {"min": low, "max": high, "rows": int(count or 0)}


def integration_quality(session: Session) -> dict[str, Any]:
    plays = session.query(func.count(ParticipationPlay.id)).scalar() or 0
    formation = (
        session.query(func.count(ParticipationPlay.id)).filter(ParticipationPlay.offense_formation.isnot(None)).scalar()
        or 0
    )
    route = session.query(func.count(ParticipationPlay.id)).filter(ParticipationPlay.route.isnot(None)).scalar() or 0
    man_zone = (
        session.query(func.count(ParticipationPlay.id))
        .filter(ParticipationPlay.defense_man_zone_type.isnot(None))
        .scalar()
        or 0
    )
    air = session.query(func.count(ParticipationPlay.id)).filter(ParticipationPlay.ngs_air_yards.isnot(None)).scalar() or 0
    player_games = session.query(func.count(ParticipationPlayerGame.id)).scalar() or 0
    ngs = _span(session, NgsWeek, NgsWeek.season_year)
    ngs_week0 = session.query(func.count(NgsWeek.id)).filter(NgsWeek.week == 0).scalar() or 0
    rosters = _span(session, RosterWeek, RosterWeek.season_year)
    depth = _span(session, DepthChartWeek, DepthChartWeek.season_year)
    depth_dated = (
        session.query(func.count(DepthChartWeek.id))
        .filter(DepthChartWeek.quality_status == "snapshot_timestamp")
        .scalar()
        or 0
    )
    news = session.query(func.count(NewsArticle.article_id)).scalar() or 0
    verified = session.query(func.count(NewsArticle.article_id)).filter(NewsArticle.verified_injury.is_(True)).scalar() or 0
    forecasts = (
        session.query(func.count(WeatherSnapshot.id)).filter(WeatherSnapshot.quality_status == "forecast_lead_24h").scalar()
        or 0
    )
    observations = (
        session.query(func.count(WeatherSnapshot.id))
        .filter(WeatherSnapshot.quality_status == "daily_observation_not_forecast")
        .scalar()
        or 0
    )
    usage_players = (
        session.query(func.count(PlayerAnalytics.id))
        .filter(PlayerAnalytics.source == "caliber_usage_v1", PlayerAnalytics.league_id == "nfl")
        .scalar()
        or 0
    )
    usage_teams = (
        session.query(func.count(TeamAnalytics.id))
        .filter(TeamAnalytics.source == "caliber_usage_v1", TeamAnalytics.league_id == "nfl")
        .scalar()
        or 0
    )
    integrations = [
        {
            "dataset": "nflverse participation",
            "provider": "nflverse",
            "league": "nfl",
            "rows": int(plays),
            "player_game_rows": int(player_games),
            "history": "2016-2025 files when the season is stored. 2026 participation is not in the release.",
            "present": {
                "offense_formation": int(formation),
                "play_level_route": int(route),
                "man_zone": int(man_zone),
                "ngs_air_yards": int(air),
            },
            "missing": [
                "player-identified routes",
                "who covered whom",
                "ngs_air_yards is empty for 2023, 2024, and 2025",
                "personnel strings are stored raw and are not standard 11/12 personnel codes",
            ],
            "analytics": [
                "prior-game offense snap share and 3-vs-10 change",
                "prior-game shotgun, under-center, and pistol share",
                "prior-game man/zone share from MAN_COVERAGE and ZONE_COVERAGE only",
            ],
            "license": LICENSES["nflverse"].license_name,
        },
        {
            "dataset": "nflverse next gen stats summaries",
            "provider": "nflverse",
            "league": "nfl",
            "rows": ngs["rows"],
            "seasons": [ngs["min"], ngs["max"]],
            "week0_season_aggregates": int(ngs_week0),
            "missing": [
                "tracking coordinates",
                "week 0 is a season aggregate and is excluded from pregame features",
                "FBS",
            ],
            "analytics": [
                "prior-week completion percentage above expectation",
                "prior-week average separation and yards after catch above expectation",
                "prior-week rush yards over expected, when the file has a value",
            ],
            "license": "CC-BY-4.0 via nflverse-data. Not an NFL tracking license.",
        },
        {
            "dataset": "nflverse weekly rosters and depth charts",
            "provider": "nflverse",
            "league": "nfl",
            "roster_rows": rosters["rows"],
            "depth_rows": depth["rows"],
            "depth_snapshot_rows": int(depth_dated),
            "seasons": [rosters["min"], rosters["max"]],
            "missing": [
                "publication timestamp on weekly files through 2024",
                "same-week weekly chart is withheld until 6 hours after that week's last kickoff",
                "FBS",
            ],
            "analytics": [
                "prior-week roster status and position",
                "prior-week depth rank and starter flag",
            ],
            "license": LICENSES["nflverse"].license_name,
        },
        {
            "dataset": "open-meteo previous-day forecast",
            "provider": "open_meteo",
            "league": "nfl and fbs where the venue has coordinates",
            "rows": int(forecasts),
            "history": "Previous-day1 archive used from 2024-01-01. Earlier games stay missing.",
            "missing": [
                "forecasts before 2024-01-01",
                "venues still at 0,0",
                "sub-hour interpolation",
                "commercial license",
            ],
            "analytics": [
                "kickoff-hour temperature, wind, gust, precipitation, humidity, and wind direction issued 24 hours earlier",
            ],
            "license": LICENSES["open_meteo"].license_name,
        },
        {
            "dataset": "noaa cdo daily observations",
            "provider": "noaa_cdo",
            "league": "nfl and fbs",
            "rows": int(observations),
            "missing": [
                "hourly kickoff observations",
                "stations farther than 40 km",
                "dome games are not matched to outdoor stations",
            ],
            "analytics": ["post-day temperature, wind, gust, and precipitation for research checks, not pregame features"],
            "license": LICENSES["noaa_cdo"].license_name,
        },
        {
            "dataset": "gdelt news",
            "provider": "gdelt",
            "league": "nfl and fbs",
            "rows": int(news),
            "verified_injury_rows": int(verified),
            "missing": ["complete archive", "player linkage", "verification"],
            "analytics": ["none in the model. Headlines are stored with seendate and are not injury designations."],
            "license": LICENSES["gdelt"].license_name,
        },
    ]
    return {
        "usage_pregame_player_rows": int(usage_players),
        "usage_pregame_team_rows": int(usage_teams),
        "integrations": integrations,
    }
