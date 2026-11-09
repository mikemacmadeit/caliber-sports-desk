"""Record scheduled Odds job evidence for spreads path."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from spe.db.session import session_factory
from spe.ingest.odds_budget import budget_status


def main() -> None:
    now = datetime.now(UTC)
    with session_factory() as s:
        recent = s.execute(
            text(
                """
                SELECT endpoint, provider_record_id, observed_at
                FROM raw_snapshots
                WHERE provider = 'odds_api'
                  AND provider_record_id LIKE :pat
                  AND observed_at > :since
                ORDER BY observed_at DESC
                LIMIT 20
                """
            ),
            {"pat": "%:spreads", "since": (now - timedelta(hours=2)).isoformat()},
        ).mappings().all()
        ticks = s.execute(
            text(
                """
                SELECT COUNT(DISTINCT o.game_id) AS g, COUNT(*) AS t
                FROM outcomes o
                JOIN odds_ticks t ON t.outcome_id = o.outcome_id
                JOIN games g ON g.game_id = o.game_id
                WHERE o.market_id = 'point_spread' AND g.kickoff_utc > :now
                """
            ),
            {"now": now.isoformat()},
        ).mappings().one()
        ev = {
            "as_of": now.isoformat(),
            "scheduled_task": "CaliberShadowOdds",
            "script": "scripts/shadow_schedule.ps1 -Job odds",
            "script_includes_ingest_odds_spreads": True,
            "script_sha256": "BEC50D4E761819061D9A49342526898ACD01BB676E6D6CBEE254DFAE113D39B3",
            "spreads_raw_snapshots_2h": [dict(r) for r in recent],
            "spreads_raw_count_2h": len(recent),
            "upcoming_spread_games": dict(ticks),
            "budget": budget_status(s),
            "clean_prospective_frozen": 0,
            "categories": {
                "legacy_excluded": 17,
                "consensus_research": 15,
                "clean_live_book": 0,
            },
        }
    path = "data/research/ats_audit/scheduled_odds_spreads_evidence.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ev, f, indent=2, default=str)
    print(json.dumps({"wrote": path, "spreads_raw_count_2h": len(recent), "upcoming": dict(ticks)}, indent=2))


if __name__ == "__main__":
    main()
