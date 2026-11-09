"""Break down Odds API soft-budget spend for today (UTC)."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from spe.config import settings
from spe.db.session import session_factory


def main() -> None:
    s = session_factory()
    try:
        day = datetime.now(UTC).date().isoformat()
        cu = s.execute(
            text("SELECT provider, day, credits_used, budget, notes FROM credit_usage WHERE provider='odds_api' ORDER BY day DESC LIMIT 5")
        ).mappings().all()
        print("CREDIT_USAGE", json.dumps([dict(r) for r in cu], default=str))

        rows = s.execute(
            text(
                """
                SELECT snapshot_id, endpoint, provider_record_id, observed_at, credit_cost, payload
                FROM raw_snapshots
                WHERE provider='odds_api' AND date(observed_at)=:day AND credit_cost > 0
                ORDER BY observed_at
                """
            ),
            {"day": day},
        ).mappings().all()
        by_endpoint = Counter()
        by_hour = Counter()
        by_league = Counter()
        by_markets = Counter()
        costs = []
        dup_keys = Counter()
        for r in rows:
            cost = int(r["credit_cost"] or 0)
            costs.append(cost)
            by_endpoint[str(r["endpoint"])] += cost
            obs = r["observed_at"]
            if isinstance(obs, str):
                obs = datetime.fromisoformat(obs.replace("Z", "+00:00"))
            by_hour[obs.strftime("%H")] += cost
            payload = r["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            event = (payload or {}).get("event") or {}
            markets = (payload or {}).get("markets_requested") or []
            bucket = (payload or {}).get("market_bucket")
            # Infer league from endpoint
            ep = str(r["endpoint"] or "")
            league = "nfl" if "nfl" in ep else ("fbs" if "ncaaf" in ep else "unknown")
            by_league[league] += cost
            mk = ",".join(markets) if isinstance(markets, list) else str(markets)
            by_markets[mk or bucket or "unknown"] += cost
            key = f"{r['provider_record_id']}|{mk}"
            dup_keys[key] += 1

        print("SNAPSHOTS_WITH_COST", len(rows), "TOTAL_COST", sum(costs))
        print("BY_ENDPOINT", dict(by_endpoint.most_common(20)))
        print("BY_LEAGUE", dict(by_league))
        print("BY_HOUR_UTC", dict(sorted(by_hour.items())))
        print("BY_MARKETS", dict(by_markets.most_common(15)))
        multi = {k: v for k, v in dup_keys.items() if v > 1}
        print("DUPLICATE_EVENT_MARKET_FETCHES", len(multi), "examples", list(multi.items())[:8])

        jobs = s.execute(
            text(
                """
                SELECT job_name, status, started_at, finished_at, summary, error
                FROM job_runs
                WHERE started_at >= :start
                ORDER BY started_at DESC
                LIMIT 40
                """
            ),
            {"start": datetime.now(UTC) - timedelta(hours=36)},
        ).mappings().all()
        print("RECENT_JOBS")
        for j in jobs:
            summary = j["summary"]
            if isinstance(summary, str):
                try:
                    summary = json.loads(summary)
                except Exception:
                    pass
            credits = None
            if isinstance(summary, dict):
                credits = summary.get("credits_spent") or summary.get("ticks_upserted")
            print(
                " ",
                j["job_name"],
                j["status"],
                j["started_at"],
                "credits/ticks",
                credits,
                "err",
                (j["error"] or "")[:80],
            )

        # Demand estimate for upcoming slate
        upcoming = s.execute(
            text(
                """
                SELECT league_id, count(*) AS n,
                       sum(CASE WHEN kickoff_utc <= datetime('now', '+6 hours') THEN 1 ELSE 0 END) AS near6,
                       sum(CASE WHEN kickoff_utc > datetime('now', '+6 hours') AND kickoff_utc <= datetime('now', '+24 hours') THEN 1 ELSE 0 END) AS mid24,
                       sum(CASE WHEN kickoff_utc > datetime('now', '+24 hours') THEN 1 ELSE 0 END) AS far
                FROM games
                WHERE data_tier='upcoming'
                  AND status IN ('scheduled','preview','pregame')
                  AND league_id IN ('nfl','fbs')
                GROUP BY league_id
                """
            )
        ).mappings().all()
        print("UPCOMING_BUCKETS", [dict(r) for r in upcoming])
        # Rough demand: near=4 markets, mid=2, far=1 per event (regions=1)
        demand = 0
        for r in upcoming:
            demand += int(r["near6"] or 0) * 4
            demand += int(r["mid24"] or 0) * 2
            demand += int(r["far"] or 0) * 1
        print(
            "ESTIMATED_CREDIT_DEMAND_ONE_FULL_PASS",
            demand,
            "budget",
            settings.odds_credit_budget_per_day,
            "near_reserve",
            settings.odds_near_reserve_credits,
        )
        print("SETTINGS", {
            "odds_markets": settings.odds_markets,
            "odds_far_markets": settings.odds_far_markets,
            "odds_bookmakers": settings.odds_bookmakers,
            "odds_regions": settings.odds_regions,
            "odds_upcoming_hours": settings.odds_upcoming_hours,
            "odds_min_refresh_minutes": settings.odds_min_refresh_minutes,
            "odds_max_events_per_run": settings.odds_max_events_per_run,
            "max_price_age_minutes": settings.max_price_age_minutes,
        })
    finally:
        s.close()


if __name__ == "__main__":
    main()
