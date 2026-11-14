"""Durable scheduled jobs. Failures survive rollback; concurrent lanes are locked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from contextlib import contextmanager
from sqlalchemy import text
from spe.db.base import as_utc, new_id
from spe.db.models import JobRun
from spe.db.session import session_factory, get_engine, init_db

JOBS = {
    "production_odds": 900,
    "production_publish": 900,
    "production_inputs": 2400,
    "production_weather": 10800,
    "production_settlement": 2400,
}


@contextmanager
def lane_lock(lane):
    # Session-level advisory locks remain held across commits and release on crash.
    if get_engine().dialect.name == "postgresql":
        with get_engine().connect() as conn:
            key = {"odds": 81301, "inputs": 81302, "settlement": 81303}[lane]
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
        with lock_path.open("w") as file:
            try:
                if os.name == "nt":
                    import msvcrt

                    file.write("0")
                    file.flush()
                    file.seek(0)
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
            else:
                try:
                    yield True
                finally:
                    if os.name == "nt":
                        file.seek(0)
                        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(file, fcntl.LOCK_UN)


def run_step(name, fn):
    started = datetime.now(UTC)
    run_id = new_id()
    with session_factory() as db:
        db.add(
            JobRun(run_id=run_id, job_name=name, started_at=started, status="running", summary={})
        )
        db.commit()
    try:
        with session_factory() as db:
            summary = fn(db) or {}
            db.commit()
        bad = summary.get("status") in {
            "needs_key",
            "error",
            "failed",
            "budget_stop",
            "plan_blocked",
            "degraded",
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
            record.status = "failed"
            record.finished_at = datetime.now(UTC)
            record.error = type(exc).__name__
            db.commit()
        raise


def warm_and_publish(db):
    from app.queries import games_for_slate, kickoff_day, list_props
    from spe.props.board_cache import write_entry, WARM_MARKETS
    from spe.publishing.ledger import publish

    games = games_for_slate(db, None, "all")
    horizon = datetime.now(UTC) + timedelta(hours=48)
    dates = sorted({kickoff_day(g.kickoff_utc) for g in games if as_utc(g.kickoff_utc) <= horizon})
    published = 0
    for date in dates:
        all_rows = list_props(db, None, None, None, "all", date)
        for market in WARM_MARKETS:
            rows = [r for r in all_rows if r["market_id"] == market]
            for league in ("all", "nfl", "fbs"):
                write_entry(
                    market,
                    league,
                    date,
                    [r for r in rows if league == "all" or r["league_id"] == league],
                )
        published += publish(db, all_rows)["published"]
        db.commit()
    return {"status": "ok", "dates": dates, "published": published}


def inputs(db):
    from spe.ingest.espn_availability import ingest_espn_availability
    from spe.ingest.nflverse_historical import ingest_nfl_history
    from spe.ingest.api_sports_injuries import ingest_api_sports_injuries

    moment = datetime.now(UTC)
    year = moment.year - (1 if moment.month <= 2 else 0)
    nfl = ingest_nfl_history(
        db, seasons=[year], include_pbp=False, refresh=True, injuries_only=True
    )
    availability = ingest_espn_availability(db)
    fbs = ingest_api_sports_injuries(db, league_id="fbs", season=year)
    blocked = any(
        r.get("status") in {"needs_key", "plan_blocked", "failed", "error"}
        for r in (nfl, availability, fbs)
    )
    return {
        "status": "degraded" if blocked else "ok",
        "nfl": nfl,
        "availability": availability,
        "fbs": fbs,
    }


def settlement(db):
    from spe.ingest.espn_scoreboard import ingest_football_scoreboard
    from spe.ingest.espn_box import ingest_final_box_scores
    from spe.publishing.ledger import settle
    from spe.publishing.boxes import ingest as ingest_published_boxes
    from spe.shadow.ats import settle_ats_predictions
    from spe.shadow.settle import settle_shadow_predictions

    scores = ingest_football_scoreboard(db, datetime.now(UTC).year)
    boxes = [ingest_final_box_scores(db, league_id=lg) for lg in ("nfl", "fbs")]
    db.flush()
    published_boxes = ingest_published_boxes(db)
    result = settle(db)
    if published_boxes["status"] == "degraded":
        result["status"] = "degraded"
    return {
        **result,
        "scores": scores,
        "boxes": boxes,
        "published_boxes": published_boxes,
        "ats": settle_ats_predictions(db),
        "shadow": settle_shadow_predictions(db),
    }


def production_cycle(lane):
    init_db()
    with lane_lock(lane) as acquired:
        if not acquired:
            return {"status": "already_running", "lane": lane}
        if lane == "odds":
            from spe.ingest.odds_live import ingest_odds_live
            from spe.ingest.odds_spreads import ingest_odds_spreads
            from spe.models.ats.board_cache import refresh_board
            from spe.shadow.ats import capture_ats_predictions

            # Shared credit ceiling is retained. Each committed stage is independently retryable.
            results = []
            errors = []
            for name, fn in [
                ("production_odds", ingest_odds_live),
                ("production_spreads", lambda db: ingest_odds_spreads(db, max_events=12)),
                ("production_publish", warm_and_publish),
                ("production_ats", lambda db: (refresh_board(db), capture_ats_predictions(db))[1]),
            ]:
                try:
                    results.append(run_step(name, fn))
                except Exception as exc:
                    errors.append(f"{name}: {type(exc).__name__}")
            if errors:
                raise RuntimeError("; ".join(errors))
            return {"status": "ok", "steps": results}
        if lane == "inputs":
            from spe.ingest.nws_live import ingest_nws_forecasts

            errors = []
            for name, fn in [
                ("production_inputs", inputs),
                ("production_weather", lambda db: ingest_nws_forecasts(db, horizon_hours=48)),
            ]:
                try:
                    run_step(name, fn)
                except Exception as exc:
                    errors.append(f"{name}: {type(exc).__name__}")
            if errors:
                raise RuntimeError("; ".join(errors))
            return {"status": "ok"}
        return run_step("production_settlement", settlement)


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
        ok = age is not None and age <= ceiling and row.status in {"ok", "running"}
        jobs.append(
            {
                "job": name,
                "ok": ok,
                "status": row.status if row else "never_run",
                "seconds_since_success": age,
            }
        )
    alerts = [f"{j['job']}: {j['status']} or overdue" for j in jobs if not j["ok"]]
    from sqlalchemy import func
    from spe.db.models import Game, Outcome, OddsTick
    from spe.config import settings

    near = (
        db.query(Game.game_id, func.max(OddsTick.observed_at))
        .join(Outcome, Outcome.game_id == Game.game_id)
        .outerjoin(OddsTick, OddsTick.outcome_id == Outcome.outcome_id)
        .filter(
            Game.kickoff_utc > now,
            Game.kickoff_utc <= now + timedelta(hours=2),
            Game.status.in_(("scheduled", "pre", "upcoming")),
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

    return {"ok": not alerts, "jobs": jobs, "alerts": alerts, "checked_at": now.isoformat()}
