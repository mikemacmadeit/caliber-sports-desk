"""Diagnose why ATS has no live-book spread ticks (Agent 4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from spe.config import settings
from spe.db.base import as_utc
from spe.db.session import session_factory
from spe.ingest.odds_budget import budget_status, remaining_soft_budget, spendable_for_bucket
from spe.ingest.odds_live import _market_bucket


def main() -> None:
    now = datetime.now(UTC)
    out: dict = {"as_of": now.isoformat(), "play_enabled": False}
    with session_factory() as s:
        out["budget"] = budget_status(s)
        out["remaining_soft"] = remaining_soft_budget(s)
        near_reserve = int(settings.odds_near_reserve_credits)
        out["near_reserve_credits"] = near_reserve
        out["spendable"] = {
            "near": spendable_for_bucket(s, "near", allow_near_emergency=False),
            "mid": spendable_for_bucket(s, "mid", allow_near_emergency=False),
            "far": spendable_for_bucket(s, "far", allow_near_emergency=False),
        }
        out["policy_note"] = (
            "Mid/far spendable = max(0, remaining - near_reserve). "
            "Near spendable = remaining (reserve is for near, not a per-request fee). "
            "A single spreads event typically costs ~1–2 credits, not 400."
        )

        horizon = now + timedelta(hours=settings.odds_upcoming_hours)
        games = s.execute(
            text(
                """
                SELECT game_id, league_id, kickoff_utc, status, home_team_id, away_team_id
                FROM games
                WHERE kickoff_utc > :now AND kickoff_utc <= :horizon
                  AND status IN ('scheduled','preview','pregame')
                  AND league_id IN ('nfl','fbs')
                ORDER BY kickoff_utc
                """
            ),
            {"now": now.isoformat(), "horizon": horizon.isoformat()},
        ).mappings().all()

        buckets = {"near": [], "mid": [], "far": []}
        for g in games:
            class _G:
                pass

            fake = _G()
            fake.kickoff_utc = g["kickoff_utc"]
            # _market_bucket expects Game-like with kickoff_utc
            from spe.db.models import Game

            game = s.get(Game, g["game_id"])
            if game is None:
                continue
            b = _market_bucket(game, now)
            ko = as_utc(game.kickoff_utc)
            hours = (ko - now).total_seconds() / 3600.0 if ko else None
            buckets[b].append(
                {
                    "game_id": game.game_id,
                    "league_id": game.league_id,
                    "kickoff_utc": ko.isoformat() if ko else None,
                    "hours_to_ko": hours,
                    "spendable_for_bucket": spendable_for_bucket(s, b, allow_near_emergency=False),
                }
            )
        out["upcoming_in_horizon"] = {k: len(v) for k, v in buckets.items()}
        out["upcoming_samples"] = {k: v[:8] for k, v in buckets.items()}

        ticks = s.execute(
            text(
                """
                SELECT o.game_id, o.side, o.line, t.bookmaker_id, t.captured_at, t.source_at,
                       g.kickoff_utc, g.league_id
                FROM outcomes o
                JOIN odds_ticks t ON t.outcome_id = o.outcome_id
                JOIN games g ON g.game_id = o.game_id
                WHERE o.market_id = 'point_spread'
                ORDER BY t.captured_at DESC
                LIMIT 20
                """
            )
        ).mappings().all()
        out["recent_spread_ticks_any"] = [dict(r) for r in ticks]
        upcoming_ticks = s.execute(
            text(
                """
                SELECT COUNT(*) AS n
                FROM outcomes o
                JOIN odds_ticks t ON t.outcome_id = o.outcome_id
                JOIN games g ON g.game_id = o.game_id
                WHERE o.market_id = 'point_spread' AND g.kickoff_utc > :now
                """
            ),
            {"now": now.isoformat()},
        ).scalar()
        out["upcoming_spread_tick_count"] = int(upcoming_ticks or 0)

        # Coverage / last ingest notes
        cov = s.execute(
            text(
                """
                SELECT dataset, league_id, status, row_count, last_ingested_at, notes
                FROM data_coverage
                WHERE provider='odds_api' AND dataset IN ('live_spreads','live_odds')
                ORDER BY last_ingested_at DESC
                LIMIT 10
                """
            )
        ).mappings().all()
        out["coverage"] = [dict(r) for r in cov]

        # Raw snapshots mentioning spreads
        snaps = s.execute(
            text(
                """
                SELECT endpoint, provider_record_id, observed_at, credit_cost
                FROM raw_snapshots
                WHERE provider='odds_api' AND observed_at > :since
                ORDER BY observed_at DESC
                LIMIT 30
                """
            ),
            {"since": (now - timedelta(hours=24)).isoformat()},
        ).mappings().all()
        out["raw_snapshots_24h_count_sample"] = len(snaps)
        out["raw_snapshots_24h_sample"] = [dict(r) for r in snaps[:10]]

        # Diagnose primary blocker
        rem = out["remaining_soft"]
        near_n = out["upcoming_in_horizon"]["near"]
        mid_n = out["upcoming_in_horizon"]["mid"]
        if out["upcoming_spread_tick_count"] > 0:
            blocker = "none_ticks_present_check_ats_query"
        elif near_n == 0 and mid_n == 0:
            blocker = "authorized_kickoff_window_empty_no_near_mid_games"
        elif out["spendable"]["near"] <= 0 and near_n > 0:
            blocker = "remaining_credits_exhausted_for_near"
        elif out["spendable"]["mid"] <= 0 and near_n == 0 and mid_n > 0:
            blocker = "mid_blocked_by_near_reserve_policy_pending_near_window"
        elif rem > 0 and near_n > 0 and out["spendable"]["near"] > 0:
            blocker = "credits_available_for_near_but_no_ticks_ingestion_or_schedule_gap"
        else:
            blocker = "investigate_further"
        out["primary_blocker"] = blocker
        out["config"] = {
            "odds_near_hours": settings.odds_near_hours,
            "odds_mid_hours": settings.odds_mid_hours,
            "odds_upcoming_hours": settings.odds_upcoming_hours,
            "odds_near_reserve_credits": settings.odds_near_reserve_credits,
            "odds_credit_budget_per_day": settings.odds_credit_budget_per_day,
        }

    path = "data/research/ats_audit/live_spread_blocker.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=str)
    print(json.dumps({"wrote": path, "primary_blocker": out["primary_blocker"], "remaining": out["remaining_soft"], "spendable": out["spendable"], "upcoming": out["upcoming_in_horizon"]}, indent=2))


if __name__ == "__main__":
    main()
