"""Durable scheduled jobs. Failures survive rollback; concurrent lanes are locked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from contextlib import contextmanager
from sqlalchemy import text
from spe.db.base import as_utc, new_id
from spe.db.models import JobRun
from spe.db.session import session_factory, get_engine, init_db

# Publish before the download. The homepage reads the saved board, and the
# cron used to die while still fetching prices, so that board never got written.
ODDS_LANE = (
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
    if get_engine().dialect.name == "postgresql":
        with get_engine().connect() as conn:
            key = {"odds": 81301, "inputs": 81302, "settlement": 81303, "touchdowns": 81304}[lane]
            locked = conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar()
            try:
                yield bool(locked)
            finally:
                if locked:
                    conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
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
            db.commit()
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


def publish_slate(db, date):
    """Save probabilities and pregame decisions in one transaction, on one connection."""
    from app.queries import list_props
    from spe.props.board_cache import write_entry, WARM_MARKETS
    from spe.publishing.ledger import publish
    from spe.shadow.desk_ledger import freeze_desk_rows
    from spe.publishing.local_board import scoring_signature

    all_rows = list_props(db, None, None, None, "all", date)
    freeze_desk_rows(db, all_rows)
    signature = scoring_signature()
    for market in WARM_MARKETS:
        rows = [r for r in all_rows if r["market_id"] == market]
        for league in ("all", "nfl", "fbs"):
            write_entry(
                market,
                league,
                date,
                [r for r in rows if league == "all" or r["league_id"] == league],
                session=db,
                scoring_signature=signature,
            )
    return {"rows": len(all_rows), "published": publish(db, all_rows)["published"]}


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
        db.commit()
    return {"status": "ok", "dates": dates, "published": published}


def publish_today(db):
    """Write the slate the homepage is showing before any slow download starts."""
    from app.queries import kickoff_day

    return warm_and_publish(db, only_dates=[kickoff_day(datetime.now(UTC))])


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
    dates = [(today-timedelta(days=n)).isoformat() for n in (2, 1, 0, -1, -2)]
    results = []
    for day in dates:
        try:
            result = refresh_results(db, day)
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
    scores = {"games": sum(r.get("scoreboard_games", 0) for r in saved_results), "scope": "past_two_and_next_two_slate_days"}
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
