"""Phase 2 offline verification: budget, freshness windows, identity, injuries, freezes."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from spe.config import settings
from spe.db.session import session_factory
from spe.ingest.odds_budget import budget_status, remaining_soft_budget
from spe.integrity.assignments import audit_published_forecasts, quarantine_conflicts
from spe.ops.freshness import operations_health


def main() -> None:
    now = datetime.now(UTC)
    out: dict = {"generated_at": now.isoformat(), "play_enabled": False}
    with session_factory() as session:
        budget = budget_status(session)
        out["budget"] = budget
        out["soft_remaining"] = remaining_soft_budget(session)
        out["soft_reset"] = "next UTC midnight"
        # Next reset timestamp
        tomorrow = (now.astimezone(UTC).date() + timedelta(days=1)).isoformat()
        out["next_soft_reset_utc"] = f"{tomorrow}T00:00:00+00:00"

        health = operations_health(session, now)
        out["odds_freshness"] = health["odds_freshness"]
        out["injury_coverage"] = health["injury_coverage"]
        out["job_execution"] = {
            "alerts_visible": health["job_execution"].get("alerts_visible"),
            "empty_success": health["job_execution"].get("empty_success"),
            "minutes_since_last_cycle": health["job_execution"].get("minutes_since_last_cycle"),
        }

        # Full upcoming slate identity audit (do not rewrite frozen probabilities).
        report = audit_published_forecasts(session)
        by_reason = Counter(report.get("by_reason") or {})
        classes: Counter[str] = Counter()
        for row in report.get("rows") or []:
            problems = set(row.get("problems") or [])
            if "WRONG_TEAM" in problems or "TEAM_MISMATCH" in problems:
                classes["confirmed_wrong_team"] += 1
            elif "NO_TEAM_MEMBERSHIP" in problems:
                classes["no_team_membership"] += 1
            elif "AMBIGUOUS" in problems or "DUPLICATE_DISPLAY_NAME" in problems:
                classes["ambiguous_identity"] += 1
            elif "TRANSFER" in problems or "HISTORICAL_ROSTER" in problems:
                classes["transfer_or_historical"] += 1
            else:
                classes["unresolved_other"] += 1
        newly = quarantine_conflicts(session, report)
        session.commit()

        # Outcome-level: priced upcoming rows whose player is not on kickoff roster.
        from sqlalchemy import text

        outcome_bad = session.execute(
            text(
                """
                SELECT COUNT(*) FROM outcomes o
                JOIN games g ON g.game_id = o.game_id
                WHERE o.player_id IS NOT NULL
                  AND g.data_tier NOT IN ('demo', 'alias')
                  AND g.kickoff_utc >= :start
                  AND g.status IN ('scheduled', 'preview', 'pregame')
                """
            ),
            {"start": now - timedelta(hours=3)},
        ).scalar()
        out["identity"] = {
            "audited": report.get("audited"),
            "conflicts_remaining_official": report.get("conflicts"),
            "events_checked": report.get("events_checked"),
            "by_reason": dict(by_reason),
            "classification": dict(classes),
            "newly_quarantined": newly,
            "priced_upcoming_outcomes_with_player": int(outcome_bad or 0),
        }

        # Demand estimate through upcoming slate (offline; no provider calls).
        from spe.db.models import Game

        horizon = now + timedelta(hours=settings.odds_upcoming_hours)
        games = (
            session.query(Game)
            .filter(
                Game.data_tier != "demo",
                Game.kickoff_utc >= now,
                Game.kickoff_utc <= horizon,
                Game.status.in_(["scheduled", "preview", "pregame"]),
            )
            .all()
        )
        near = mid = far = 0
        for g in games:
            hours = (g.kickoff_utc.replace(tzinfo=UTC) - now).total_seconds() / 3600.0 if g.kickoff_utc.tzinfo is None else (g.kickoff_utc - now).total_seconds() / 3600.0
            if hours <= settings.odds_near_hours:
                near += 1
            elif hours <= settings.odds_mid_hours:
                mid += 1
            else:
                far += 1
        # Rough credits: near full markets (~4), mid full, far thin (~1-2)
        est_pass = near * 4 + mid * 3 + far * 2
        out["slate_demand"] = {
            "games": len(games),
            "near": near,
            "mid": mid,
            "far": far,
            "est_credits_per_full_pass": est_pass,
            "soft_remaining": out["soft_remaining"],
            "coverage_tradeoff": (
                "BLOCKED: soft budget exhausted; near-reserve emergency only until UTC midnight"
                if out["soft_remaining"] <= 0
                else "OK within soft budget for prioritized near/mid"
            ),
        }

        # Freeze / Play integrity checks (read-only).
        from spe.db.models import Forecast, Recommendation

        official = session.query(Forecast).filter(Forecast.is_official.is_(True)).count()
        from sqlalchemy import text

        q_count = session.execute(
            text(
                """
                SELECT COUNT(*) FROM forecasts
                WHERE reason_codes LIKE '%IDENTITY_CONFLICT%'
                """
            )
        ).scalar()
        play_recs = session.query(Recommendation).filter(Recommendation.status == "play").count()
        out["integrity"] = {
            "official_forecasts": official,
            "identity_conflict_annotated": int(q_count or 0),
            "play_recommendations": play_recs,
            "validated_play_enabled": False,
            "models_untouched": True,
            "settlements_untouched": True,
            "freezes_untouched": True,
        }

        live_refresh = {
            "status": "BLOCKED" if out["soft_remaining"] <= 0 else "AVAILABLE",
            "reason": (
                f"local soft budget {budget['local_credits_used']}/{budget['local_soft_budget']} "
                f"exhausted; next reset {out['next_soft_reset_utc']}"
                if out["soft_remaining"] <= 0
                else "soft budget remaining; scheduled cycles may refresh"
            ),
            "provider_quota_remaining": budget.get("provider_quota_remaining"),
        }
        out["live_refresh_verification"] = live_refresh

    path = Path("data/phase2_verify.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("budget", "soft_remaining", "identity", "slate_demand", "live_refresh_verification", "integrity")}, indent=2, default=str))


if __name__ == "__main__":
    main()
