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
from spe.db.models import BoardSnapshot, ClosingLine, Game, PublishedStat, RawSnapshot
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


def _remember_pick(selected, row):
    if not isinstance(row, dict) or not row.get("game_id"):
        return
    if row.get("player_id"):
        selected[(row["game_id"], row["player_id"])] = row
    # Both saved choices need observed results. Comparison-table players
    # are not picks, and missing box-score rows must remain unresolved.
    for lane in ("most_likely", "value_candidate"):
        choice = (row.get("decision") or {}).get(lane) or {}
        if choice.get("player_id") and choice.get("name"):
            selected[(row["game_id"], choice["player_id"])] = {
                **row, "player_id": choice["player_id"], "player_name": choice["name"]}


def _named_picks(db, date):
    """Picks on the saved day card. One key lookup, not a scan of every old card."""
    from spe.models.player_td.desk_replay import day_card_key
    selected = {}
    found = False
    for league in ("all", "nfl", "fbs"):
        saved = db.get(BoardSnapshot, day_card_key(date, league))
        payload = saved.payload if saved is not None else None
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not rows:
            continue
        found = True
        for row in rows:
            _remember_pick(selected, row)
    return selected if found else None


def refresh_results(db, date, fetch=fetch_day, summary=_fetch_summary, *, cards_only=False):
    from spe.db.base import as_utc
    from spe.models.player_td.desk_replay import ZONE
    selected = _named_picks(db, date)
    if selected is None and cards_only:
        return {"status": "ok", "failures": [], "date": date, "scoreboard_games": 0,
                "graded_players": 0, "missing_boxes": []}
    if selected is None:
        from spe.models.player_td.desk_replay import CACHE_PREFIX
        from sqlalchemy import or_
        # Filter JSON dates in SQL; do not deserialize every historical card.
        start = datetime.fromisoformat(date).replace(tzinfo=ZONE)
        end = start + timedelta(days=1)
        kickoff = BoardSnapshot.payload["row"]["kickoff_utc"].as_string()
        snapshots = db.scalars(select(BoardSnapshot).where(kickoff >= start.astimezone(UTC).isoformat(), kickoff < end.astimezone(UTC).isoformat(), or_(BoardSnapshot.key.like(CACHE_PREFIX + "%"), BoardSnapshot.key.like("guarded-desk-v1:%"), BoardSnapshot.key.like("revised-policy-v1:%"), BoardSnapshot.key.like("scorer-replay-v1:%"), BoardSnapshot.key.like("scorer-desk-v1:%"), BoardSnapshot.key.like("retrained-desk-v1:%"), BoardSnapshot.key.like("corrected-inputs-v1:%")))).all()
        selected = {}
        for snapshot in snapshots:
            row = (snapshot.payload or {}).get("row")
            if row and as_utc(row["kickoff_utc"]).astimezone(ZONE).date().isoformat() == date:
                _remember_pick(selected, row)
    from spe.ingest.espn_box import _espn_event_id
    from spe.models.player_td.desk_market import store_archive, store_pregame_quote, summary_market, PREGAME_MARKET_ENDPOINT
    now = datetime.now(UTC)
    start = datetime.fromisoformat(date).replace(tzinfo=ZONE)
    games = db.scalars(select(Game).where(Game.kickoff_utc >= start.astimezone(UTC),
        Game.kickoff_utc < (start+timedelta(days=1)).astimezone(UTC), Game.data_tier.notin_(("demo", "alias")))).all()
    event_games = {(g.league_id, _espn_event_id(db,g)):g for g in games}
    leagues = ["nfl", "fbs"]
    boards, failures = {}, []
    for lg in leagues:
        try:
            boards[lg] = fetch(lg, date)
        except Exception as exc:
            failures.append({"league": lg, "stage": "scoreboard", "error": type(exc).__name__})
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
                fresh_quote = db.scalars(select(RawSnapshot).where(RawSnapshot.provider == "espn",
                    RawSnapshot.endpoint == PREGAME_MARKET_ENDPOINT, RawSnapshot.provider_record_id == gid,
                    RawSnapshot.observed_at >= now-timedelta(hours=1)).limit(1)).first()
                if not fresh and not fresh_quote:
                    tasks.append((lg,str(event["id"]),gid))
    def get_box(task):
        try:
            return task, summary(task[0], task[1])
        except Exception:
            return task, None
    # Release the database connection before the score reads. A dropped
    # connection used to erase every grade from this pass.
    db.commit()
    with ThreadPoolExecutor(max_workers=4) as pool:
        boxes = list(pool.map(get_box, tasks))
    # Finish network reads before taking SQLite's writer lock.
    for lg, events in boards.items():
        upsert_scoreboard_events(db, lg, events)
    written = 0
    missing = []
    from spe.identity.results import espn_player_ids, match_result
    identities = espn_player_ids(db, {pid for _, pid in selected})
    for (_, event_id, gid), payload in boxes:
        game = db.get(Game,gid)
        received_at = datetime.now(UTC)
        if game and as_utc(game.kickoff_utc) > received_at:
            context = summary_market(payload,event_id,received_at)
            if context:
                store_archive(db,{gid:context})
                store_pregame_quote(db,gid,context)
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
            person = match_result(people, pid, row["player_name"], identities, row.get("team"))
            if person is None or "anytime_td" not in person["markets"]:
                missing.append(f"{gid}:{pid}")
                continue
            key = f"{gid}|{pid}|anytime_td"
            stat = db.get(PublishedStat, key)
            if stat is None:
                stat = PublishedStat(key=key, game_id=gid, player_id=pid, market_id="anytime_td")
                db.add(stat)
            stat.actual = person["markets"]["anytime_td"]
            stat.observed_at = datetime.now(UTC)
            stat.source = f"espn.summary:{event_id}"
            written += 1
        db.commit()
    db.flush()
    print(f"refresh_results {date} graded={written} missing={len(missing)}", flush=True)
    return {"status": "degraded" if failures or missing else "ok", "failures": failures, "date": date, "scoreboard_games": sum(map(len, boards.values())), "graded_players": written, "missing_boxes": missing}


def ensure_live_touchdowns(date):
    """Write touchdowns that have already happened, then grade the saved card.

    Page changes read the saved names. This only fills in wins and losses.
    """
    key = ("live", settings.database_url, date)
    with _lock:
        state = _states.get(key, {})
        if state.get("running"):
            return "running"
        if time.monotonic() - state.get("attempted", -1000) < 20:
            return "ready"
        _states[key] = {"running": True, "attempted": time.monotonic()}

    def refresh():
        failed = False
        try:
            from datetime import timedelta
            from spe.db.base import as_utc
            from spe.db.session import session_factory
            from spe.desk.day_results import refresh_live_touchdowns
            from spe.models.player_td.desk_replay import ZONE, saved_day_card
            start = datetime.fromisoformat(date).replace(tzinfo=ZONE)
            with session_factory() as db:
                games = db.scalars(select(Game).where(
                    Game.kickoff_utc >= start.astimezone(UTC),
                    Game.kickoff_utc < (start + timedelta(days=1)).astimezone(UTC),
                    Game.data_tier.notin_(("demo", "alias")),
                )).all()
                started = [game for game in games if as_utc(game.kickoff_utc) <= datetime.now(UTC)]
                if started:
                    refresh_live_touchdowns(db, started)
                    db.commit()
            with session_factory() as db:
                for league in ("all", "nfl", "fbs"):
                    saved_day_card(db, date, league)
                db.commit()
        except Exception:
            failed = True
            logging.getLogger(__name__).exception("Live touchdown refresh failed for %s", date)
        finally:
            with _lock:
                _states[key].update(running=False, failed=failed)

    threading.Thread(target=refresh, daemon=True, name=f"live-td-{date}").start()
    return "running"


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
