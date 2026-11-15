"""Bounded, cached public ESPN market archive. No paid requests or outcome features."""
import json
import random
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from spe.ingest.espn_box import _fetch_summary

TARGET = Path("data/cache/desk_market_archive.json")


def fetch(row):
    gid, league, event = row
    payload = _fetch_summary(league, event) or {}
    for quote in payload.get("pickcenter") or []:
        try:
            total = float(quote["overUnder"])
            spread = abs(float(quote["spread"]))
        except (KeyError, TypeError, ValueError):
            continue
        home = quote.get("homeTeamOdds") or {}
        away = quote.get("awayTeamOdds") or {}
        if spread and not (home.get("favorite") or away.get("favorite")):
            continue
        handicap = -spread if home.get("favorite") else spread
        if not 0 < total < 150 or abs(handicap) >= total:
            continue
        return gid, {"home_handicap": handicap, "total": total, "source": "espn_summary_market",
                     "observed_at": datetime.now(UTC).isoformat(), "event_id": event}
    return gid, None


def main():
    existing = json.loads(TARGET.read_text()) if TARGET.exists() else {}
    with sqlite3.connect(Path("data/caliber.db").resolve().as_uri()+"?mode=ro", uri=True) as db:
        candidates = list(db.execute("""SELECT DISTINCT g.game_id,g.league_id,c.provider_record_id,substr(g.kickoff_utc,1,4)
            FROM closing_lines c JOIN games g ON g.game_id=c.game_id WHERE c.provider='cfbd'
            AND c.market_id='point_spread' AND g.kickoff_utc>='2023-07-01' AND g.kickoff_utc<'2026-11-14'"""))
        targets = []
        rng = random.Random(20261114)
        for year, n in (("2023",100),("2024",100),("2025",200),("2026",60)):
            pool = sorted(r[:3] for r in candidates if r[3] == year)
            targets.extend(rng.sample(pool, min(n,len(pool))))
        targets.extend((gid,lg,gid.split(":")[-1]) for gid,lg in db.execute("""SELECT game_id,league_id FROM games
            WHERE game_id LIKE 'espn:%' AND kickoff_utc>='2026-11-14' AND kickoff_utc<'2026-11-17'
            AND data_tier NOT IN ('demo','alias')"""))
    targets = sorted(set(r for r in targets if r[0] not in existing))
    print(f"Fetching at most {len(targets)} public summaries; four concurrent requests; cached on disk.",flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for i,(gid,context) in enumerate(pool.map(fetch,targets),1):
            if context:
                existing[gid]=context
            if i % 25 == 0:
                TARGET.write_text(json.dumps(existing,indent=2))
                print(f"{i}/{len(targets)} requests; {len(existing)} usable cached markets",flush=True)
    TARGET.write_text(json.dumps(existing,indent=2))
    print(f"Finished: {len(existing)} cached markets",flush=True)


if __name__ == "__main__":
    main()
