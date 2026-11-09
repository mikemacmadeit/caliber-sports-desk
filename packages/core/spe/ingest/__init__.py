"""Ingest package. Heavy modules load lazily to avoid circular imports with identity.resolve."""

from __future__ import annotations

from typing import Any

__all__ = [
    "append_raw",
    "build_player_features",
    "catalog_payload",
    "coverage_report",
    "feature_coverage",
    "ingest_fbs_history",
    "ingest_fbs_players",
    "ingest_nfl_history",
    "ingest_odds_history",
    "ingest_odds_live",
    "record_weather_coverage",
    "remaining_credits",
]


def __getattr__(name: str) -> Any:
    if name == "append_raw" or name == "remaining_credits":
        from spe.ingest.snapshots import append_raw, remaining_credits

        return append_raw if name == "append_raw" else remaining_credits
    if name == "build_player_features" or name == "feature_coverage":
        from spe.ingest.features import build_player_features, feature_coverage

        return build_player_features if name == "build_player_features" else feature_coverage
    if name == "catalog_payload":
        from spe.ingest.catalog import catalog_payload

        return catalog_payload
    if name == "coverage_report":
        from spe.ingest.coverage import coverage_report

        return coverage_report
    if name == "ingest_fbs_history":
        from spe.ingest.cfbd_historical import ingest_fbs_history

        return ingest_fbs_history
    if name == "ingest_fbs_players":
        from spe.ingest.cfbd_players import ingest_fbs_players

        return ingest_fbs_players
    if name == "ingest_nfl_history":
        from spe.ingest.nflverse_historical import ingest_nfl_history

        return ingest_nfl_history
    if name == "ingest_odds_history":
        from spe.ingest.odds_historical import ingest_odds_history

        return ingest_odds_history
    if name == "ingest_odds_live":
        from spe.ingest.odds_live import ingest_odds_live

        return ingest_odds_live
    if name == "record_weather_coverage":
        from spe.ingest.weather_historical import record_weather_coverage

        return record_weather_coverage
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
