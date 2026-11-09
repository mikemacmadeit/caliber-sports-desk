"""Quick Phase 2 data inventory."""
from sqlalchemy import text

from spe.db.session import session_factory

s = session_factory()
queries = [
    "SELECT league_id, COUNT(*) n FROM plays p JOIN games g ON g.game_id=p.game_id WHERE g.data_tier='historical' AND g.status='final' GROUP BY 1",
    "SELECT league_id, MIN(kickoff_utc) mn, MAX(kickoff_utc) mx, COUNT(*) n FROM games WHERE data_tier='historical' AND status='final' GROUP BY 1",
    "SELECT league_id, status, COUNT(*) n FROM games WHERE kickoff_utc > datetime('now') GROUP BY 1,2",
    "SELECT league_id, COUNT(DISTINCT season_year) seasons, MIN(season_year), MAX(season_year) FROM (SELECT g.league_id, CAST(substr(g.season_id, instr(g.season_id,'-')+1) AS INT) season_year FROM games g WHERE g.data_tier='historical' AND g.status='final') GROUP BY 1",
]
for q in queries:
    print("---")
    print(q)
    print([dict(r) for r in s.execute(text(q)).mappings()])
s.close()
