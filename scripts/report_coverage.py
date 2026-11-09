from sqlalchemy import text

from spe.db.session import init_db, session_factory
from spe.ingest.coverage import coverage_report

init_db()
s = session_factory()
r = coverage_report(s)
print("COUNTS", r["counts"])
print("CREDS", r["credentials"])
print("--- coverage rows ---")
for row in r["coverage"]:
    print(
        f"{row['league_id']:4} {row['provider']:12} {row['dataset']:28} "
        f"season={row['season_year']} n={row['row_count']} {row['status']}"
    )
print("--- sample historical games ---")
for gid, week, home, away, kick, tier in s.execute(
    text(
        """
        SELECT g.game_id, g.week, h.abbreviation, a.abbreviation, g.kickoff_utc, g.data_tier
        FROM games g
        JOIN teams h ON h.team_id = g.home_team_id
        JOIN teams a ON a.team_id = g.away_team_id
        WHERE g.data_tier='historical' AND g.league_id='nfl'
        ORDER BY g.kickoff_utc
        LIMIT 5
        """
    )
):
    print(gid, week, away, "@", home, kick, tier)
print("--- 2024 player-game stats sample ---")
for name, targets, rec, rec_yds, rush_yds, rush_td, rec_td, q in s.execute(
    text(
        """
        SELECT p.name, s.targets, s.receptions, s.rec_yards, s.rush_yards, s.rush_td, s.rec_td, s.quality_status
        FROM player_game_stats s
        JOIN players p ON p.player_id = s.player_id
        JOIN games g ON g.game_id = s.game_id
        WHERE g.season_id='nfl-2024' AND s.targets >= 8
        ORDER BY s.rec_yards DESC
        LIMIT 8
        """
    )
):
    print(name, "tgt", targets, "rec", rec, "rec_yds", rec_yds, "rush_yds", rush_yds, "td", rush_td + rec_td, q)
print("demo games", s.execute(text("SELECT COUNT(*) FROM games WHERE data_tier='demo'")).scalar())
print("nfl seasons", [row[0] for row in s.execute(text("SELECT season_id, COUNT(*) FROM games WHERE league_id='nfl' AND data_tier='historical' GROUP BY season_id ORDER BY 1"))])
s.close()
