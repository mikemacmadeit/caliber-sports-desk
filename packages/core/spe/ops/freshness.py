"""Injury coverage and odds freshness health for live operations."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.config import settings
from spe.db.base import as_utc
from spe.db.models import Game, InjuryEvent, JobRun, ShadowPrediction
from spe.ingest.snapshots import remaining_credits
from spe.market.price_age import max_fresh_minutes
from spe.ops.availability import NFL_REPORT_LEAD_HOURS, availability_for_player, games_with_inactive_list


def operations_health(session: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    from spe.ingest.odds_budget import budget_status

    return {
        "injury_coverage": injury_coverage_health(session, now),
        "odds_freshness": odds_freshness_health(session, now),
        "job_execution": job_execution_health(session, now),
        "credits": {
            "odds_api_remaining_budget": remaining_credits(session, "odds_api", settings.odds_credit_budget_per_day),
            "odds_api_daily_budget": settings.odds_credit_budget_per_day,
            "max_price_age_minutes": settings.max_price_age_minutes,
            "odds_min_refresh_minutes": settings.odds_min_refresh_minutes,
            "budget": budget_status(session),
        },
        "play_enabled": False,
    }


def injury_coverage_health(session: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    horizon = now + timedelta(hours=settings.odds_upcoming_hours)
    games = (
        session.query(Game)
        .filter(
            Game.data_tier != "demo",
            Game.kickoff_utc >= now - timedelta(hours=3),
            Game.kickoff_utc <= horizon,
            Game.status.in_(["scheduled", "preview", "pregame"]),
        )
        .all()
    )
    game_ids = [g.game_id for g in games]
    complete = {
        row[0]
        for row in session.query(InjuryEvent.game_id)
        .filter(InjuryEvent.game_id.in_(game_ids or ["__none__"]), InjuryEvent.coverage_complete.is_(True))
        .distinct()
        .all()
    }
    inactive_lists = games_with_inactive_list(session, game_ids)
    by_league: dict[str, dict[str, Any]] = {}
    coverage_counts: Counter[str] = Counter()
    games_out: list[dict[str, Any]] = []
    for game in games:
        lead = as_utc(game.kickoff_utc)
        info = availability_for_player(
            league_id=game.league_id,
            kickoff=game.kickoff_utc,
            player_injury=None,
            game_has_complete_report=game.game_id in complete,
            now=now,
            inactive_list_received=game.game_id in inactive_lists,
        )
        coverage = str(info["coverage"])
        coverage_counts[coverage] += 1
        bucket = by_league.setdefault(
            game.league_id,
            {
                "games": 0,
                "report_received_complete": 0,
                "report_expected_missing": 0,
                "report_not_yet_scheduled": 0,
                "source_unavailable": 0,
            },
        )
        bucket["games"] += 1
        if coverage in bucket:
            bucket[coverage] += 1
        games_out.append(
            {
                "game_id": game.game_id,
                "league_id": game.league_id,
                "kickoff_utc": lead,
                "coverage": coverage,
                "hours_to_kickoff": None if lead is None else (lead - now).total_seconds() / 3600.0,
                "within_nfl_report_window": (
                    False
                    if lead is None
                    else (lead - now) <= timedelta(hours=NFL_REPORT_LEAD_HOURS)
                ),
            }
        )

    shadow_reasons = Counter()
    for pred in session.query(ShadowPrediction).filter(ShadowPrediction.league_id.in_(["nfl", "fbs"])).all():
        for reason in pred.abstain_reasons or []:
            if "INJURY" in reason or reason in {"NEVER_ASSUME_HEALTHY", "CONFIRMED_INACTIVE", "UNCERTAIN_PLAYER_AVAILABILITY"}:
                shadow_reasons[reason] += 1

    latest_nfl_injury = (
        session.query(InjuryEvent.reported_at)
        .filter(InjuryEvent.source.like("nflverse%"))
        .order_by(InjuryEvent.reported_at.desc())
        .first()
    )
    return {
        "coverage_counts": dict(coverage_counts),
        "by_league": by_league,
        "games": sorted(games_out, key=lambda row: row["hours_to_kickoff"] or 999)[:80],
        "shadow_injury_abstentions": dict(shadow_reasons),
        "nflverse_latest_reported_at": latest_nfl_injury[0] if latest_nfl_injury else None,
        "api_sports_injury_ingest": _api_sports_injury_status(session),
        "notes": [
            "Missing reports are never treated as healthy players.",
            "NFL games with a complete report treat unnamed players as probable.",
            "FBS uses API-Sports injuries when the subscription plan allows the current season.",
            "plan_blocked / source_unavailable means availability is unknown, not healthy.",
        ],
        "honesty": {
            "plan_blocked_means": "unknown_availability",
            "never_assume_healthy": True,
            "partial_source_claims_full_coverage": False,
        },
    }


def odds_freshness_health(session: Session, now: datetime | None = None) -> dict[str, Any]:
    """Freshness by kickoff window. Started games are excluded from pregame denominators."""
    now = now or datetime.now(UTC)
    rows = session.execute(
        text(
            """
            SELECT o.game_id, g.league_id, g.kickoff_utc, g.status,
                   t.bookmaker_id, o.market_id,
                   MAX(COALESCE(t.observed_at, t.captured_at)) AS latest_observed_at,
                   MAX(t.source_at) AS latest_source_at,
                   MAX(t.captured_at) AS latest_captured_at
            FROM odds_ticks t
            JOIN outcomes o ON o.outcome_id = t.outcome_id
            JOIN games g ON g.game_id = o.game_id
            WHERE g.data_tier != 'demo'
              AND g.kickoff_utc >= :start
              AND g.kickoff_utc <= :end
            GROUP BY o.game_id, g.league_id, g.kickoff_utc, g.status, t.bookmaker_id, o.market_id
            """
        ),
        {
            "start": now - timedelta(hours=3),
            "end": now + timedelta(hours=settings.odds_upcoming_hours),
        },
    ).mappings().all()

    windows = {
        "near_pregame": {"hours_max": 6.0, "hours_min": 0.0},
        "later_pregame": {"hours_max": float(settings.odds_upcoming_hours), "hours_min": 6.0},
        "in_progress": {"hours_max": 0.0, "hours_min": -3.0},
    }
    by_window: dict[str, dict[str, Any]] = {
        name: {"price_rows": 0, "stale_rows": 0, "unverified_rows": 0, "ages": []} for name in windows
    }
    by_league_window: dict[str, dict[str, dict[str, int]]] = {}

    ages: list[float] = []
    stale = 0
    by_book: Counter[str] = Counter()
    by_market: Counter[str] = Counter()
    by_league_stale: Counter[str] = Counter()
    near_kickoff_stale = 0
    pregame_ages: list[float] = []
    pregame_stale = 0

    for row in rows:
        latest = _as_dt(row["latest_observed_at"]) or _as_dt(row["latest_captured_at"])
        if latest is None:
            continue
        age = (now - latest).total_seconds() / 60.0
        kickoff = _as_dt(row["kickoff_utc"])
        hours = None if kickoff is None else (kickoff - now).total_seconds() / 3600.0
        status = str(row["status"] or "").lower()
        started = hours is not None and hours < 0
        in_progress_status = status in {"in_progress", "live", "halftime", "final", "completed"}

        window = "later_pregame"
        if started or in_progress_status:
            window = "in_progress"
        elif hours is not None and hours <= 6:
            window = "near_pregame"

        bucket = by_window[window]
        bucket["price_rows"] += 1
        bucket["ages"].append(age)
        is_stale = age > max_fresh_minutes(kickoff, now)
        if is_stale:
            bucket["stale_rows"] += 1

        league = str(row["league_id"])
        lg = by_league_window.setdefault(
            league,
            {
                "near_pregame": {"priced": 0, "stale": 0},
                "later_pregame": {"priced": 0, "stale": 0},
                "in_progress": {"priced": 0, "stale": 0},
            },
        )
        lg[window]["priced"] += 1
        if is_stale:
            lg[window]["stale"] += 1

        # Pregame denominators exclude started / in-progress games.
        if window != "in_progress":
            ages.append(age)
            pregame_ages.append(age)
            if is_stale:
                stale += 1
                pregame_stale += 1
                by_book[str(row["bookmaker_id"])] += 1
                by_market[str(row["market_id"])] += 1
                by_league_stale[league] += 1
                if window == "near_pregame":
                    near_kickoff_stale += 1

    ages_sorted = sorted(pregame_ages)
    window_out: dict[str, Any] = {}
    for name, bucket in by_window.items():
        wa = sorted(bucket["ages"])
        denom = bucket["price_rows"]
        window_out[name] = {
            "price_rows": denom,
            "stale_rows": bucket["stale_rows"],
            "stale_share": (bucket["stale_rows"] / denom) if denom else 0.0,
            "age_minutes_p50": _percentile(wa, 0.5),
            "age_minutes_p90": _percentile(wa, 0.9),
            "denominator": "pregame_excludes_started" if name != "in_progress" else "started_or_live",
        }

    return {
        "price_rows": len(ages_sorted),
        "stale_rows": stale,
        "stale_share": (stale / len(ages_sorted)) if ages_sorted else 0.0,
        "age_minutes_p50": _percentile(ages_sorted, 0.5),
        "age_minutes_p90": _percentile(ages_sorted, 0.9),
        "age_minutes_max": ages_sorted[-1] if ages_sorted else None,
        "stale_by_bookmaker": dict(by_book),
        "stale_by_market": dict(by_market),
        "stale_by_league": dict(by_league_stale),
        "near_kickoff_stale_rows": near_kickoff_stale,
        "max_price_age_minutes": settings.max_price_age_minutes,
        "windows": window_out,
        "by_league_window": by_league_window,
        "denominator_note": "Top-level stale_share uses current pregame rows only; in-progress excluded.",
        "pregame_stale_rows": pregame_stale,
        "pregame_price_rows": len(pregame_ages),
    }


def job_execution_health(session: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    jobs = session.query(JobRun).order_by(JobRun.started_at.desc()).limit(60).all()
    by_name: dict[str, dict[str, Any]] = {}
    failures = 0
    for job in jobs:
        bucket = by_name.setdefault(job.job_name, {"runs": 0, "failed": 0, "last_status": None, "last_started_at": None})
        bucket["runs"] += 1
        if job.status == "failed":
            bucket["failed"] += 1
            failures += 1
        if bucket["last_started_at"] is None:
            bucket["last_status"] = job.status
            bucket["last_started_at"] = job.started_at
            bucket["last_error"] = job.error
    expected = [
        "odds_refresh",
        "pregame_forecasts",
        "shadow_capture",
        "atd_candidate_capture",
        "shadow_settle",
        "shadow_report",
    ]
    missing = [name for name in expected if name not in by_name]
    last_cycle = by_name.get("shadow_report") or by_name.get("atd_candidate_capture") or by_name.get("shadow_capture")
    age_min = None
    if last_cycle and last_cycle.get("last_started_at") is not None:
        started = as_utc(last_cycle["last_started_at"])
        if started is not None:
            age_min = (now - started).total_seconds() / 60.0
    recent_failures = [
        {
            "job_name": job.job_name,
            "started_at": job.started_at,
            "error": job.error,
        }
        for job in jobs
        if job.status == "failed"
    ][:10]
    empty_success: list[dict[str, Any]] = []
    for job in jobs[:20]:
        if job.status != "ok" or not isinstance(job.summary, dict):
            continue
        summary = job.summary
        if job.job_name == "odds_refresh":
            if summary.get("status") == "budget_stop" or summary.get("budget_stop"):
                empty_success.append({"job_name": job.job_name, "reason": "budget_stop", "started_at": job.started_at})
            else:
                considered = int(summary.get("events_fetched") or 0) + int(summary.get("events_skipped") or 0)
                if int(summary.get("ticks_upserted") or 0) == 0 and considered > 0:
                    empty_success.append(
                        {"job_name": job.job_name, "reason": "refreshed_zero", "started_at": job.started_at}
                    )
        if job.job_name == "pregame_forecasts" and int(summary.get("written") or summary.get("published") or 0) == 0:
            if summary.get("status") != "no_games":
                empty_success.append(
                    {"job_name": job.job_name, "reason": "published_zero", "started_at": job.started_at}
                )

    return {
        "recent_runs": len(jobs),
        "failures": failures,
        "recent_failures": recent_failures,
        "empty_success": empty_success[:10],
        "by_job": by_name,
        "expected_jobs_missing_history": missing,
        "minutes_since_last_cycle": age_min,
        "scheduler_hint": "Register scripts/register_shadow_tasks.ps1 if minutes_since_last_cycle keeps growing.",
        "alerts_visible": bool(
            failures or missing or empty_success or (age_min is not None and age_min > 180)
        ),
    }


def _api_sports_injury_status(session: Session) -> dict[str, Any]:
    from spe.db.models import DataCoverage

    row = (
        session.query(DataCoverage)
        .filter(DataCoverage.provider == "api_sports", DataCoverage.dataset == "injuries")
        .order_by(DataCoverage.last_ingested_at.desc())
        .first()
    )
    if row is None:
        return {"status": "not_run", "notes": "refresh-fbs-injuries has not been executed yet"}
    return {
        "status": row.status,
        "league_id": row.league_id,
        "season_year": row.season_year,
        "row_count": row.row_count,
        "notes": row.notes,
        "last_ingested_at": row.last_ingested_at,
    }


def _as_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return as_utc(value)


def _percentile(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = int(round(q * (len(sorted_values) - 1)))
    return sorted_values[max(0, min(idx, len(sorted_values) - 1))]
