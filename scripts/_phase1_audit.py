"""Read-only Phase 1 TD data-integrity audit against the live DB."""
from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import text

import importlib.util
from pathlib import Path

from spe.db.session import session_factory
from spe.integrity.assignments import audit_published_forecasts


def _load_games_module():
    path = Path(__file__).resolve().parents[1] / "packages" / "core" / "spe" / "identity" / "games.py"
    spec = importlib.util.spec_from_file_location("spe_identity_games", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod

REPORT: dict = {"generated_at": datetime.now(UTC).isoformat()}


def main() -> None:
    s = session_factory()
    try:
        rows = s.execute(
            text(
                """
                SELECT league_id,
                       CASE
                         WHEN game_id LIKE 'espn:%' THEN 'espn'
                         WHEN game_id LIKE 'nfl:%' THEN 'nfl'
                         WHEN game_id LIKE 'fbs:%' THEN 'fbs'
                         ELSE 'other'
                       END AS kind,
                       data_tier,
                       COUNT(*) AS n
                FROM games
                WHERE kickoff_utc >= datetime('now', '-2 days')
                GROUP BY 1, 2, 3
                ORDER BY 1, 3, 2
                """
            )
        ).mappings().all()
        REPORT["upcoming_games"] = [dict(r) for r in rows]

        games_mod = _load_games_module()
        dups = games_mod.find_duplicate_groups(s, upcoming_only=True)
        REPORT["duplicate_groups"] = len(dups)
        REPORT["duplicate_examples"] = [[g.game_id for g in group] for group in dups[:12]]

        REPORT["players"] = {
            "espn_style_nfl": s.execute(
                text(
                    "SELECT COUNT(*) FROM players "
                    "WHERE player_id LIKE 'nfl:%' AND player_id NOT LIKE 'nfl:00-%'"
                )
            ).scalar(),
            "gsis_nfl": s.execute(
                text("SELECT COUNT(*) FROM players WHERE player_id LIKE 'nfl:00-%'")
            ).scalar(),
            "fbs": s.execute(text("SELECT COUNT(*) FROM players WHERE player_id LIKE 'fbs:%'")).scalar(),
        }

        REPORT["rosters"] = {
            "open": s.execute(text("SELECT COUNT(*) FROM rosters WHERE valid_to IS NULL")).scalar(),
            "closed": s.execute(text("SELECT COUNT(*) FROM rosters WHERE valid_to IS NOT NULL")).scalar(),
            "multi_open_teams": s.execute(
                text(
                    """
                    SELECT COUNT(*) FROM (
                      SELECT player_id FROM rosters
                      WHERE valid_to IS NULL
                      GROUP BY player_id
                      HAVING COUNT(DISTINCT team_id) > 1
                    )
                    """
                )
            ).scalar(),
        }

        REPORT["features_atd"] = [
            dict(r)
            for r in s.execute(
                text(
                    """
                    SELECT g.league_id,
                           COUNT(DISTINCT g.game_id) AS games,
                           COUNT(DISTINCT CASE WHEN f.feature_id IS NOT NULL THEN g.game_id END)
                             AS games_with_features,
                           SUM(CASE WHEN o.market_id = 'anytime_td' THEN 1 ELSE 0 END) AS atd_outcomes,
                           SUM(
                             CASE
                               WHEN o.market_id = 'anytime_td'
                                AND o.player_id LIKE 'nfl:%'
                                AND o.player_id NOT LIKE 'nfl:00-%'
                               THEN 1 ELSE 0
                             END
                           ) AS espn_id_atd,
                           SUM(
                             CASE
                               WHEN o.market_id = 'anytime_td' AND o.player_id LIKE 'nfl:00-%'
                               THEN 1 ELSE 0
                             END
                           ) AS gsis_atd
                    FROM games g
                    LEFT JOIN features f ON f.game_id = g.game_id
                    LEFT JOIN outcomes o ON o.game_id = g.game_id
                    WHERE g.data_tier = 'upcoming'
                      AND g.kickoff_utc >= datetime('now', '-1 day')
                    GROUP BY g.league_id
                    """
                )
            ).mappings().all()
        ]

        REPORT["espn_id_history_sample"] = [
            dict(r)
            for r in s.execute(
                text(
                    """
                    SELECT o.player_id, p.name,
                           (SELECT COUNT(*) FROM player_game_stats pgs WHERE pgs.player_id = o.player_id) AS hist
                    FROM outcomes o
                    JOIN players p ON p.player_id = o.player_id
                    JOIN games g ON g.game_id = o.game_id
                    WHERE o.market_id = 'anytime_td'
                      AND g.data_tier = 'upcoming'
                      AND o.player_id LIKE 'nfl:%'
                      AND o.player_id NOT LIKE 'nfl:00-%'
                    LIMIT 12
                    """
                )
            ).mappings().all()
        ]

        REPORT["gsis_history_sample"] = [
            dict(r)
            for r in s.execute(
                text(
                    """
                    SELECT o.player_id, p.name,
                           (SELECT COUNT(*) FROM player_game_stats pgs WHERE pgs.player_id = o.player_id) AS hist
                    FROM outcomes o
                    JOIN players p ON p.player_id = o.player_id
                    JOIN games g ON g.game_id = o.game_id
                    WHERE o.market_id = 'anytime_td'
                      AND g.data_tier = 'upcoming'
                      AND o.player_id LIKE 'nfl:00-%'
                    LIMIT 12
                    """
                )
            ).mappings().all()
        ]

        # Open roster rows that disagree with latest RosterWeek for same player.
        REPORT["roster_week_vs_open"] = [
            dict(r)
            for r in s.execute(
                text(
                    """
                    WITH latest AS (
                      SELECT player_id, team_id, season_year, week,
                             ROW_NUMBER() OVER (
                               PARTITION BY player_id ORDER BY season_year DESC, week DESC
                             ) AS rn
                      FROM roster_weeks
                      WHERE provider = 'nflverse'
                    )
                    SELECT r.player_id, p.name, r.team_id AS open_team, l.team_id AS week_team,
                           l.season_year, l.week
                    FROM rosters r
                    JOIN players p ON p.player_id = r.player_id
                    JOIN latest l ON l.player_id = r.player_id AND l.rn = 1
                    WHERE r.valid_to IS NULL
                      AND r.team_id != l.team_id
                    LIMIT 20
                    """
                )
            ).mappings().all()
        ]

        audit = audit_published_forecasts(s)
        REPORT["forecast_audit"] = {
            "audited": audit["audited"],
            "conflicts": audit["conflicts"],
            "events_checked": audit["events_checked"],
            "by_reason": audit["by_reason"],
            "sample": audit["rows"][:15],
        }

        REPORT["provider_maps"] = [
            dict(r)
            for r in s.execute(
                text(
                    """
                    SELECT provider, entity_type, COUNT(*) AS n
                    FROM provider_id_map
                    GROUP BY 1, 2
                    ORDER BY 1, 2
                    """
                )
            ).mappings().all()
        ]

        print(json.dumps(REPORT, indent=2, default=str))
    finally:
        s.close()


if __name__ == "__main__":
    main()
