"""Execute approved Option B near-kickoff refresh within remaining soft credits.

Temporary TOTAL ceiling 2312 until 2026-11-08T00:00:00Z.
Does not raise past 2312, purchase plans, or touch models/Play.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from spe.config import settings
from spe.db.base import as_utc
from spe.db.models import Game, Outcome, OddsTick, Team
from spe.db.session import session_factory
from spe.ingest.odds_budget import (
    budget_status,
    effective_soft_budget,
    remaining_soft_budget,
    spendable_for_bucket,
    used_credits,
)
from spe.ingest.odds_live import ingest_odds_live
from spe.ops.freshness import odds_freshness_health


def _age_minutes(session, game_id: str, now: datetime) -> float | None:
    from sqlalchemy import text

    row = session.execute(
        text(
            """
            SELECT MAX(COALESCE(t.observed_at, t.captured_at))
            FROM odds_ticks t
            JOIN outcomes o ON o.outcome_id = t.outcome_id
            WHERE o.game_id = :gid
            """
        ),
        {"gid": game_id},
    ).scalar()
    if row is None:
        return None
    stamp = as_utc(row)
    if stamp is None:
        return None
    return (now - stamp).total_seconds() / 60.0


def main() -> None:
    now = datetime.now(UTC)
    report: dict = {
        "started_at": now.isoformat(),
        "approval": {
            "temporary_total_ceiling": 2312,
            "expires_at": "2026-11-08T00:00:00+00:00",
            "standard_ceiling_after_reset": 2000,
            "no_plan_purchase": True,
            "play_enabled": False,
        },
    }
    with session_factory() as session:
        before_used = used_credits(session)
        before_status = budget_status(session)
        left = remaining_soft_budget(session)
        report["before"] = {
            **before_status,
            "effective_soft_budget": effective_soft_budget(now),
            "near_spendable": spendable_for_bucket(session, "near"),
            "mid_spendable": spendable_for_bucket(session, "mid"),
            "far_spendable": spendable_for_bucket(session, "far"),
        }
        assert before_status["temporary_ceiling_active"] is True
        assert effective_soft_budget(now) == 2312
        assert spendable_for_bucket(session, "mid") == 0 or left > 400
        # With ~261 left and near_reserve 400, mid/far must be 0.
        assert spendable_for_bucket(session, "mid") == 0
        assert spendable_for_bucket(session, "far") == 0
        assert left == max(0, 2312 - before_used)

        # Snapshot near slate before refresh
        soft_reset = datetime(2026, 11, 8, 0, 0, tzinfo=UTC)
        near_before = []
        games = (
            session.query(Game)
            .filter(
                Game.data_tier.notin_(["demo", "alias"]),
                Game.kickoff_utc >= now,
                Game.kickoff_utc <= soft_reset,
                Game.status.in_(["scheduled", "preview", "pregame"]),
            )
            .order_by(Game.kickoff_utc.asc())
            .all()
        )
        for g in games:
            ko = as_utc(g.kickoff_utc)
            h = (ko - now).total_seconds() / 3600.0 if ko else 999
            if h > settings.odds_near_hours:
                continue
            age = _age_minutes(session, g.game_id, now)
            near_before.append(
                {
                    "game_id": g.game_id,
                    "league": g.league_id,
                    "hours_to_kick": round(h, 2),
                    "age_minutes": None if age is None else round(age, 1),
                    "stale_or_missing": age is None or age > settings.max_price_age_minutes,
                }
            )
        report["near_before"] = {
            "games": len(near_before),
            "stale_or_missing": sum(1 for x in near_before if x["stale_or_missing"]),
            "rows": near_before,
        }

        # Prioritized near refresh: raise per-run event cap so we can use remaining
        # credits in one pass (4 cr × events). Still hard-capped by shared reserve.
        max_events = max(1, left // 4)
        prev_max = settings.odds_max_events_per_run
        settings.odds_max_events_per_run = min(80, max(prev_max, max_events))
        try:
            # Only NFL+FBS; ingest already prioritizes near/stale first and mid/far
            # cannot spend while remaining < near_reserve.
            result = ingest_odds_live(session, now=now, leagues=("nfl", "fbs"))
            session.commit()
        finally:
            settings.odds_max_events_per_run = prev_max

        after_used = used_credits(session)
        after_status = budget_status(session)
        additional = after_used - before_used
        report["ingest_result"] = {
            "status": result.get("status"),
            "events_fetched": result.get("events_fetched"),
            "events_skipped": result.get("events_skipped"),
            "skipped_for_near_reserve": result.get("skipped_for_near_reserve"),
            "skipped_dedup": result.get("skipped_dedup"),
            "events_unavailable_cooldown": result.get("events_unavailable_cooldown"),
            "credits_spent": result.get("credits_spent"),
            "credits_reserved": result.get("credits_reserved"),
            "ticks_upserted": result.get("ticks_upserted"),
            "ticks_reobserved": result.get("ticks_reobserved"),
            "ticks_price_changed": result.get("ticks_price_changed"),
            "api_remaining": result.get("api_remaining"),
            "markets_by_bucket": result.get("markets_by_bucket"),
            "leagues": result.get("leagues"),
        }
        report["after"] = after_status
        report["additional_credits_consumed"] = additional
        report["remaining_allowance"] = remaining_soft_budget(session)
        assert after_used <= 2312 or additional == 0
        assert additional <= left + 4  # tiny reconcile slack only if provider actual > est

        near_after = []
        for g in games:
            ko = as_utc(g.kickoff_utc)
            h = (ko - now).total_seconds() / 3600.0 if ko else 999
            if h > settings.odds_near_hours:
                continue
            age = _age_minutes(session, g.game_id, now)
            near_after.append(
                {
                    "game_id": g.game_id,
                    "league": g.league_id,
                    "hours_to_kick": round(h, 2),
                    "age_minutes": None if age is None else round(age, 1),
                    "stale_or_missing": age is None or age > settings.max_price_age_minutes,
                    "fresh": age is not None and age <= settings.max_price_age_minutes,
                }
            )
        report["near_after"] = {
            "games": len(near_after),
            "fresh": sum(1 for x in near_after if x.get("fresh")),
            "stale_or_missing": sum(1 for x in near_after if x["stale_or_missing"]),
            "rows": near_after,
        }
        report["freshness_windows"] = odds_freshness_health(session, now).get("windows")
        report["integrity"] = {
            "models_untouched": True,
            "freezes_untouched": True,
            "validated_play": False,
            "standard_ceiling_after_reset": 2000,
            "scheduled_recovery_verification": "PENDING",
        }
        report["finished_at"] = datetime.now(UTC).isoformat()

    path = "data/option_b_execution_report.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(
        json.dumps(
            {
                "before_used": before_used,
                "after_used": report["after"]["local_credits_used"],
                "additional": report["additional_credits_consumed"],
                "remaining": report["remaining_allowance"],
                "ingest": report["ingest_result"],
                "near_before_stale": report["near_before"]["stale_or_missing"],
                "near_after_fresh": report["near_after"]["fresh"],
                "near_after_stale": report["near_after"]["stale_or_missing"],
                "ceiling": report["after"]["local_soft_budget"],
                "temporary_active": report["after"]["temporary_ceiling_active"],
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
