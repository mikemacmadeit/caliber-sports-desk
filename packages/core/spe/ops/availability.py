"""Pregame availability classification. Missing reports are never healthy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from spe.config import settings
from spe.db.base import as_utc
from spe.db.models import InjuryEvent

# NFL official injury reports typically begin mid-week. Before that window a blank
# slate is "not yet scheduled", not a coverage failure.
NFL_REPORT_LEAD_HOURS = 96
INACTIVE_LIST_SOURCE = "espn.gameday_inactives"


def designation_rules_player_out(designation: str | None) -> bool:
    """True only for an explicit out tag. A missed practice is not an out."""
    text = (designation or "").lower()
    if "injured reserve" in text:
        return True
    words = set(text.replace("/", " ").replace("-", " ").split())
    return bool(words & {"out", "ir", "pup", "suspended", "inactive"})


def classify_injury_row(row: InjuryEvent | None) -> tuple[str, float, float | None]:
    if row is None:
        return "unknown", 0.0, None
    text = f"{row.practice_status or ''} {row.designation or ''}".lower()
    words = set(text.replace("/", " ").split())
    if "did not" in text or "injured reserve" in text or words & {"out", "ir", "pup", "suspended", "inactive"}:
        return "confirmed_inactive", 0.0, row.confidence
    if "doubtful" in text:
        return "doubtful", 0.35, row.confidence
    if "limited" in text or "questionable" in text:
        return "uncertain", 0.7, row.confidence
    if "full" in text or "probable" in text:
        return "expected_active", 0.92, row.confidence
    return "unknown", 0.0, row.confidence


def inside_inactive_window(league_id: str, kickoff: datetime | None, now: datetime) -> bool:
    """True when an NFL game is close enough that the inactive list is the report."""
    if league_id != "nfl" or kickoff is None:
        return False
    kick = as_utc(kickoff)
    if kick is None:
        return False
    hours = (kick - now).total_seconds() / 3600.0
    return 0 <= hours <= float(settings.inactive_window_hours)


def games_with_inactive_list(session: Any, game_ids: list[str]) -> set[str]:
    """Games whose gameday inactive list was actually received."""
    if not game_ids:
        return set()
    rows = (
        session.query(InjuryEvent.game_id)
        .filter(
            InjuryEvent.game_id.in_(game_ids),
            InjuryEvent.coverage_complete.is_(True),
            InjuryEvent.source.startswith(INACTIVE_LIST_SOURCE),
        )
        .distinct()
        .all()
    )
    return {game_id for (game_id,) in rows}


def availability_for_player(
    *,
    league_id: str,
    kickoff: datetime | None,
    player_injury: InjuryEvent | None,
    game_has_complete_report: bool,
    now: datetime | None = None,
    inactive_list_received: bool = False,
) -> dict[str, Any]:
    """Return status, probability, confidence, and precise abstain/coverage codes."""
    now = now or datetime.now(UTC)
    kickoff = as_utc(kickoff) if kickoff is not None else None
    if inactive_list_received and player_injury is not None:
        source = str(getattr(player_injury, "source", "") or "")
        if not source.startswith(INACTIVE_LIST_SOURCE):
            player_injury = None

    if player_injury is not None:
        status, prob, confidence = classify_injury_row(player_injury)
        if status == "confirmed_inactive":
            return {
                "availability_status": "doubtful",
                "availability_prob": prob,
                "injury_confidence": confidence,
                "coverage": "confirmed_player_unavailable",
                "abstain_reasons": ["CONFIRMED_INACTIVE"],
                "recommend_status": "doubtful",
            }
        if status == "expected_active":
            return {
                "availability_status": "probable",
                "availability_prob": prob,
                "injury_confidence": confidence,
                "coverage": "report_received_complete",
                "abstain_reasons": [],
                "recommend_status": "probable",
            }
        if status == "uncertain":
            return {
                "availability_status": "questionable",
                "availability_prob": prob,
                "injury_confidence": confidence,
                "coverage": "report_received_complete",
                "abstain_reasons": ["UNCERTAIN_PLAYER_AVAILABILITY"],
                "recommend_status": "questionable",
            }
        if status == "doubtful":
            return {
                "availability_status": "doubtful",
                "availability_prob": prob,
                "injury_confidence": confidence,
                "coverage": "report_received_complete",
                "abstain_reasons": ["AVAILABILITY_TOO_LOW"],
                "recommend_status": "doubtful",
            }
        return {
            "availability_status": "unknown",
            "availability_prob": 0.0,
            "injury_confidence": confidence,
            "coverage": "report_received_incomplete",
            "abstain_reasons": ["INJURY_COVERAGE_INCOMPLETE", "NEVER_ASSUME_HEALTHY"],
            "recommend_status": "unknown",
        }

    if inside_inactive_window(league_id, kickoff, now):
        if inactive_list_received:
            return {
                "availability_status": "probable",
                "availability_prob": 0.92,
                "injury_confidence": 0.8,
                "coverage": "report_received_complete",
                "abstain_reasons": [],
                "recommend_status": "probable",
            }
        return {
            "availability_status": "unknown",
            "availability_prob": 0.0,
            "injury_confidence": None,
            "coverage": "report_expected_missing",
            "abstain_reasons": ["INJURY_REPORT_EXPECTED_MISSING", "NEVER_ASSUME_HEALTHY"],
            "recommend_status": "unknown",
        }

    if game_has_complete_report:
        # A finished official report that does not name this player is not a missing report.
        return {
            "availability_status": "probable",
            "availability_prob": 0.92,
            "injury_confidence": 0.7,
            "coverage": "report_received_complete",
            "abstain_reasons": [],
            "recommend_status": "probable",
        }

    if league_id == "fbs":
        # FBS has no complete report for this game. Do not invent healthy players.
        return {
            "availability_status": "unknown",
            "availability_prob": 0.0,
            "injury_confidence": None,
            "coverage": "source_unavailable",
            "abstain_reasons": ["INJURY_SOURCE_UNAVAILABLE", "NEVER_ASSUME_HEALTHY"],
            "recommend_status": "unknown",
        }

    if kickoff is not None and kickoff - now > timedelta(hours=NFL_REPORT_LEAD_HOURS):
        return {
            "availability_status": "unknown",
            "availability_prob": 0.0,
            "injury_confidence": None,
            "coverage": "report_not_yet_scheduled",
            "abstain_reasons": ["INJURY_REPORT_NOT_YET_SCHEDULED", "NEVER_ASSUME_HEALTHY"],
            "recommend_status": "unknown",
        }

    return {
        "availability_status": "unknown",
        "availability_prob": 0.0,
        "injury_confidence": None,
        "coverage": "report_expected_missing",
        "abstain_reasons": ["INJURY_REPORT_EXPECTED_MISSING", "NEVER_ASSUME_HEALTHY"],
        "recommend_status": "unknown",
    }


def recommend_availability_fields(info: dict[str, Any]) -> tuple[str, float, float | None]:
    """Map rich coverage into the values RecommendInput expects today."""
    return (
        str(info["availability_status"]),
        float(info["availability_prob"]),
        info.get("injury_confidence"),
    )
