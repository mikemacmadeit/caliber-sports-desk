"""Free input refresh with durable shared artifacts and explicit partial failures."""
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from spe.config import settings
from spe.db.base import as_utc
from spe.ingest.shared_inputs import hydrate_inputs, publish_inputs

BAD = {"error", "failed", "unavailable", "degraded", "requires_export_job", "needs_key", "plan_blocked"}


def failed(body):
    return isinstance(body, dict) and (body.get('status') in BAD or bool(body.get('errors'))
        or bool(body.get('failed_games')) or any(failed(v) for v in body.values() if isinstance(v, dict)))


def refresh_free_desk_inputs(db, now=None):
    from spe.ingest.free_college import refresh_free_college
    from spe.ingest.public_availability import refresh_public_availability
    from spe.ingest.desk_evidence import refresh_local_evidence
    from spe.ingest.college_situations import refresh_college_situations
    from spe.ingest.college_rosters import refresh_college_rosters
    from spe.ingest.recent_td_boxes import refresh_recent_boxes

    now = as_utc(now) or datetime.now(UTC)
    versions = hydrate_inputs(db)
    result = {}

    def attempt(name, operation, artifacts=()):
        try:
            with db.begin_nested():
                body = operation() or {}
                if failed(body):
                    result[name] = {**body, "status": "degraded"}
                    return False
                published = publish_inputs(db, artifacts, now=now)
                if set(published['published']) != set(artifacts):
                    raise ValueError('Required input artifact was not produced')
                result[name] = {**body, "status": body.get("status", "ok")}
            db.commit()
            return True
        except Exception as exc:
            db.rollback()
            result[name] = {"status": "unavailable", "reason": type(exc).__name__}
            return False

    path = settings.parquet_dir / "desk_evidence" / "fbs.json"
    if path.name in versions and path.exists() and now.timestamp() - path.stat().st_mtime < 21600:
        result["college"] = {"status": "fresh"}
    else:
        year = now.year - int(now.month < 7)
        def college():
            sources = refresh_free_college(db, years=(year,), refresh=True)
            situations = refresh_college_situations(db)
            derived = refresh_local_evidence(force=True, db=db)
            return {"sources": sources, "situations": situations, "derived": derived}
        attempt("college", college, ("fbs_free_opportunities.json", "fbs_situations.json", "fbs.json", "nfl.json"))
    attempt("rosters", lambda: refresh_college_rosters(db, now=now), ("fbs_rosters.json",))
    attempt("availability", lambda: refresh_public_availability(db, now=now))
    path = settings.parquet_dir / "desk_evidence" / "recent_complete_boxes.json"
    if path.name in versions and path.exists() and now.timestamp() - path.stat().st_mtime < 21600:
        result["recent_boxes"] = {"status": "fresh"}
    else:
        attempt("recent_boxes", lambda: refresh_recent_boxes(db, now.astimezone(ZoneInfo("America/Chicago")).date().isoformat()), ("recent_complete_boxes.json",))
    bad = [name for name, body in result.items() if body.get("status") in BAD]
    return {"status": "degraded" if bad else "ok", "sources": result,
            "warnings": [f"{name}: unavailable or incomplete; previous inputs retained" for name in bad]}
