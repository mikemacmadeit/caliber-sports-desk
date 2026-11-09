"""Summarize today's Odds API soft-budget spend by job/endpoint (offline)."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime

from sqlalchemy import text

from spe.db.session import session_factory
from spe.ingest.odds_budget import budget_day, budget_status


def main() -> None:
    day = budget_day()
    with session_factory() as s:
        # Raw snapshots with credit_cost > 0 historically; new path uses credit_cost=0 on raw
        # after reserve — also attribute via job_runs summaries.
        by_endpoint: Counter[str] = Counter()
        by_hour: Counter[str] = Counter()
        cost_endpoint: Counter[str] = Counter()
        rows = s.execute(
            text(
                """
                SELECT endpoint, credit_cost, observed_at, payload
                FROM raw_snapshots
                WHERE provider = 'odds_api'
                  AND date(observed_at) = :day
                """
            ),
            {"day": day},
        ).mappings().all()
        for row in rows:
            ep = str(row["endpoint"] or "")
            key = "spreads" if "spread" in ep.lower() or (isinstance(row["payload"], str) and "spreads" in row["payload"]) else "event_odds"
            if isinstance(row["payload"], dict) and row["payload"].get("dataset") == "spreads":
                key = "spreads"
            elif ep.startswith("odds_event") or "/events/" in ep:
                key = "props_event"
            elif "sports" in ep and "odds" in ep:
                key = "league_odds"
            by_endpoint[key] += 1
            cost_endpoint[key] += int(row["credit_cost"] or 0)
            ts = row["observed_at"]
            if ts is not None:
                by_hour[str(ts)[11:13]] += 1

        jobs = s.execute(
            text(
                """
                SELECT job_name, status, summary, started_at
                FROM job_runs
                WHERE date(started_at) = :day
                  AND job_name LIKE '%odds%'
                ORDER BY started_at DESC
                LIMIT 40
                """
            ),
            {"day": day},
        ).mappings().all()
        job_spend = []
        for j in jobs:
            summary = j["summary"]
            if isinstance(summary, str):
                try:
                    summary = json.loads(summary)
                except Exception:
                    summary = {}
            summary = summary or {}
            job_spend.append(
                {
                    "job_name": j["job_name"],
                    "status": j["status"],
                    "started_at": j["started_at"],
                    "credits_spent": summary.get("credits_spent"),
                    "events_fetched": summary.get("events_fetched"),
                    "events_skipped": summary.get("events_skipped"),
                    "skipped_dedup": summary.get("skipped_dedup"),
                    "skipped_for_near_reserve": summary.get("skipped_for_near_reserve"),
                    "run_status": summary.get("status"),
                }
            )

        print(
            json.dumps(
                {
                    "day_utc": day,
                    "budget": budget_status(s),
                    "raw_snapshot_counts_by_kind": dict(by_endpoint),
                    "raw_credited_cost_by_kind": dict(cost_endpoint),
                    "raw_requests_by_hour_utc": dict(sorted(by_hour.items())),
                    "recent_odds_jobs": job_spend[:20],
                },
                indent=2,
                default=str,
            )
        )


if __name__ == "__main__":
    main()
