"""Restart-safe shadow jobs. Failures are recorded. Play stays off."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy.orm import Session

from spe.db.base import new_id
from spe.db.models import JobRun
from spe.shadow.capture import capture_shadow_predictions
from spe.shadow.report import shadow_track_record
from spe.shadow.settle import settle_shadow_predictions


def _start(session: Session, job_name: str) -> JobRun:
    run = JobRun(run_id=new_id(), job_name=job_name, started_at=datetime.now(UTC), status="running", summary={})
    session.add(run)
    session.flush()
    return run


def _finish(session: Session, run: JobRun, summary: dict[str, Any], *, error: str | None = None) -> dict[str, Any]:
    run.finished_at = datetime.now(UTC)
    run.status = "failed" if error else "ok"
    run.summary = summary
    run.error = error
    session.flush()
    return {"run_id": run.run_id, "job_name": run.job_name, "status": run.status, "summary": summary, "error": error}


def run_job(session: Session, job_name: str, fn: Callable[[Session], dict[str, Any]]) -> dict[str, Any]:
    run = _start(session, job_name)
    try:
        summary = fn(session)
        return _finish(session, run, summary)
    except Exception as exc:  # noqa: BLE001 - job ledger must record the failure
        session.rollback()
        failed = JobRun(
            run_id=new_id(),
            job_name=job_name,
            started_at=run.started_at,
            finished_at=datetime.now(UTC),
            status="failed",
            summary={"play_enabled": False},
            error=str(exc),
        )
        session.add(failed)
        session.flush()
        raise


def job_capture(session: Session) -> dict[str, Any]:
    return run_job(session, "shadow_capture", capture_shadow_predictions)


def job_settle(session: Session) -> dict[str, Any]:
    return run_job(session, "shadow_settle", settle_shadow_predictions)


def job_report(session: Session) -> dict[str, Any]:
    def _report(s: Session) -> dict[str, Any]:
        track = shadow_track_record(s)
        return {
            "play_enabled": False,
            "totals": track["totals"],
            "abstain_reason_counts": track["abstain_reason_counts"],
            "markets_scored": len(track["by_market"]),
            "jobs_recorded": len(track["jobs"]),
        }

    return run_job(session, "shadow_report", _report)


def job_atd_candidates(session: Session) -> dict[str, Any]:
    """Freeze M3/M5/M6/atd_sim_v1 Anytime TD candidates beside live prices."""
    from spe.simulate.correlated_atd.shadow_candidates import capture_all_atd_candidates

    return run_job(session, "atd_candidate_capture", capture_all_atd_candidates)


def run_shadow_cycle(session: Session, *, refresh_odds: bool = True, publish: bool = True) -> dict[str, Any]:
    """Odds → model forecasts → M3/M4 capture → ATD candidates → settle → report.

    Restart-safe. Does not enable Validated Play. Does not rewrite frozen rows.
    """
    out: dict[str, Any] = {"play_enabled": False, "research_only": True, "validated_play": False, "steps": []}
    if refresh_odds:
        from spe.ingest.odds_live import ingest_odds_live

        out["steps"].append(run_job(session, "odds_refresh", lambda s: ingest_odds_live(s)))
        session.commit()
    if publish:
        from spe.ingest.model_forecasts import publish_model_forecasts

        out["steps"].append(run_job(session, "pregame_forecasts", publish_model_forecasts))
        session.commit()
    out["steps"].append(job_capture(session))
    session.commit()
    # Anytime TD research candidates (M3/M5/M6/sim) — append-only beside sportsbook ticks.
    out["steps"].append(job_atd_candidates(session))
    session.commit()
    out["steps"].append(job_settle(session))
    session.commit()
    out["steps"].append(job_report(session))
    session.commit()
    out["track_record"] = shadow_track_record(session)
    out["integrity"] = audit_cycle_data_quality(session, out["steps"])
    out["alerts"] = out["integrity"].get("alerts") or []
    return out


def audit_cycle_data_quality(session: Session, steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Distinguish job process success from producing the required live data."""
    from spe.ops.freshness import operations_health

    health = operations_health(session)
    alerts: list[str] = []
    by_job = {step.get("job_name"): step for step in steps if isinstance(step, dict)}

    odds = by_job.get("odds_refresh") or {}
    odds_summary = odds.get("summary") or {}
    odds_inner = odds_summary.get("status") or odds.get("status")
    if odds_inner == "budget_stop" or odds_summary.get("budget_stop"):
        alerts.append("odds_refresh_budget_stop: soft credit budget blocked refreshes")
    if odds.get("status") == "ok" and int(odds_summary.get("ticks_upserted") or 0) == 0:
        considered = int(odds_summary.get("events_fetched") or 0) + int(odds_summary.get("events_skipped") or 0)
        if considered > 0:
            alerts.append("odds_refresh_empty: considered upcoming events but upserted zero ticks")

    forecasts = by_job.get("pregame_forecasts") or {}
    fc_summary = forecasts.get("summary") or {}
    if forecasts.get("status") == "ok" and int(fc_summary.get("written") or fc_summary.get("published") or 0) == 0:
        if fc_summary.get("status") != "no_games":
            alerts.append("pregame_forecasts_empty: job ok but published zero forecasts")

    odds_health = health.get("odds_freshness") or {}
    if float(odds_health.get("stale_share") or 0) >= 0.5 and int(odds_health.get("price_rows") or 0) > 0:
        alerts.append(
            f"odds_stale_majority: {odds_health.get('stale_rows')}/{odds_health.get('price_rows')} prices older than max age"
        )
    if int(odds_health.get("near_kickoff_stale_rows") or 0) > 0:
        alerts.append(f"near_kickoff_stale: {odds_health['near_kickoff_stale_rows']} near-kickoff price rows are stale")

    injury = health.get("injury_coverage") or {}
    api_inj = injury.get("api_sports_injury_ingest") or {}
    if str(api_inj.get("status") or "").lower() in {"plan_blocked", "blocked", "error"}:
        alerts.append(f"fbs_injuries_blocked: {api_inj.get('notes') or api_inj.get('status')}")

    job_exec = health.get("job_execution") or {}
    if job_exec.get("alerts_visible"):
        alerts.append("job_execution_alerts_visible: recent failures or missing expected jobs")

    credits = health.get("credits") or {}
    remaining = credits.get("odds_api_remaining_budget")
    if remaining is not None and int(remaining) <= 0:
        alerts.append("odds_api_budget_exhausted: remaining soft budget is zero")

    return {
        "alerts": alerts,
        "data_ok": len(alerts) == 0,
        "operations_health": {
            "odds_freshness": {
                "stale_share": odds_health.get("stale_share"),
                "stale_rows": odds_health.get("stale_rows"),
                "price_rows": odds_health.get("price_rows"),
                "near_kickoff_stale_rows": odds_health.get("near_kickoff_stale_rows"),
            },
            "credits": credits,
            "job_execution_alerts_visible": job_exec.get("alerts_visible"),
        },
        "play_enabled": False,
    }
