from __future__ import annotations

from datetime import datetime
from typing import Any


REQUIRED_KEYS = (
    "available_as_of",
    "sample_size",
    "missingness",
    "source",
)


def feature_coverage(blob: dict[str, Any]) -> dict[str, Any]:
    missing = [k for k in REQUIRED_KEYS if k not in blob]
    flags = blob.get("missing_flags") or {}
    return {
        "required_missing": missing,
        "missing_flags": flags,
        "complete": not missing,
    }


def build_player_features(
    *,
    as_of: datetime,
    sample_size: int,
    source: str,
    usage: dict[str, float],
    opponent: dict[str, float],
    availability: dict[str, Any],
    weather: dict[str, Any] | None,
    market_anchor: dict[str, Any] | None,
    missing_flags: dict[str, bool] | None = None,
) -> dict[str, Any]:
    flags = {
        "injury_report_missing": bool((availability or {}).get("report_missing")),
        "weather_missing": weather is None,
        "market_missing": market_anchor is None,
        **(missing_flags or {}),
    }
    missingness = sum(1 for v in flags.values() if v) / max(len(flags), 1)
    blob = {
        "available_as_of": as_of.isoformat(),
        "sample_size": sample_size,
        "missingness": missingness,
        "source": source,
        "missing_flags": flags,
        "usage": usage,
        "opponent": opponent,
        "availability": availability,
        "weather": weather,
        "market_anchor": market_anchor,
        # Explicitly excluded unless a later ablation proves held-out value:
        "due_for_td": None,
    }
    return blob
