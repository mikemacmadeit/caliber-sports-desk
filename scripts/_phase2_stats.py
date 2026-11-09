"""Emit Phase 2 scoreboard stats as JSON."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import text

from spe.db.session import session_factory
from spe.ops.freshness import injury_coverage_health, odds_freshness_health


def main() -> None:
    now = datetime.now(UTC)
    with session_factory() as s:
        of = odds_freshness_health(s, now)
        inj = injury_coverage_health(s, now)
        reasons: Counter[str] = Counter()
        rows = s.execute(
            text(
                """
                SELECT reason_codes FROM forecasts
                WHERE reason_codes LIKE '%IDENTITY_CONFLICT%'
                """
            )
        ).scalars().all()
        for rc in rows:
            codes = rc if isinstance(rc, list) else []
            if isinstance(rc, str):
                try:
                    codes = json.loads(rc)
                except Exception:
                    codes = []
            for c in codes or []:
                if c != "IDENTITY_CONFLICT":
                    reasons[str(c)] += 1
        payload = {
            "odds_freshness": {
                "price_rows": of.get("price_rows"),
                "stale_rows": of.get("stale_rows"),
                "stale_share": of.get("stale_share"),
                "near_kickoff_stale_rows": of.get("near_kickoff_stale_rows"),
                "pregame_price_rows": of.get("pregame_price_rows"),
                "pregame_stale_rows": of.get("pregame_stale_rows"),
                "windows": of.get("windows"),
                "by_league_window": of.get("by_league_window"),
                "denominator_note": of.get("denominator_note"),
            },
            "injury": {
                "coverage_counts": inj.get("coverage_counts"),
                "by_league": inj.get("by_league"),
                "api_sports_injury_ingest": inj.get("api_sports_injury_ingest"),
                "honesty": inj.get("honesty"),
            },
            "identity_conflict_secondary_reasons": reasons.most_common(20),
            "identity_conflict_forecasts": len(rows),
        }
    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
