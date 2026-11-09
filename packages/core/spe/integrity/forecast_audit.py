"""Audit and quarantine published forecasts without rewriting probabilities or odds."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from spe.analytics.definitions import FEATURE_VERSION
from spe.db.base import as_utc
from spe.db.models import FeatureRow, Forecast, Game, ModelVersion, Outcome
from spe.identity.games import ALIAS_TIER, resolve_canonical_game_id
from spe.integrity.assignments import (
    IDENTITY_CONFLICT,
    audit_published_forecasts,
    quarantine_conflicts,
)

STALE_MODEL = "STALE_MODEL_TRAIN_CUTOFF"
RETIRED_SCORING_RATE = "RETIRED_SCORING_RATE_MODEL"
MISSING_FEATURES = "MISSING_CURRENT_GAME_FEATURES"
ALIAS_GAME = "ALIAS_GAME_ID"
INVALID_FOR_EVAL = "INVALID_FOR_RESEARCH_EVAL"

_INTEGRITY_KEEP = {
    IDENTITY_CONFLICT,
    MISSING_FEATURES,
    ALIAS_GAME,
    RETIRED_SCORING_RATE,
    "ROSTER_OTHER_TEAM",
    "ROSTER_WEEK_OTHER_TEAM",
    "NO_TEAM_MEMBERSHIP",
    "NAME_MISMATCH",
    "LEAGUE_ID_MISMATCH",
    "ODDS_EVENT_OTHER_GAME",
    "MISSING_PLAYER",
}


def audit_and_invalidate_forecasts(session: Session) -> dict[str, Any]:
    """Identity conflicts + missing upcoming features + alias game rows.

    Marks affected official forecasts ``is_official=False`` and appends reason
    codes. Does not change ``p``, odds ticks, or historical probability values.

    Stale-model quarantine is reported but not applied in Phase 1 — M3/M4 stay
    available for shadow comparison until a later promotion gate says otherwise.
    Retired scoring-rate models are always quarantined.
    """
    identity = audit_published_forecasts(session)
    identity_flagged = quarantine_conflicts(session, identity)

    stale = _flag_stale_models(session, apply=False)
    missing = _flag_missing_features(session)
    alias = _flag_alias_game_forecasts(session)

    return {
        "identity": {
            "audited": identity.get("audited"),
            "conflicts": identity.get("conflicts"),
            "by_reason": identity.get("by_reason"),
            "flagged": identity_flagged,
        },
        "stale_model": stale,
        "missing_features": missing,
        "alias_game": alias,
        "play_enabled": False,
    }


def _flag_stale_models(session: Session, *, apply: bool = False) -> dict[str, Any]:
    versions = {v.model_version_id: v for v in session.query(ModelVersion).all()}
    stale_ids: set[str] = set()
    retired_ids: set[str] = set()
    cutoff_limit = datetime(2025, 1, 1, tzinfo=UTC)
    for vid, row in versions.items():
        if vid.startswith("slate-td-rate") or (row.code_sha or "") == "scoring-rate":
            retired_ids.add(vid)
            continue
        if vid.startswith("m3-") or vid.startswith("baseline-"):
            stale_ids.add(vid)
            continue
        cutoff = as_utc(row.training_cutoff)
        if cutoff is not None and cutoff < cutoff_limit:
            stale_ids.add(vid)

    retired_flagged = _quarantine_versions(
        session,
        retired_ids,
        reason_codes=(RETIRED_SCORING_RATE, INVALID_FOR_EVAL),
        invalid_reason=RETIRED_SCORING_RATE,
    )
    restored = _restore_m3_shadow_forecasts(session)

    candidate_rows = 0
    if stale_ids:
        candidate_rows = (
            session.query(Forecast)
            .join(Outcome, Outcome.outcome_id == Forecast.outcome_id)
            .join(Game, Game.game_id == Outcome.game_id)
            .filter(
                Forecast.is_official.is_(True),
                Forecast.model_version_id.in_(stale_ids),
                Game.data_tier.in_(("upcoming", "experimental")),
                Game.status.in_(("scheduled", "preview", "pregame")),
            )
            .count()
        )

    if apply and stale_ids:
        flagged = _quarantine_versions(
            session,
            stale_ids,
            reason_codes=(STALE_MODEL, INVALID_FOR_EVAL),
            invalid_reason=STALE_MODEL,
        )
        return {
            "flagged": flagged,
            "retired_flagged": retired_flagged,
            "restored_m3": restored,
            "stale_versions": sorted(stale_ids),
            "retired_versions": sorted(retired_ids),
            "applied": True,
        }

    return {
        "flagged": 0,
        "candidates": candidate_rows,
        "retired_flagged": retired_flagged,
        "restored_m3": restored,
        "stale_versions": sorted(stale_ids),
        "retired_versions": sorted(retired_ids),
        "applied": False,
        "note": "Phase 1 reports stale M3/M4 candidates only; shadow keeps them official.",
    }


def _quarantine_versions(
    session: Session,
    version_ids: set[str],
    *,
    reason_codes: tuple[str, ...],
    invalid_reason: str,
) -> int:
    if not version_ids:
        return 0
    rows = (
        session.query(Forecast)
        .join(Outcome, Outcome.outcome_id == Forecast.outcome_id)
        .join(Game, Game.game_id == Outcome.game_id)
        .filter(
            Forecast.is_official.is_(True),
            Forecast.model_version_id.in_(version_ids),
            Game.data_tier.in_(("upcoming", "experimental", ALIAS_TIER)),
            Game.status.in_(("scheduled", "preview", "pregame")),
        )
        .all()
    )
    flagged = 0
    for forecast in rows:
        reasons = list(forecast.reason_codes or [])
        for code in reason_codes:
            if code not in reasons:
                reasons.append(code)
        forecast.reason_codes = reasons
        forecast.is_official = False
        components = dict(forecast.components or {})
        components["invalid_for_research_eval"] = True
        invalid = list(components.get("invalid_reasons") or [])
        if invalid_reason not in invalid:
            invalid.append(invalid_reason)
        components["invalid_reasons"] = invalid
        forecast.components = components
        flagged += 1
    session.flush()
    return flagged


def _restore_m3_shadow_forecasts(session: Session) -> int:
    """Undo over-quarantine that marked M3 unofficial solely for STALE_MODEL."""
    rows = (
        session.query(Forecast)
        .filter(Forecast.is_official.is_(False), Forecast.model_version_id.like("m3-%"))
        .all()
    )
    restored = 0
    for forecast in rows:
        original = list(forecast.reason_codes or [])
        if any(code in _INTEGRITY_KEEP for code in original):
            continue
        if STALE_MODEL not in original and INVALID_FOR_EVAL not in original:
            continue
        forecast.reason_codes = [r for r in original if r not in {STALE_MODEL, INVALID_FOR_EVAL}]
        forecast.is_official = True
        components = dict(forecast.components or {})
        components.pop("invalid_for_research_eval", None)
        invalid = [r for r in (components.get("invalid_reasons") or []) if r != STALE_MODEL]
        if invalid:
            components["invalid_reasons"] = invalid
        else:
            components.pop("invalid_reasons", None)
        forecast.components = components
        restored += 1
    session.flush()
    return restored


def _flag_missing_features(session: Session) -> dict[str, Any]:
    featured = {
        row.game_id
        for row in session.query(FeatureRow.game_id)
        .filter(FeatureRow.feature_version == FEATURE_VERSION)
        .distinct()
    }
    rows = (
        session.query(Forecast, Outcome, Game)
        .join(Outcome, Outcome.outcome_id == Forecast.outcome_id)
        .join(Game, Game.game_id == Outcome.game_id)
        .filter(
            Forecast.is_official.is_(True),
            Game.data_tier == "upcoming",
            Game.status.in_(("scheduled", "preview", "pregame")),
        )
        .all()
    )
    latest: dict[str, tuple[Forecast, Game]] = {}
    for forecast, outcome, game in rows:
        cur = latest.get(outcome.outcome_id)
        if cur is None or (as_utc(forecast.as_of) or forecast.as_of) >= (as_utc(cur[0].as_of) or cur[0].as_of):
            latest[outcome.outcome_id] = (forecast, game)

    flagged = 0
    missing_games: set[str] = set()
    for forecast, game in latest.values():
        if game.game_id in featured:
            continue
        missing_games.add(game.game_id)
        reasons = list(forecast.reason_codes or [])
        for code in (MISSING_FEATURES, INVALID_FOR_EVAL):
            if code not in reasons:
                reasons.append(code)
        forecast.reason_codes = reasons
        forecast.is_official = False
        components = dict(forecast.components or {})
        components["invalid_for_research_eval"] = True
        invalid = list(components.get("invalid_reasons") or [])
        if MISSING_FEATURES not in invalid:
            invalid.append(MISSING_FEATURES)
        components["invalid_reasons"] = invalid
        forecast.components = components
        flagged += 1
    session.flush()
    return {"flagged": flagged, "games_missing_features": sorted(missing_games)[:40]}


def _flag_alias_game_forecasts(session: Session) -> dict[str, Any]:
    rows = (
        session.query(Forecast, Outcome, Game)
        .join(Outcome, Outcome.outcome_id == Forecast.outcome_id)
        .join(Game, Game.game_id == Outcome.game_id)
        .filter(Forecast.is_official.is_(True), Game.data_tier == ALIAS_TIER)
        .all()
    )
    flagged = 0
    for forecast, outcome, game in rows:
        reasons = list(forecast.reason_codes or [])
        for code in (ALIAS_GAME, INVALID_FOR_EVAL):
            if code not in reasons:
                reasons.append(code)
        forecast.reason_codes = reasons
        forecast.is_official = False
        components = dict(forecast.components or {})
        components["canonical_game_id"] = resolve_canonical_game_id(session, game.game_id)
        components["invalid_for_research_eval"] = True
        forecast.components = components
        flagged += 1
    session.flush()
    return {"flagged": flagged}
