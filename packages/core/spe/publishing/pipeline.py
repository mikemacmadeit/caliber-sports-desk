"""Durable scheduled jobs. Failures survive rollback; concurrent lanes are locked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from contextlib import contextmanager
from sqlalchemy import text
from spe.db.base import as_utc, new_id
from spe.db.models import JobRun
from spe.db.session import session_factory, get_engine, init_db

# The homepage and the ATS board have to be written before the price download.
# That download, and a long score of games that already kicked off, used to run
# until the database dropped the connection. ATS never started, and the live
# card stayed empty. Score today's still-upcoming games first, refresh ATS from
# lines already stored, then download and publish again.
ODDS_LANE = (
    "production_ats",
    "production_publish",
    "production_pregame_availability",
    "production_odds",
    "production_spreads",
    "production_publish",
    "production_ats",
)


JOBS = {
    "production_odds": 900,
    "production_pregame_availability": 900,
    "production_ats": 900,
    "production_spreads": 900,
    "production_free_college": 8 * 3600,
    "production_publish": 900,
    "production_touchdowns": 900,
    "production_inputs": 2400,
    "production_weather": 10800,
    "production_nflverse": 8 * 3600,
    "production_settlement": 2400,
}


@contextmanager
def lane_lock(lane):
    # Session-level advisory locks remain held across commits and release on crash.
    # The database drops a socket after several minutes. Hold the lock on a
    # connection that is renewed before that, and never fail the lane because
    # the unlock itself finds a dead socket.
    if get_engine().dialect.name == "postgresql":
        import threading
        import time

        key = {"odds": 81301, "inputs": 81302, "settlement": 81303, "touchdowns": 81304}[lane]
        state = {"held": False}
        stop = threading.Event()
        ready = threading.Event()

        def _keep():
            failures = 0
            while not stop.is_set():
                conn = None
                try:
                    conn = get_engine().connect()
                    locked = bool(conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar())
                    conn.commit()
                    state["held"] = locked
                    ready.set()
                    if not locked:
                        return
                    failures = 0
                    opened = time.monotonic()
                    while not stop.is_set() and time.monotonic() - opened < 150:
                        if stop.wait(20):
                            break
                        conn.execute(text("SELECT 1"))
                        conn.commit()
                except Exception:
                    state["held"] = False
                    failures += 1
                    if not ready.is_set() and failures >= 3:
                        ready.set()
                        return
                    if stop.is_set():
                        return
                    time.sleep(1)
                    continue
                finally:
                    if conn is not None:
                        try:
                            if state["held"]:
                                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                                conn.commit()
                        except Exception:
                            pass
                        try:
                            conn.close()
                        except Exception:
                            pass
            state["held"] = False

        keeper = threading.Thread(target=_keep, name=f"lane-lock-{lane}", daemon=True)
        keeper.start()
        ready.wait(15)
        try:
            yield bool(state["held"])
        finally:
            stop.set()
            keeper.join(timeout=5)
    else:
        import os
        from spe.config import settings

        lock_path = settings.parquet_dir.parent / f"production-{lane}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Opening with "w" truncates the file and raises PermissionError on
            # Windows while another job already holds it. That crashed the lane
            # instead of reporting it busy.
            file = open(lock_path, "a+b")
        except OSError:
            yield False
            return
        acquired = False
        try:
            try:
                if os.name == "nt":
                    import msvcrt

                    if file.seek(0, os.SEEK_END) < 1:
                        file.write(b"0")
                        file.flush()
                    file.seek(0)
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                acquired = False
            yield acquired
        finally:
            if acquired:
                if os.name == "nt":
                    file.seek(0)
                    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(file.fileno(), fcntl.LOCK_UN)
            file.close()


def run_step(name, fn):
    started = datetime.now(UTC)
    run_id = new_id()
    with session_factory() as db:
        db.add(
            JobRun(run_id=run_id, job_name=name, started_at=started, status="running", summary={})
        )
        db.commit()
    import logging
    import threading
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(30):
            try:
                with session_factory() as session:
                    record = session.get(JobRun, run_id)
                    if record and record.status == "running":
                        record.summary = {**(record.summary or {}), "heartbeat_at": datetime.now(UTC).isoformat()}
                        session.commit()
            except Exception:
                logging.getLogger(__name__).warning("Job heartbeat unavailable: %s", name)
    pulse = threading.Thread(target=heartbeat, daemon=True)
    pulse.start()
    try:
        with session_factory() as db:
            summary = fn(db) or {}
            # Publish commits each batch on its own connection. This session
            # then sits idle until the server closes it. Losing that idle
            # socket must not throw away batches that are already saved.
            try:
                db.commit()
            except Exception:
                db.rollback()
                if not summary.get("persisted"):
                    raise
        stop.set()
        pulse.join(timeout=3)
        summary = {**summary, "duration_seconds": round((datetime.now(UTC)-started).total_seconds(), 3), "heartbeat_at": datetime.now(UTC).isoformat()}
        bad = summary.get("status") in {
            "needs_key",
            "error",
            "failed",
            "budget_stop",
            "plan_blocked",
            "degraded",
            "unavailable",
            "requires_export_job",
        }
        with session_factory() as db:
            record = db.get(JobRun, run_id)
            record.status = "degraded" if bad else "ok"
            record.finished_at = datetime.now(UTC)
            record.summary = summary
            db.commit()
        if bad:
            raise RuntimeError(f"{name}: {summary.get('status')}")
        return summary
    except Exception as exc:
        with session_factory() as db:
            record = db.get(JobRun, run_id)
            if record.status != "degraded":
                record.status = "failed"
            record.finished_at = datetime.now(UTC)
            record.error = type(exc).__name__
            import traceback
            from pathlib import Path
            frames = [f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in traceback.extract_tb(exc.__traceback__)[-8:]]
            record.summary = {**(record.summary or {}), "failure_frames": frames,
                "duration_seconds": round((datetime.now(UTC)-started).total_seconds(), 3)}
            db.commit()
        logging.getLogger(__name__).error("Job %s failed: %s (run %s) at %s", name, type(exc).__name__, run_id, frames)
        raise
    finally:
        stop.set()
        pulse.join(timeout=3)
        from spe.ops.memory import release_processing_memory
        release_processing_memory()


def _board_game_ids(games, now, *, upcoming_only=False):
    """Games still on the homepage come first. A dropped run must leave those, not only games that already kicked off."""
    dead = {"final", "in_progress", "canceled", "cancelled", "postponed"}
    future, rest = [], []
    for game in games:
        if getattr(game, "status", None) in dead:
            continue
        kick = as_utc(getattr(game, "kickoff_utc", None))
        if kick and kick > now:
            future.append(game)
        elif not upcoming_only:
            rest.append(game)
    future.sort(key=lambda game: as_utc(game.kickoff_utc))
    ordered = future if upcoming_only else [*future, *rest]
    return [game.game_id for game in ordered]


def _save_batch(market, date, chunk, prior, signature):
    """Score one handful of games on a new connection. Retry once if that socket dies."""
    from sqlalchemy.exc import OperationalError
    from app.queries import list_props
    from spe.props.board_cache import write_entry
    from spe.publishing.ledger import publish
    from spe.shadow.desk_ledger import freeze_desk_rows
    import time

    for attempt in (1, 2):
        try:
            with session_factory() as batch:
                rows = list_props(batch, market, None, None, "all", date, game_ids=chunk or None)
                freeze_desk_rows(batch, rows)
                published = publish(batch, rows)["published"]
                collected = [*prior, *rows]
                for league in ("all", "nfl", "fbs"):
                    write_entry(
                        market,
                        league,
                        date,
                        [r for r in collected if league == "all" or r["league_id"] == league],
                        session=batch,
                        scoring_signature=signature,
                    )
                batch.commit()
                return rows, published
        except OperationalError as exc:
            print(f"publish_slate {date} {market} batch attempt {attempt} {type(exc).__name__}", flush=True)
            time.sleep(2)
    return None, 0


def publish_slate(db, date, *, upcoming_only=False):
    """Score a few games at a time and save. A dropped database connection cannot erase them."""
    from app.queries import games_for_slate
    from spe.props.board_cache import WARM_MARKETS
    from spe.publishing.local_board import scoring_signature

    signature = scoring_signature()
    now = datetime.now(UTC)
    game_ids = _board_game_ids(games_for_slate(db, date, "all"), now, upcoming_only=upcoming_only)
    db.commit()
    saved = 0
    published = 0
    failed = 0
    batch = 6
    for market in WARM_MARKETS:
        collected: list = []
        print(f"publish_slate {date} {market} start games={len(game_ids)}", flush=True)
        chunks = [game_ids[i:i + batch] for i in range(0, len(game_ids), batch)] or [[]]
        for index, chunk in enumerate(chunks, start=1):
            if chunk == [] and game_ids:
                continue
            rows, count = _save_batch(market, date, chunk, collected, signature)
            if rows is None:
                failed += 1
                print(f"publish_slate {date} {market} batch {index}/{len(chunks)} skipped", flush=True)
                continue
            collected.extend(rows)
            published += count
            print(f"publish_slate {date} {market} batch {index}/{len(chunks)} saved {len(rows)}", flush=True)
        saved += len(collected)
    return {"rows": saved, "published": published, "failed_batches": failed}


def warm_and_publish(db, only_dates=None):
    from app.queries import games_for_slate, kickoff_day

    games = games_for_slate(db, None, "all")
    horizon = datetime.now(UTC) + timedelta(hours=48)
    dates = sorted({kickoff_day(g.kickoff_utc) for g in games if as_utc(g.kickoff_utc) <= horizon})
    if only_dates is not None:
        wanted = set(only_dates)
        dates = [day for day in dates if day in wanted]
    # The page opens on today. Save that slate before later days or a timeout.
    today = kickoff_day(datetime.now(UTC))
    if today in dates:
        dates = [today, *[day for day in dates if day != today]]
    published = 0
    for date in dates:
        published += publish_slate(db, date)["published"]
        try:
            db.commit()
        except Exception:
            db.rollback()
    return {"status": "ok", "dates": dates, "published": published, "persisted": True}


def publish_today(db):
    """Write the games still on the homepage before any slow download starts."""
    from app.queries import kickoff_day

    day = kickoff_day(datetime.now(UTC))
    result = publish_slate(db, day, upcoming_only=True)
    status = "degraded" if result["failed_batches"] and not result["rows"] else "ok"
    return {"status": status, "dates": [day], "persisted": True, **result}


def inputs(db):
    from spe.ingest.espn_availability import ingest_espn_availability
    from spe.ingest.nfl_availability import refresh_nfl_availability
    from spe.ingest.api_sports_injuries import ingest_api_sports_injuries
    from spe.config import settings

    moment = datetime.now(UTC)
    year = moment.year - (1 if moment.month <= 2 else 0)
    def attempt(fn):
        try:
            # These ingesters checkpoint internally. An enclosing savepoint is
            # invalid after their first commit and used to fail ESPN availability.
            result = fn()
            db.commit()
            return result
        except Exception as exc:
            db.rollback()
            return {"status": "unavailable", "error": type(exc).__name__}

    nfl = attempt(lambda: refresh_nfl_availability(db))
    availability = attempt(lambda: ingest_espn_availability(db))
    # This optional paid endpoint must not make successful free-source ingestion fail.
    # Missing coverage stays explicit and never turns into a healthy-player designation.
    fbs = attempt(lambda: ingest_api_sports_injuries(db, league_id="fbs", season=year))
    bad = {"needs_key", "plan_blocked", "failed", "error", "unavailable", "degraded"}
    blocked = any(r.get("status") in bad for r in (nfl, availability))
    return {
        "status": "degraded" if blocked else "ok",
        "nfl": nfl,
        "availability": availability,
        "fbs": fbs,
        "warnings": ["Optional FBS injury coverage is unavailable; player status remains unverified."] if fbs.get("status") in bad else [],
    }


def refresh_touchdown_cards(db):
    """Scheduled model + simulation run; replay_day retains every started selection."""
    from app.queries import games_for_slate, kickoff_day
    from spe.models.player_td.desk_replay import replay_day
    from spe.ingest.shared_inputs import hydrate_inputs
    import time
    versions = hydrate_inputs(db)
    deadline = time.monotonic() + 210
    now = datetime.now(UTC)
    dates = sorted({kickoff_day(g.kickoff_utc) for g in games_for_slate(db, None, "all")
                    if now < as_utc(g.kickoff_utc) <= now + timedelta(hours=48)})
    counts = {}
    for day in dates:
        card = replay_day(db, day, "all", checkpoint=True, deadline=deadline, input_versions=versions)
        counts[day] = {"picks": len(card.get("rows") or []),
                       "unavailable": len(card.get("unavailable_games") or []),
                       "simulation_unavailable": sum(((r.get('desk_analysis') or {}).get('simulation') or {}).get('status') == 'unavailable' for r in card.get('rows', [])),
                       "complete": card.get("refresh_complete", True)}
        db.commit()
    return {"status": "degraded" if any(v["unavailable"] or v['simulation_unavailable'] or not v["complete"] for v in counts.values()) else "ok",
            "dates": counts, "input_versions": versions}


def refresh_saved_results(db):
    from spe.db.models import Game
    from app.queries import kickoff_day
    from spe.models.player_td.desk_replay_results import refresh_results
    from spe.props.game_picks import saved_desk_props
    from spe.desk.day_results import apply_recorded_results
    from spe.ingest.espn_box import _fetch_summary
    now = datetime.now(UTC)
    # Recent grades and pregame market context share the bounded scoreboard feed.
    from zoneinfo import ZoneInfo
    today = now.astimezone(ZoneInfo("America/Chicago")).date()
    # Ten days back covers a finished weekend that the two-day window had already skipped.
    dates = [(today - timedelta(days=n)).isoformat() for n in range(10, -3, -1)]
    results = []
    for day in dates:
        try:
            result = refresh_results(db, day, cards_only=True)
            rows = saved_desk_props(db, "all", day) or []
            apply_recorded_results(db, [], rows, fetch=_fetch_summary)
            db.commit()
            results.append(result)
        except Exception as exc:
            db.rollback()
            results.append({"date": day, "status": "degraded", "error": type(exc).__name__})
    return results


def settlement(db):
    from spe.ingest.espn_scoreboard import ingest_football_scoreboard
    from spe.ingest.espn_box import ingest_final_box_scores
    from spe.publishing.ledger import settle, record_frozen_desk_picks
    from spe.publishing.boxes import ingest as ingest_published_boxes
    from spe.shadow.ats import settle_ats_predictions
    from spe.shadow.settle import settle_shadow_predictions

    saved_results = refresh_saved_results(db)
    scores = {"games": sum(r.get("scoreboard_games", 0) for r in saved_results), "scope": "recent_slate_days"}
    boxes = [ingest_final_box_scores(db, league_id=lg) for lg in ("nfl", "fbs")]
    db.flush()
    published_boxes = ingest_published_boxes(db)
    record_frozen_desk_picks(db)
    result = settle(db)
    if published_boxes["status"] == "degraded" or any(r.get("status") == "degraded" for r in [*saved_results, *boxes]):
        result["status"] = "degraded"
    return {
        **result,
        "scores": scores,
        "boxes": boxes,
        "published_boxes": published_boxes,
        "saved_results": saved_results,
        "ats": settle_ats_predictions(db),
        "shadow": settle_shadow_predictions(db),
    }


def production_cycle(lane):
    if lane not in {"odds", "inputs", "settlement", "touchdowns"}:
        raise ValueError("Unknown production lane")
    init_db()
    with lane_lock(lane) as acquired:
        if not acquired:
            return {"status": "already_running", "lane": lane}
        names = {
            "odds": ["production_publish", "production_pregame_availability", "production_odds", "production_spreads", "production_ats"],
            "inputs": ["production_inputs", "production_weather", "production_nflverse", "production_free_college"],
            "settlement": ["production_settlement"],
            "touchdowns": ["production_touchdowns"],
        }[lane]
        # Holding the lane lock proves any previous running record lost its worker.
        with session_factory() as db:
            db.query(JobRun).filter(JobRun.job_name.in_(names), JobRun.status == "running").update(
                {"status": "interrupted", "finished_at": datetime.now(UTC), "error": "WorkerInterrupted"}, synchronize_session=False)
            db.commit()
        if lane == "odds":
            from spe.ingest.odds_live import ingest_odds_live
            from spe.ingest.odds_spreads import ingest_odds_spreads
            from spe.models.ats.board_cache import refresh_board
            from spe.shadow.ats import capture_ats_predictions

            # Save the scoreboard number before the first ATS pass. The paid
            # feed can refuse a near-kickoff game, and that pass still needs a line.
            try:
                from spe.ingest.espn_spreads import store_public_spreads

                with session_factory() as spread_db:
                    store_public_spreads(spread_db)
                    spread_db.commit()
            except Exception:
                import logging

                logging.getLogger(__name__).warning("Public scoreboard spreads were not saved", exc_info=True)
            # Save the board from prices already stored before downloading more.
            # The cron kills this lane at its time limit. A download used to run
            # first, so the publish step was killed and the homepage stayed empty.
            results = []
            errors = []
            from spe.ingest.public_availability import refresh_public_availability
            steps = {
                "production_publish": publish_today,
                "production_pregame_availability": refresh_public_availability,
                "production_odds": ingest_odds_live,
                "production_spreads": lambda db: ingest_odds_spreads(db, max_events=12),
                "production_ats": lambda db: (refresh_board(db), capture_ats_predictions(db))[1],
            }
            publish_passes = 0
            for name in ODDS_LANE:
                fn = warm_and_publish if name == "production_publish" and publish_passes else steps[name]
                if name == "production_publish":
                    publish_passes += 1
                try:
                    results.append(run_step(name, fn))
                except Exception as exc:
                    errors.append(f"{name}: {type(exc).__name__}")
            if errors:
                raise RuntimeError("; ".join(errors))
            return {"status": "ok", "steps": results}
        if lane == "inputs":
            from spe.config import settings
            errors = []
            for name in names:
                try:
                    if settings.app_env in {"prod", "production"}:
                        # Keep the advisory lane lock in this lean parent. A fresh
                        # child releases each provider's native parsing buffers and
                        # imports before the next feed. The supervisor's process
                        # group still owns and terminates every child on timeout.
                        import subprocess
                        import sys
                        subprocess.run([sys.executable, "-m", "worker.input_step", name], check=True)
                    else:
                        run_step(name, input_operation(name))
                except Exception as exc:
                    errors.append(f"{name}: {type(exc).__name__}")
            if errors:
                raise RuntimeError("; ".join(errors))
            return {"status": "ok"}
        if lane == "touchdowns":
            return run_step("production_touchdowns", refresh_touchdown_cards)
        return run_step("production_settlement", settlement)


def input_operation(name):
    """Import only the provider requested by an isolated production input step."""
    if name == "production_inputs":
        return inputs
    if name == "production_weather":
        from spe.ingest.nws_live import ingest_nws_forecasts
        return lambda db: ingest_nws_forecasts(db, horizon_hours=48)
    if name == "production_nflverse":
        from spe.ingest.nflverse_desk import refresh_nflverse_desk
        return refresh_nflverse_desk
    if name == "production_free_college":
        from spe.ingest.free_desk_inputs import refresh_free_desk_inputs
        return refresh_free_desk_inputs
    raise ValueError("Unknown input step")


def health(db, now=None):
    now = as_utc(now) or datetime.now(UTC)
    db.execute(text("SELECT 1"))
    jobs = []
    for name, ceiling in JOBS.items():
        row = db.query(JobRun).filter_by(job_name=name).order_by(JobRun.started_at.desc()).first()
        last_ok = (
            db.query(JobRun)
            .filter_by(job_name=name, status="ok")
            .order_by(JobRun.finished_at.desc())
            .first()
        )
        age = (
            (now - as_utc(last_ok.finished_at)).total_seconds()
            if last_ok and last_ok.finished_at
            else None
        )
        running_age = (now - (as_utc((row.summary or {}).get("heartbeat_at")) or as_utc(row.started_at))).total_seconds() if row and row.status == "running" else None
        ok = age is not None and age <= ceiling and row.status in {"ok", "running"} and (running_age is None or running_age < 120)
        jobs.append(
            {
                "job": name,
                "ok": ok,
                "status": row.status if row else "never_run",
                "seconds_since_success": age,
                "max_age_seconds": ceiling,
                "heartbeat_age_seconds": running_age,
                "duration_seconds": (row.summary or {}).get("duration_seconds") if row else None,
                "last_error": row.error if row else None,
                "last_finished_at": as_utc(row.finished_at).isoformat() if row and row.finished_at else None,
                "warnings": (row.summary or {}).get("warnings", []) if row else [],
            }
        )
    alerts = [f"{j['job']}: {j['status']} or overdue" for j in jobs if not j["ok"]]
    from sqlalchemy import func
    from spe.db.models import Game, Outcome, OddsTick
    from spe.config import settings

    near = (
        db.query(Game.game_id, func.max(OddsTick.observed_at))
        .outerjoin(Outcome, Outcome.game_id == Game.game_id)
        .outerjoin(OddsTick, OddsTick.outcome_id == Outcome.outcome_id)
        .filter(
            Game.kickoff_utc > now,
            Game.kickoff_utc <= now + timedelta(hours=2),
            Game.status.in_(("scheduled", "pre", "upcoming")),
            Game.data_tier.notin_(("demo", "alias")),
        )
        .group_by(Game.game_id)
        .all()
    )
    stale_games = [
        gid
        for gid, observed in near
        if not observed
        or (now - as_utc(observed)).total_seconds() > settings.odds_near_fresh_minutes * 60
    ]
    if stale_games:
        alerts.append(
            f"Near-kickoff price verification overdue for {len(stale_games)} tracked games"
        )

    from spe.db.models import BoardSnapshot
    from app.queries import kickoff_day
    dates = {kickoff_day(now), kickoff_day(now+timedelta(hours=2))}
    cards = db.query(BoardSnapshot).filter(BoardSnapshot.key.in_(
        [f"atd-desk-card-v1:{day}:{league}" for day in dates for league in ("all", "nfl", "fbs")])).all()
    covered = {r.get("game_id") for card in cards for r in (card.payload or {}).get("rows", [])}
    explained = {r.get("game_id") for card in cards for r in (card.payload or {}).get("unavailable_games", [])}
    unexplained = sorted(gid for gid, _ in near if gid not in covered | explained)
    if unexplained:
        alerts.append(f"TD card coverage missing without explanation for {len(unexplained)} near-kickoff games")
    from spe.db.models import InputArtifact
    college_soon = db.query(Game.game_id).filter(Game.league_id == 'fbs',
        Game.kickoff_utc > now, Game.kickoff_utc <= now+timedelta(hours=48),
        Game.data_tier.notin_(('demo', 'alias')), Game.status.notin_(('final', 'post', 'canceled', 'cancelled'))).first() is not None
    artifact_health = []
    for name in ('fbs.json', 'fbs_rosters.json', 'recent_complete_boxes.json'):
        record = db.query(InputArtifact.updated_at, InputArtifact.byte_count).filter(InputArtifact.key == name).first()
        age = (now-as_utc(record.updated_at)).total_seconds() if record else None
        usable = bool(record and record.byte_count > 2 and age is not None and 0 <= age <= 8*3600)
        artifact_health.append({'input':name, 'required':college_soon, 'ok':usable,
            'seconds_since_refresh':age, 'max_age_seconds':8*3600})
        if college_soon and not usable:
            alerts.append(f'Shared college input missing or overdue: {name}')
    return {"ok": not alerts, "jobs": jobs, "alerts": alerts, "checked_at": now.isoformat(),
            "shared_inputs": artifact_health,
            "coverage": {"near_kickoff_games": len(near), "games_with_fresh_odds": len(near)-len(stale_games),
                         "games_missing_fresh_odds": stale_games, "games_missing_td_explanation": unexplained}}


def job_status(db, name, now=None):
    now = as_utc(now) or datetime.now(UTC)
    row = db.query(JobRun).filter_by(job_name=name).order_by(JobRun.started_at.desc()).first()
    if row is None:
        return "not_started"
    stamp = as_utc((row.summary or {}).get("heartbeat_at")) or as_utc(row.finished_at) or as_utc(row.started_at)
    if row.status == "running":
        return "running" if (now-stamp).total_seconds() < 120 else "stalled"
    if row.status not in {"ok"}:
        return "failed"
    return "ready" if (now-stamp).total_seconds() <= JOBS.get(name, 900) else "stale"
