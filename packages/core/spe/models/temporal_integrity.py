"""Completed-game temporal gates for training features.

Kickoff alone does not prove a game has finished. Training and prior features
require status=final plus a completion buffer before the prediction timestamp.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.analytics.engine import GAME_COMPLETE_AFTER
from spe.config import settings
from spe.db.base import as_utc


def completion_buffer() -> timedelta:
    hours = float(getattr(settings, "game_complete_hours", None) or 4.5)
    return timedelta(hours=hours) if hours > 0 else GAME_COMPLETE_AFTER


def game_completed_at(kickoff: datetime | pd.Timestamp) -> datetime:
    kick = as_utc(kickoff) if not isinstance(kickoff, pd.Timestamp) else kickoff.to_pydatetime()
    if kick.tzinfo is None:
        kick = kick.replace(tzinfo=UTC)
    return kick + completion_buffer()


def is_available_before(kickoff: datetime | pd.Timestamp, as_of: datetime) -> bool:
    """True when box-score / play features from this game could be known by as_of."""
    moment = as_utc(as_of) or as_of
    return game_completed_at(kickoff) <= moment


def training_cutoff(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Latest timestamp through which completed historical games may enter training."""
    moment = as_utc(now) or datetime.now(UTC)
    buffer = completion_buffer()
    by_league: dict[str, Any] = {}
    for league in ("nfl", "fbs"):
        finals = session.execute(
            text(
                """
                SELECT game_id, kickoff_utc, season_id, week
                FROM games
                WHERE league_id = :lg AND data_tier = 'historical' AND status = 'final'
                ORDER BY kickoff_utc
                """
            ),
            {"lg": league},
        ).mappings().all()
        usable = [r for r in finals if is_available_before(r["kickoff_utc"], moment)]
        max_kick = usable[-1]["kickoff_utc"] if usable else None
        cutoff = game_completed_at(max_kick) if max_kick is not None else None
        by_league[league] = {
            "n_final_total": len(finals),
            "n_usable_before_now": len(usable),
            "latest_usable_kickoff": as_utc(max_kick).isoformat() if max_kick is not None else None,
            "train_cutoff": cutoff.isoformat() if cutoff is not None else None,
        }
    return {
        "as_of": moment.isoformat(),
        "completion_buffer_hours": buffer.total_seconds() / 3600.0,
        "leagues": by_league,
    }


def audit_temporal_integrity(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Report leakage risks: non-final historical rows and priors that use unfinished games."""
    moment = as_utc(now) or datetime.now(UTC)
    non_final = session.execute(
        text(
            """
            SELECT league_id, status, COUNT(*) AS n
            FROM games
            WHERE data_tier = 'historical' AND status != 'final'
            GROUP BY league_id, status
            """
        )
    ).mappings().all()
    plays_on_non_final = session.execute(
        text(
            """
            SELECT g.league_id, COUNT(*) AS n
            FROM plays p
            JOIN games g ON g.game_id = p.game_id
            WHERE g.data_tier = 'historical' AND g.status != 'final'
            GROUP BY g.league_id
            """
        )
    ).mappings().all()
    stats_on_non_final = session.execute(
        text(
            """
            SELECT g.league_id, COUNT(*) AS n
            FROM player_game_stats p
            JOIN games g ON g.game_id = p.game_id
            WHERE g.data_tier = 'historical' AND g.status != 'final'
            GROUP BY g.league_id
            """
        )
    ).mappings().all()
    # Upcoming games whose kickoff has passed but status is not final — cannot be training labels.
    stale_upcoming = session.execute(
        text(
            """
            SELECT league_id, COUNT(*) AS n
            FROM games
            WHERE data_tier = 'upcoming'
              AND status NOT IN ('final', 'canceled', 'cancelled')
              AND kickoff_utc < :moment
            GROUP BY league_id
            """
        ),
        {"moment": moment.isoformat()},
    ).mappings().all()
    cutoff = training_cutoff(session, now=moment)
    issues: list[str] = []
    if non_final:
        issues.append("historical_non_final_games_present")
    if any(int(r["n"] or 0) > 0 for r in plays_on_non_final):
        issues.append("plays_linked_to_non_final_historical")
    if any(int(r["n"] or 0) > 0 for r in stats_on_non_final):
        issues.append("stats_linked_to_non_final_historical")
    return {
        "status": "ok" if not issues else "issues_found",
        "as_of": moment.isoformat(),
        "issues": issues,
        "historical_non_final": [dict(r) for r in non_final],
        "plays_on_non_final": [dict(r) for r in plays_on_non_final],
        "stats_on_non_final": [dict(r) for r in stats_on_non_final],
        "stale_upcoming_not_final": [dict(r) for r in stale_upcoming],
        "training_cutoff": cutoff,
        "policy": {
            "require_status": "final",
            "require_data_tier": "historical",
            "require_completed_at_before_as_of": True,
            "kickoff_alone_insufficient": True,
        },
    }


def filter_completed_frame(frame: pd.DataFrame, as_of: datetime, *, kickoff_col: str = "kickoff_utc") -> pd.DataFrame:
    """Drop rows whose game could not have been finished by as_of."""
    if frame.empty or kickoff_col not in frame.columns:
        return frame
    moment = as_utc(as_of) or as_of
    kick = pd.to_datetime(frame[kickoff_col], utc=True)
    completed = kick + pd.Timedelta(completion_buffer())
    keep = completed <= pd.Timestamp(moment)
    if "status" in frame.columns:
        keep = keep & frame["status"].fillna("").astype(str).str.lower().eq("final")
    return frame.loc[keep].copy()
