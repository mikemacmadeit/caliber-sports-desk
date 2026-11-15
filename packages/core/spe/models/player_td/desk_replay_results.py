"""Refresh replay grades from the free ESPN scoreboard and completed box scores."""
import json
import logging
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from spe.config import settings
from spe.db.models import BoardSnapshot, ClosingLine, Game, PublishedStat
from spe.identity.match import normalize_name
from spe.ingest.espn_box import _fetch_summary
from spe.ingest.espn_scoreboard import upsert_scoreboard_events
from spe.publishing.boxes import parse

_lock = threading.Lock()
_states = {}


def fetch_day(league, date):
    path = "nfl" if league == "nfl" else "college-football"
    url = f"https://site.api.espn.com/apis/site/v2/sports/football/{path}/scoreboard?dates={date.replace('-', '')}&limit=400"
    if league == "fbs":
        url += "&groups=80"
    request = urllib.request.Request(url, headers={"User-Agent": "caliber-replay/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response).get("events", [])


def refresh_results(db, date, fetch=fetch_day, summary=_fetch_summary):
    from spe.models.player_td.desk_replay import CACHE_PREFIX
    from sqlalchemy import or_
    snapshots = db.scalars(select(BoardSnapshot).where(or_(BoardSnapshot.key.like(CACHE_PREFIX + "%"), BoardSnapshot.key.like("guarded-desk-v1:%"), BoardSnapshot.key.like("revised-policy-v1:%"), BoardSnapshot.key.like("scorer-replay-v1:%"), BoardSnapshot.key.like("scorer-desk-v1:%"), BoardSnapshot.key.like("retrained-desk-v1:%"), BoardSnapshot.key.like("corrected-inputs-v1:%")))).all()
    selected = {}
    from spe.db.base import as_utc
    from spe.models.player_td.desk_replay import ZONE
    for snapshot in snapshots:
        row = (snapshot.payload or {}).get("row")
        if row and as_utc(row["kickoff_utc"]).astimezone(ZONE).date().isoformat() == date:
            selected[(row["game_id"], row["player_id"])] = row
            # Both saved choices need observed results. Comparison-table players
            # are not picks, and missing box-score rows must remain unresolved.
            for lane in ("most_likely", "value_candidate"):
                choice = (row.get("decision") or {}).get(lane)
                if not choice or not choice.get("player_id") or not choice.get("name"):
                    continue
                if lane == "value_candidate" and not (choice.get("research_selected") or choice.get("price_supported")):
                    continue
                selected[(row["game_id"], choice["player_id"])] = {
                    **row, "player_id": choice["player_id"], "player_name": choice["name"]}
    from spe.ingest.espn_box import _espn_event_id
    from spe.models.player_td.desk_market import store_archive, summary_market
    now = datetime.now(UTC)
    start = datetime.fromisoformat(date).replace(tzinfo=ZONE)
    games = db.scalars(select(Game).where(Game.kickoff_utc >= start.astimezone(UTC),
        Game.kickoff_utc < (start+timedelta(days=1)).astimezone(UTC), Game.data_tier.notin_(("demo", "alias")))).all()
    event_games = {(g.league_id, _espn_event_id(db,g)):g for g in games}
    leagues = sorted({r["league_id"] for r in selected.values()} | {g.league_id for g in games})
    boards = {lg: fetch(lg, date) for lg in leagues}
    tasks = []
    for lg, events in boards.items():
        for event in events:
            game = event_games.get((lg,str(event["id"])))
            gid = game.game_id if game else f"espn:{lg}:{event['id']}"
            if (event.get("status") or {}).get("type", {}).get("completed"):
                if any(key[0] == gid for key in selected):
                    tasks.append((lg, str(event["id"]), gid))
            elif game and as_utc(game.kickoff_utc) > now:
                fresh = db.scalars(select(ClosingLine).where(ClosingLine.game_id == gid,
                    ClosingLine.source == "espn_summary_market", ClosingLine.market_id == "total",
                    ClosingLine.observed_at >= now-timedelta(hours=1))).first()
                if not fresh:
                    tasks.append((lg,str(event["id"]),gid))
    def get_box(task):
        return task, summary(task[0], task[1])
    with ThreadPoolExecutor(max_workers=4) as pool:
        boxes = list(pool.map(get_box, tasks))
    # Finish network reads before taking SQLite's writer lock.
    for lg, events in boards.items():
        upsert_scoreboard_events(db, lg, events)
    written = 0
    missing = []
    for (_, event_id, gid), payload in boxes:
        game = db.get(Game,gid)
        if game and as_utc(game.kickoff_utc) > now:
            context = summary_market(payload,event_id,now)
            if context:
                store_archive(db,{gid:context})
            continue
        completed = any(c.get("status", {}).get("type", {}).get("completed")
                        for c in (payload or {}).get("header", {}).get("competitions", []))
        if not completed:
            missing.append(gid)
            continue
        people = parse(payload)
        for (game_id, pid), row in selected.items():
            if game_id != gid:
                continue
            matches = [p for p in people if normalize_name(p["name"]) == normalize_name(row["player_name"])]
            if len(matches) != 1 or "anytime_td" not in matches[0]["markets"]:
                missing.append(f"{gid}:{pid}")
                continue
            key = f"{gid}|{pid}|anytime_td"
            stat = db.get(PublishedStat, key)
            if stat is None:
                stat = PublishedStat(key=key, game_id=gid, player_id=pid, market_id="anytime_td")
                db.add(stat)
            stat.actual = matches[0]["markets"]["anytime_td"]
            stat.observed_at = datetime.now(UTC)
            stat.source = f"espn.summary:{event_id}"
            written += 1
    db.flush()
    return {"date": date, "scoreboard_games": sum(map(len, boards.values())), "graded_players": written, "missing_boxes": missing}


def ensure_results(date):
    """Local pages refresh free results in the background, at most once a minute."""
    if settings.app_env != "dev":
        return "local_refresh_disabled"
    key = (settings.database_url, date)
    with _lock:
        state = _states.get(key, {})
        if state.get("running"):
            return "running"
        if time.monotonic() - state.get("attempted", -1000) < 60:
            return "failed" if state.get("failed") else "ready"
        _states[key] = {"running": True, "attempted": time.monotonic()}
    def refresh():
        failed = False
        try:
            from spe.db.session import session_factory
            from spe.ingest.public_availability import refresh_public_availability
            # Refresh free pregame reports independently of paid-input jobs.
            # Commit before the separate scoreboard network reads.
            with session_factory() as db:
                refresh_public_availability(db)
                db.commit()
            with session_factory() as db:
                refresh_results(db, date)
                db.commit()
        except Exception:
            failed = True
            logging.getLogger(__name__).exception("Replay results refresh failed for %s", date)
        finally:
            with _lock:
                _states[key].update(running=False, failed=failed)
    threading.Thread(target=refresh, daemon=True, name=f"replay-results-{date}").start()
    return "running"
