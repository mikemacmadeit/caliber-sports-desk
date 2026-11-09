"""Exact ATD capture gaps + blocked settlement audit."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime

from sqlalchemy import text

from spe.db.session import init_db, session_factory

init_db()
s = session_factory()
s.connection().exec_driver_sql("PRAGMA busy_timeout=120000")
now = datetime.now(UTC)

FAM = """
CASE
  WHEN s.model_version_id LIKE 'm3-%' THEN 'm3'
  WHEN s.model_version_id LIKE 'm4-%' THEN 'm4'
  WHEN s.model_version_id LIKE 'm5-%' THEN 'm5'
  WHEN s.model_version_id LIKE 'm6_hist%' OR s.model_version_id LIKE '%hist_gbm%'
       OR s.model_version_id LIKE '%histgbm%' THEN 'm6_histgbm'
  WHEN s.model_version_id LIKE 'm6-%' THEN 'm6'
  WHEN s.model_version_id LIKE 'atd_sim%' THEN 'atd_sim_v1'
  WHEN s.model_version_id LIKE 'player_td%' THEN 'player_td_v1'
  ELSE 'other'
END
"""

print("=== SETTLEMENT STATUS ===")
for r in s.execute(
    text(
        f"""
        SELECT {FAM} fam, st.result, COUNT(*) rows,
               COUNT(DISTINCT s.player_id||'|'||s.game_id) player_games,
               COUNT(DISTINCT s.game_id) games
        FROM shadow_predictions s
        LEFT JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
        WHERE s.market_id='anytime_td' AND s.forecast_as_of < (
          SELECT kickoff_utc FROM games g WHERE g.game_id=s.game_id
        )
        GROUP BY 1,2 ORDER BY 1,2
        """
    )
).fetchall():
    print(r)

print("\n=== BLOCKED DETAIL ===")
blocked = s.execute(
    text(
        """
        SELECT s.shadow_id, s.league_id, s.game_id, s.player_id, pl.name, s.model_version_id,
               st.void_reason, st.settled_at, g.kickoff_utc, g.status,
               CASE WHEN EXISTS(
                 SELECT 1 FROM player_game_stats p WHERE p.game_id=s.game_id AND p.player_id=s.player_id
               ) THEN 1 ELSE 0 END has_box,
               (SELECT COUNT(*) FROM player_game_stats p WHERE p.game_id=s.game_id) game_box_rows
        FROM shadow_settlements st
        JOIN shadow_predictions s ON s.shadow_id=st.shadow_id
        JOIN games g ON g.game_id=s.game_id
        LEFT JOIN players pl ON pl.player_id=s.player_id
        WHERE s.market_id='anytime_td' AND st.result='blocked'
        ORDER BY g.kickoff_utc, s.player_id
        """
    )
).mappings().all()
print("blocked_family_rows", len(blocked))
print("blocked_unique_pg", len({(r["player_id"], r["game_id"]) for r in blocked}))
print("blocked_unique_games", len({r["game_id"] for r in blocked}))
print("void_reasons", Counter(r["void_reason"] for r in blocked))
print("has_box_now", Counter(r["has_box"] for r in blocked))
for r in blocked[:12]:
    print(dict(r))

print("\n=== NFL PRICED GAPS (upcoming tier only) ===")
required = ("m3", "m4", "m5", "m6", "atd_sim_v1", "player_td_v1", "m6_histgbm")
priced = s.execute(
    text(
        """
        WITH latest AS (
          SELECT o.outcome_id, o.game_id, o.player_id, g.league_id, g.kickoff_utc, g.data_tier,
                 g.status, pl.name, pl.position_group,
                 t.captured_at AS quote_at, t.price_american,
                 ROW_NUMBER() OVER (PARTITION BY o.outcome_id ORDER BY t.captured_at DESC) rn
          FROM outcomes o
          JOIN games g ON g.game_id=o.game_id
          JOIN odds_ticks t ON t.outcome_id=o.outcome_id
          LEFT JOIN players pl ON pl.player_id=o.player_id
          WHERE o.market_id='anytime_td' AND o.side IN ('yes','over') AND o.player_id IS NOT NULL
            AND g.kickoff_utc > :now AND g.status IN ('scheduled','preview','pregame')
            AND g.league_id='nfl' AND g.data_tier='upcoming'
        )
        SELECT * FROM latest WHERE rn=1
        """
    ),
    {"now": now.isoformat()},
).mappings().all()
print("priced_upcoming_nfl_outcomes", len(priced))
print("priced_upcoming_nfl_games", len({r["game_id"] for r in priced}))

miss = defaultdict(list)
have_c = Counter()
for row in priced:
    present = {
        r[0]
        for r in s.execute(
            text(
                f"""
                SELECT {FAM} fam FROM shadow_predictions s
                JOIN games g ON g.game_id=s.game_id
                WHERE s.outcome_id=:oid AND s.market_id='anytime_td'
                  AND s.forecast_as_of < g.kickoff_utc
                """
            ),
            {"oid": row["outcome_id"]},
        ).fetchall()
    }
    for fam in required:
        if fam in present:
            have_c[fam] += 1
        else:
            miss[fam].append(row)

print("have", dict(have_c))
print("missing_counts", {k: len(v) for k, v in miss.items()})
for fam, rows in miss.items():
    by_game = Counter(r["game_id"] for r in rows)
    by_pos = Counter(r["position_group"] for r in rows)
    print(f"\n-- {fam} missing {len(rows)} --")
    print("  games", dict(by_game.most_common(8)))
    print("  pos", dict(by_pos))
    for r in rows[:5]:
        print(" ", r["name"], r["position_group"], r["game_id"], "kick", r["kickoff_utc"])

print("\n=== RESEARCH PREDICTIONS WITHOUT PRICE (NFL upcoming) ===")
# player_td freezes for upcoming games that lack current priced outcome
unpriced = s.execute(
    text(
        """
        SELECT COUNT(DISTINCT s.player_id||'|'||s.game_id)
        FROM shadow_predictions s
        JOIN games g ON g.game_id=s.game_id
        WHERE s.market_id='anytime_td' AND s.model_version_id LIKE 'player_td%'
          AND g.league_id='nfl' AND g.data_tier='upcoming'
          AND g.kickoff_utc > :now AND s.forecast_as_of < g.kickoff_utc
          AND NOT EXISTS (
            SELECT 1 FROM outcomes o
            JOIN odds_ticks t ON t.outcome_id=o.outcome_id
            WHERE o.game_id=s.game_id AND o.player_id=s.player_id AND o.market_id='anytime_td'
          )
        """
    ),
    {"now": now.isoformat()},
).scalar()
print("player_td_unpriced_player_games", unpriced)

print("\n=== JOB RUNS ===")
for r in s.execute(
    text(
        """
        SELECT job_name, status, started_at, finished_at, substr(CAST(summary AS TEXT),1,120)
        FROM job_runs
        WHERE job_name IN ('shadow_capture','atd_candidate_capture','shadow_settle','odds_refresh','pregame_forecasts')
        ORDER BY started_at DESC LIMIT 15
        """
    )
).fetchall():
    print(r)

s.close()
