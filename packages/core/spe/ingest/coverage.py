from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from spe.adapters.cfbd import CfbdAdapter
from spe.adapters.odds_api import OddsApiAdapter
from spe.config import settings
from spe.db.models import (
    BacktestRun,
    DataCoverage,
    FeatureRow,
    Game,
    IngestRun,
    Player,
    PlayerAnalytics,
    PlayerGameStat,
    Play,
    Team,
    TeamAnalytics,
)
from spe.ingest.catalog import catalog_payload
from spe.ingest.quality import integration_quality


def coverage_report(session: Session) -> dict[str, Any]:
    games_nfl = session.query(func.count(Game.game_id)).filter(Game.league_id == "nfl", Game.data_tier == "historical").scalar() or 0
    games_fbs = session.query(func.count(Game.game_id)).filter(Game.league_id == "fbs", Game.data_tier == "historical").scalar() or 0
    games_demo = session.query(func.count(Game.game_id)).filter(Game.data_tier == "demo").scalar() or 0
    teams_nfl = session.query(func.count(Team.team_id)).filter(Team.league_id == "nfl").scalar() or 0
    teams_fbs = session.query(func.count(Team.team_id)).filter(Team.league_id == "fbs").scalar() or 0
    stats = session.query(func.count(PlayerGameStat.id)).scalar() or 0
    plays = session.query(func.count(Play.play_pk)).scalar() or 0
    players = session.query(func.count(Player.player_id)).scalar() or 0
    rows = session.query(DataCoverage).order_by(DataCoverage.league_id, DataCoverage.provider, DataCoverage.dataset).all()
    runs = session.query(IngestRun).order_by(IngestRun.started_at.desc()).limit(25).all()
    cfbd = CfbdAdapter()
    odds = OddsApiAdapter()
    return {
        "catalog": catalog_payload(),
        "credentials": {
            "cfbd_api_key": bool(settings.cfbd_api_key),
            "odds_api_key": bool(settings.odds_api_key),
            "open_meteo_enabled": settings.open_meteo_enabled,
            "noaa_cdo_token": bool(settings.noaa_cdo_token),
            "api_sports_key": bool(settings.api_sports_key),
            "openai_api_key": bool(settings.openai_api_key),
        },
        "quality": integration_quality(session),
        "counts": {
            "historical_nfl_games": games_nfl,
            "historical_fbs_games": games_fbs,
            "demo_games": games_demo,
            "nfl_teams": teams_nfl,
            "fbs_teams": teams_fbs,
            "players": players,
            "player_game_stats": stats,
            "plays": plays,
            "team_analytics": session.query(func.count(TeamAnalytics.id)).scalar() or 0,
            "player_analytics": session.query(func.count(PlayerAnalytics.id)).scalar() or 0,
            "pregame_features": session.query(func.count(FeatureRow.feature_id)).scalar() or 0,
            "backtest_runs": session.query(func.count(BacktestRun.run_id)).scalar() or 0,
        },
        "coverage": [
            {
                "provider": r.provider,
                "dataset": r.dataset,
                "league_id": r.league_id,
                "season_year": r.season_year,
                "row_count": r.row_count,
                "status": r.status,
                "last_ingested_at": r.last_ingested_at.isoformat() if r.last_ingested_at else None,
                "notes": r.notes,
            }
            for r in rows
        ],
        "recent_runs": [
            {
                "run_id": r.run_id,
                "provider": r.provider,
                "dataset": r.dataset,
                "league_id": r.league_id,
                "season_year": r.season_year,
                "status": r.status,
                "rows_upserted": r.rows_upserted,
                "rows_skipped": r.rows_skipped,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "notes": r.notes,
                "error": r.error,
            }
            for r in runs
        ],
        "blocked_without_credentials": [
            {
                "provider": "cfbd",
                "configured": cfbd.health()["configured"],
                "needed_for": "NCAA FBS historical games, rosters, PPA, player stats",
            },
            {
                "provider": "odds_api",
                "configured": odds.health()["configured"],
                "needed_for": "historical sportsbook prices and player props (paid historical endpoint)",
            },
        ],
        "unavailable_without_license": [
            "player route charts (play-level NGS route text is not a player assignment)",
            "WR-CB coverage assignments",
            "NFL tracking coordinates / Big Data Bowl",
            "historical player-prop ticks before 2023-05-03",
            "Open-Meteo commercial use, and forecast hours before 2024-01-01",
            "NOAA CDO observations until NOAA_CDO_TOKEN is set",
        ],
        "forecast_status": "demo_and_experimental_only",
        "note": "Historical box scores are not validated predictions. Do not treat demo probabilities as live forecasts.",
    }
