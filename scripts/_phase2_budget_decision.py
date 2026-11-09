"""Costed soft-budget decision inputs (offline + optional free sports list)."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from spe.adapters.odds_api import OddsApiAdapter
from spe.config import settings
from spe.db.base import as_utc
from spe.db.models import Game
from spe.db.session import session_factory
from spe.ingest.odds_budget import budget_status, soft_overspend, used_credits
from spe.ops.freshness import odds_freshness_health


def main() -> None:
    now = datetime.now(UTC)
    soft_reset = datetime(2026, 11, 8, 0, 0, tzinfo=UTC)
    out: dict = {
        "generated_at": now.isoformat(),
        "local_soft_reset_utc": soft_reset.isoformat(),
        "local_soft_reset_chicago": "2026-11-07 18:00 America/Chicago",
        "play_enabled": False,
    }

    with session_factory() as s:
        out["budget"] = budget_status(s)
        out["soft_overspend"] = soft_overspend(s)
        out["used"] = used_credits(s)

        # Recent provider remaining from job summaries (no new spend).
        jobs = s.execute(
            text(
                """
                SELECT started_at, summary FROM job_runs
                WHERE job_name = 'odds_refresh'
                ORDER BY started_at DESC
                LIMIT 30
                """
            )
        ).mappings().all()
        api_rem_history = []
        for row in jobs:
            sm = row["summary"]
            if isinstance(sm, str):
                try:
                    sm = json.loads(sm)
                except Exception:
                    sm = {}
            sm = sm or {}
            api_rem_history.append(
                {
                    "started_at": str(row["started_at"]),
                    "api_remaining": sm.get("api_remaining"),
                    "credits_spent": sm.get("credits_spent"),
                    "status": sm.get("status"),
                    "events_fetched": sm.get("events_fetched"),
                    "skipped_for_near_reserve": sm.get("skipped_for_near_reserve"),
                    "skipped_dedup": sm.get("skipped_dedup"),
                }
            )
        out["recent_odds_jobs"] = api_rem_history
        last_known = next((j["api_remaining"] for j in api_rem_history if j["api_remaining"] is not None), None)
        out["last_known_provider_remaining_from_jobs"] = last_known

        # 00–06 UTC Friday evening Central window
        snaps = s.execute(
            text(
                """
                SELECT observed_at, payload, credit_cost
                FROM raw_snapshots
                WHERE provider = 'odds_api'
                  AND date(observed_at) = '2026-11-07'
                  AND CAST(strftime('%H', observed_at) AS INT) BETWEEN 0 AND 5
                """
            )
        ).mappings().all()
        by_horizon: Counter[str] = Counter()
        by_status: Counter[str] = Counter()
        cost = 0
        for row in snaps:
            cost += int(row["credit_cost"] or 0)
            p = row["payload"] if isinstance(row["payload"], dict) else {}
            if isinstance(row["payload"], str):
                try:
                    p = json.loads(row["payload"])
                except Exception:
                    p = {}
            kick = p.get("kickoff_utc")
            obs = as_utc(row["observed_at"])
            if kick and obs is not None:
                try:
                    kt = datetime.fromisoformat(str(kick).replace("Z", "+00:00"))
                    h = (kt - obs).total_seconds() / 3600.0
                    if h < 0:
                        bucket = "already_started"
                    elif h <= 6:
                        bucket = "near_0_6h"
                    elif h <= 12:
                        bucket = "nearish_6_12h"
                    elif h <= 24:
                        bucket = "same_day_12_24h"
                    elif h <= 48:
                        bucket = "mid_24_48h"
                    else:
                        bucket = "far_48h_plus"
                    by_horizon[bucket] += 1
                except Exception:
                    by_horizon["unknown"] += 1
            else:
                by_horizon["no_kickoff"] += 1
            if p.get("markets_unavailable"):
                by_status["markets_unavailable"] += 1
            elif int(p.get("n_records") or 0) > 0:
                by_status["had_records"] += 1
            else:
                by_status["empty_or_other"] += 1
        out["window_00_06_utc"] = {
            "note": "2026-11-07 00:00–05:59 UTC = Fri Nov 6 6:00 PM–11:59 PM America/Chicago",
            "requests": len(snaps),
            "raw_credited_cost": cost,
            "by_hours_to_kickoff_at_fetch": dict(by_horizon),
            "by_response": dict(by_status),
        }

        # Slate until soft reset and through Sunday local reset (Mon 00:00 UTC)
        sunday_soft_reset = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)  # Mon UTC = Sun 6pm Central end of day budget
        # Actually local soft resets every UTC midnight:
        # Sat evening after Fri 6pm Central reset (Sat 00:00 UTC) ... wait
        # Soft day is UTC date. Reset at 2026-11-08 00:00 UTC = Sat Nov 7 6pm Central.
        # Next reset after that: 2026-11-09 00:00 UTC = Sun Nov 8 6pm Central.
        # User asked Saturday evening through Sunday's local budget reset.
        # So from now until 2026-11-09T00:00:00Z covers Sat evening + Sunday day until Sun 6pm Central.
        horizon_end = datetime(2026, 11, 9, 0, 0, tzinfo=UTC)

        games = (
            s.query(Game)
            .filter(
                Game.data_tier.notin_(["demo", "alias"]),
                Game.kickoff_utc >= now,
                Game.kickoff_utc <= now + timedelta(hours=settings.odds_upcoming_hours),
                Game.status.in_(["scheduled", "preview", "pregame"]),
            )
            .all()
        )

        def age_minutes(game_id: str) -> float | None:
            row = s.execute(
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

        buckets = {"near": [], "mid": [], "far": []}
        for g in games:
            ko = as_utc(g.kickoff_utc)
            assert ko is not None
            h = (ko - now).total_seconds() / 3600.0
            age = age_minutes(g.game_id)
            stale = age is None or age > settings.max_price_age_minutes
            item = {
                "game_id": g.game_id,
                "league": g.league_id,
                "hours_to_kick": round(h, 2),
                "age_minutes": None if age is None else round(age, 1),
                "stale_or_missing": stale,
                "kickoff_utc": ko.isoformat(),
                "before_soft_reset": ko <= soft_reset,
                "before_sunday_local_reset": ko <= horizon_end,
            }
            if h <= settings.odds_near_hours:
                buckets["near"].append(item)
            elif h <= settings.odds_mid_hours:
                buckets["mid"].append(item)
            else:
                buckets["far"].append(item)

        def summarize(items: list[dict]) -> dict:
            before = [x for x in items if x["before_soft_reset"]]
            return {
                "games": len(items),
                "stale_or_missing": sum(1 for x in items if x["stale_or_missing"]),
                "kick_before_soft_reset": len(before),
                "stale_before_soft_reset": sum(1 for x in before if x["stale_or_missing"]),
                "sample_stale_before_reset": [x for x in before if x["stale_or_missing"]][:25],
            }

        out["slate"] = {k: summarize(v) for k, v in buckets.items()}
        out["slate_raw_near_before_reset"] = [x for x in buckets["near"] if x["before_soft_reset"]]
        out["freshness_windows"] = odds_freshness_health(s, now).get("windows")

        # Option B: minimum useful temporary allowance until soft reset
        # Near games before reset needing refresh: stale/missing near kickoffs
        near_need = [x for x in buckets["near"] if x["before_soft_reset"] and x["stale_or_missing"]]
        # Also include near games that are fresh now but will need 1 reobserve before kick if kick is soon
        near_all_before = [x for x in buckets["near"] if x["before_soft_reset"]]
        # Cost model: near uses full market set = 4 credits/event (1 region × 4 markets)
        credits_per_near_fetch = 4
        # Cadence: one refresh now for stale; plus one more if >2h until kick (bounded)
        expected = 0
        maximum = 0
        plan_games = []
        for g in near_all_before:
            refreshes = 0
            if g["stale_or_missing"]:
                refreshes += 1
            # If kickoff still > 2h away after a refresh, allow one follow-up
            if g["hours_to_kick"] > 2:
                refreshes += 1
            refreshes = min(refreshes, 2)
            expected += refreshes * credits_per_near_fetch
            maximum += 2 * credits_per_near_fetch
            plan_games.append({**g, "planned_refreshes": refreshes, "credits": refreshes * credits_per_near_fetch})

        # Round up to a clean ceiling with small headroom for est vs actual
        headroom = 16  # 4 events × 4 credits contingency
        option_b_expected = expected
        option_b_max = maximum + headroom
        # Minimum useful = cover stale near now + one follow-up for longer near games
        out["option_b"] = {
            "scope": "near-kickoff pregame only (<= odds_near_hours), kickoff before soft reset",
            "markets": settings.odds_market_list,
            "credits_per_event_fetch": credits_per_near_fetch,
            "cadence": "1 refresh if stale/missing; at most 1 follow-up if >2h to kick; never far/mid",
            "games_in_scope": len(near_all_before),
            "games_currently_stale_or_missing": len(near_need),
            "expected_credits": option_b_expected,
            "maximum_credits": option_b_max,
            "recommended_additional_allowance": option_b_max,
            "total_ceiling_if_approved": settings.odds_credit_budget_per_day + option_b_max,
            "expires_at_utc": soft_reset.isoformat(),
            "games": plan_games,
            "dollar_cost": "UNKNOWN_FROM_LOCAL_LOGS — Odds API subscription credits; no per-request invoice in app. Confirm on provider dashboard whether remaining quota is prepaid monthly or overage-billed.",
            "incremental_dollar_cost_known": False,
        }

        # Forecast Sat evening → Sunday local reset under revised polling inside 2000
        # After soft reset at Sat 6pm Central, new day budget 2000 with 400 near reserve.
        free_for_mid_far = settings.odds_credit_budget_per_day - settings.odds_near_reserve_credits
        # Rough cycle estimate with new cadence
        sat_sun_games = [g for g in games if as_utc(g.kickoff_utc) and as_utc(g.kickoff_utc) <= horizon_end]
        n_near = sum(1 for g in sat_sun_games if ((as_utc(g.kickoff_utc) - soft_reset).total_seconds() / 3600) <= settings.odds_near_hours)  # rough
        # Better: at soft_reset time, classify by kickoff from soft_reset
        n_near = n_mid = n_far = 0
        for g in games:
            ko = as_utc(g.kickoff_utc)
            if ko is None or ko > horizon_end:
                continue
            h = (ko - soft_reset).total_seconds() / 3600.0
            if h < 0:
                continue
            if h <= settings.odds_near_hours:
                n_near += 1
            elif h <= settings.odds_mid_hours:
                n_mid += 1
            else:
                n_far += 1
        # Polling plan through ~24h window (soft_reset → next UTC midnight is only to Sun 6pm? 
        # soft_reset Sat 6pm CT → next reset Sun 6pm CT = 24h = one soft day 2026-11-08
        # Passes: near every ~15-20m urgent, but deduped; budget: prioritize
        # Conservative demand: 3 near passes × 4cr × n_near_active_per_pass
        # Use capped events per run
        max_per_run = settings.odds_max_events_per_run
        near_cr = 4
        mid_cr = 4
        far_cr = 2
        # Assume ~8 scheduled odds_refresh cycles in 24h (every ~20–60m depending on task)
        cycles = 8
        # Each cycle: up to min(max_per_run, available) near first
        per_cycle_near = min(max_per_run, n_near)
        # With dedupe, near games not re-fetched every cycle — assume ~1/3 of cycles hit each near game
        expected_near_fetches = n_near * 3  # ~3 refreshes per near game over 24h
        expected_mid_fetches = n_mid * 1
        expected_far_fetches = min(n_far, 20) * 1  # thin + skip_far_fresh
        demand = expected_near_fetches * near_cr + expected_mid_fetches * mid_cr + expected_far_fetches * far_cr
        out["post_reset_forecast_to_sunday_local_reset"] = {
            "soft_day_utc": "2026-11-08",
            "window": "2026-11-08T00:00:00Z → 2026-11-09T00:00:00Z (Sat 6pm–Sun 6pm America/Chicago)",
            "games_kicking_in_window": {"near": n_near, "mid": n_mid, "far": n_far},
            "soft_budget": settings.odds_credit_budget_per_day,
            "near_reserve_within_cap": settings.odds_near_reserve_credits,
            "mid_far_spendable": free_for_mid_far,
            "expected_credits_demand": demand,
            "shortfall_vs_2000": max(0, demand - settings.odds_credit_budget_per_day),
            "fits_inside_2000": demand <= settings.odds_credit_budget_per_day,
            "assumptions": {
                "near_refreshes_per_game": 3,
                "mid_refreshes_per_game": 1,
                "far_fetches_capped": min(n_far, 20),
                "near_credits": near_cr,
                "mid_credits": mid_cr,
                "far_credits": far_cr,
                "cycles_hint": cycles,
                "per_cycle_near_cap": per_cycle_near,
            },
        }

    # Provider remaining: sports list is typically low/no market cost; still may consume.
    # Only call if key present; record remaining. Events endpoint costs; /sports is usually free.
    provider_probe: dict = {"attempted": False}
    try:
        adapter = OddsApiAdapter()
        if adapter.api_key:
            provider_probe["attempted"] = True
            # /sports is the documented low-cost/account probe
            result = adapter._get("/sports")
            provider_probe["endpoint"] = "/sports"
            provider_probe["credit_cost_last"] = adapter.last_credits_last
            provider_probe["provider_remaining"] = adapter.last_credits_remaining
            provider_probe["provider_used"] = adapter.last_credits_used
            provider_probe["n_sports"] = len(result.records)
        else:
            provider_probe["error"] = "ODDS_API_KEY not configured"
    except Exception as exc:  # noqa: BLE001
        provider_probe["error"] = str(exc)
    out["provider_probe"] = provider_probe

    path = "data/phase2_budget_decision.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=str)
    print(json.dumps({k: out[k] for k in (
        "budget", "soft_overspend", "window_00_06_utc", "slate", "option_b",
        "post_reset_forecast_to_sunday_local_reset", "provider_probe",
        "last_known_provider_remaining_from_jobs", "local_soft_reset_utc", "local_soft_reset_chicago",
    )}, indent=2, default=str))


if __name__ == "__main__":
    main()
