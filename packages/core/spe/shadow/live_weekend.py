"""Weekend live Anytime TD validation ops.

No new models. Validated Play stays off. Does not manufacture real settlements.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.ops.freshness import operations_health
from spe.shadow.dry_run import run_settlement_dry_run
from spe.shadow.jobs import job_atd_candidates, job_settle
from spe.simulate.correlated_atd.shadow_candidates import verify_open_candidate_rows

CANDIDATE_PREFIXES = ("m3-", "m5-", "m6-", "atd_sim_v1-")
REQUIRED_FAMILIES = ("m3", "m5", "m6", "atd_sim_v1")


def report_dir() -> Any:
    from pathlib import Path

    path = Path("data") / "atd_sim_dev" / "live_weekend"
    path.mkdir(parents=True, exist_ok=True)
    return path


def family_of(model_version_id: str) -> str:
    from spe.shadow.report import _family

    return _family(model_version_id)


def weekend_games(session: Session, *, now: datetime | None = None, days: int = 4) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    rows = session.execute(
        text(
            """
            SELECT game_id, league_id, status, data_tier, kickoff_utc, home_team_id, away_team_id
            FROM games
            WHERE league_id IN ('nfl', 'fbs')
              AND data_tier = 'upcoming'
              AND status IN ('scheduled', 'preview', 'pregame')
              AND kickoff_utc >= :start
              AND kickoff_utc <= :end
            ORDER BY kickoff_utc
            """
        ),
        {"start": now - timedelta(hours=1), "end": now + timedelta(days=days)},
    ).mappings().all()
    return [dict(r) for r in rows]


def freeze_coverage(session: Session, games: list[dict[str, Any]]) -> dict[str, Any]:
    """Confirm M3/M5/M6/sim Anytime TD rows exist pre-kickoff for priced eligible players."""
    if not games:
        return {"n_games": 0, "error": "no_weekend_games"}
    gids = [g["game_id"] for g in games]
    priced = session.execute(
        text(
            """
            SELECT o.outcome_id, o.game_id, o.player_id, o.side,
                   g.league_id, g.kickoff_utc, g.home_team_id, g.away_team_id,
                   pl.name, pl.position_group,
                   t.tick_id, t.bookmaker_id, t.price_american, t.captured_at
            FROM outcomes o
            JOIN games g ON g.game_id = o.game_id
            JOIN odds_ticks t ON t.outcome_id = o.outcome_id
            LEFT JOIN players pl ON pl.player_id = o.player_id
            WHERE o.game_id IN :gids
              AND o.market_id = 'anytime_td'
              AND o.side IN ('yes', 'over')
              AND o.player_id IS NOT NULL
            """
        ).bindparams(bindparam("gids", expanding=True)),
        {"gids": gids},
    ).mappings().all()

    # Latest tick per outcome.
    latest: dict[str, dict[str, Any]] = {}
    for row in priced:
        oid = row["outcome_id"]
        prev = latest.get(oid)
        if prev is None or _dt(row["captured_at"]) >= _dt(prev["captured_at"]):
            latest[oid] = dict(row)

    shadows = session.execute(
        text(
            """
            SELECT shadow_id, outcome_id, player_id, game_id, league_id, model_version_id,
                   forecast_as_of, price_american, bookmaker_id, odds_tick_id, components,
                   research_only, play_enabled, recommendation_status
            FROM shadow_predictions
            WHERE market_id = 'anytime_td' AND game_id IN :gids
            """
        ).bindparams(bindparam("gids", expanding=True)),
        {"gids": gids},
    ).mappings().all()

    by_outcome_fam: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for s in shadows:
        fam = family_of(str(s["model_version_id"]))
        if fam not in REQUIRED_FAMILIES:
            continue
        by_outcome_fam.setdefault((str(s["outcome_id"]), fam), []).append(dict(s))

    missing: list[dict[str, Any]] = []
    field_issues: list[dict[str, Any]] = []
    ok = 0
    checked = 0
    by_league_fam: dict[str, dict[str, dict[str, int]]] = {}

    for oid, row in latest.items():
        league = str(row["league_id"])
        kick = as_utc(row["kickoff_utc"])
        lg_bucket = by_league_fam.setdefault(league, {f: {"present": 0, "missing": 0} for f in REQUIRED_FAMILIES})
        for fam in REQUIRED_FAMILIES:
            checked += 1
            rows = by_outcome_fam.get((oid, fam)) or []
            # Prefer a row frozen before kickoff on the latest tick when possible.
            chosen = None
            for cand in sorted(rows, key=lambda r: _dt(r["forecast_as_of"]), reverse=True):
                fas = as_utc(cand["forecast_as_of"])
                if kick and fas and fas >= kick:
                    continue
                chosen = cand
                if cand.get("odds_tick_id") == row["tick_id"]:
                    break
            if chosen is None:
                lg_bucket[fam]["missing"] += 1
                missing.append(
                    {
                        "outcome_id": oid,
                        "player_id": row["player_id"],
                        "player": row.get("name"),
                        "game_id": row["game_id"],
                        "league_id": league,
                        "family": fam,
                    }
                )
                continue
            lg_bucket[fam]["present"] += 1
            ok += 1
            problems = _field_problems(chosen, row, kick)
            if problems and len(field_issues) < 40:
                field_issues.append(
                    {
                        "shadow_id": chosen["shadow_id"],
                        "family": fam,
                        "player_id": chosen["player_id"],
                        "game_id": chosen["game_id"],
                        "problems": problems,
                    }
                )

    return {
        "n_weekend_games": len(games),
        "n_priced_anytime_outcomes": len(latest),
        "checks": checked,
        "frozen_ok": ok,
        "missing_family_forecasts": len(missing),
        "field_issue_rows": len(field_issues),
        "by_league_family": by_league_fam,
        "missing_by_family": {
            fam: sum(1 for m in missing if m["family"] == fam) for fam in REQUIRED_FAMILIES
        },
        "missing_examples": (
            [m for m in missing if m["family"] in {"m3", "m5", "m6"}][:15]
            + [m for m in missing if m["family"] == "atd_sim_v1"][:5]
        ),
        "field_issue_examples": field_issues[:20],
        "play_enabled": False,
    }


def _field_problems(shadow: dict[str, Any], priced: dict[str, Any], kick: datetime | None) -> list[str]:
    problems: list[str] = []
    if shadow.get("player_id") != priced.get("player_id"):
        problems.append("player_mismatch")
    if shadow.get("game_id") != priced.get("game_id"):
        problems.append("game_mismatch")
    if not shadow.get("model_version_id"):
        problems.append("missing_model_version")
    if shadow.get("price_american") is None:
        problems.append("missing_price")
    if not shadow.get("odds_tick_id"):
        problems.append("missing_odds_tick")
    fas = as_utc(shadow.get("forecast_as_of"))
    if kick and fas and not (fas < kick):
        problems.append("not_before_kickoff")
    if shadow.get("play_enabled"):
        problems.append("play_enabled_true")
    if not shadow.get("research_only"):
        problems.append("research_only_false")
    if shadow.get("recommendation_status") != "no_bet":
        problems.append("recommendation_not_no_bet")
    comps = shadow.get("components") if isinstance(shadow.get("components"), dict) else {}
    team_id = comps.get("team_id")
    if team_id and team_id not in {priced.get("home_team_id"), priced.get("away_team_id")}:
        problems.append("team_not_in_game")
    return problems


def deduped_eval_units(session: Session) -> dict[str, Any]:
    """Evaluation units that do not inflate sample size with duplicate sportsbook snapshots.

    Unit = (league, player_id, game_id, family) using the latest pre-kickoff freeze only.
    """
    rows = session.execute(
        text(
            """
            SELECT s.shadow_id, s.league_id, s.player_id, s.game_id, s.model_version_id,
                   s.forecast_as_of, s.odds_tick_id, s.outcome_id, s.forecast_probability,
                   g.kickoff_utc, st.result, st.actual_value
            FROM shadow_predictions s
            JOIN games g ON g.game_id = s.game_id
            LEFT JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
            WHERE s.market_id = 'anytime_td'
              AND (
                s.model_version_id LIKE 'm3-%anytime_td'
                OR s.model_version_id LIKE 'm3-%-anytime_td'
                OR s.model_version_id LIKE 'm5-%'
                OR s.model_version_id LIKE 'm6-%'
                OR s.model_version_id LIKE 'atd_sim_v1-%'
              )
            """
        )
    ).mappings().all()

    best: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    raw = 0
    for r in rows:
        fam = family_of(str(r["model_version_id"]))
        if fam not in REQUIRED_FAMILIES:
            continue
        raw += 1
        fas = as_utc(r["forecast_as_of"])
        kick = as_utc(r["kickoff_utc"])
        if kick and fas and not (fas < kick):
            continue
        key = (str(r["league_id"]), str(r["player_id"]), str(r["game_id"]), fam)
        prev = best.get(key)
        if prev is None or _dt(r["forecast_as_of"]) >= _dt(prev["forecast_as_of"]):
            best[key] = dict(r)

    by_league: dict[str, dict[str, Any]] = {}
    for (league, _pid, _gid, fam), row in best.items():
        bucket = by_league.setdefault(
            league,
            {"units": 0, "settled_win_loss": 0, "pending": 0, "blocked_or_other": 0, "by_family": {}},
        )
        bucket["units"] += 1
        fb = bucket["by_family"].setdefault(fam, {"units": 0, "settled_win_loss": 0})
        fb["units"] += 1
        result = row.get("result")
        if result in {"win", "loss"}:
            bucket["settled_win_loss"] += 1
            fb["settled_win_loss"] += 1
        elif result in {None, ""}:
            bucket["pending"] += 1
        else:
            bucket["blocked_or_other"] += 1

    return {
        "raw_anytime_candidate_rows": raw,
        "deduped_units": len(best),
        "inflation_ratio": float(raw / len(best)) if best else None,
        "by_league": by_league,
        "rule": "latest pre-kickoff freeze per (league, player, game, family)",
        "note": "Do not use raw shadow row counts as the evaluation N.",
    }


def scheduler_status() -> dict[str, Any]:
    """Confirm Windows Task Scheduler CaliberShadow* tasks are enabled and recently runnable."""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-ScheduledTask -TaskName 'CaliberShadow*' | "
             "Select-Object TaskName, State | ConvertTo-Json -Compress"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        tasks = []
        if completed.returncode == 0 and completed.stdout.strip():
            raw = json.loads(completed.stdout)
            tasks = raw if isinstance(raw, list) else [raw]
        # TaskState: Unknown=0 Disabled=1 Queued=2 Ready=3 Running=4
        ready_states = {2, 3, 4, "Queued", "Ready", "Running"}
        enabled = [t for t in tasks if t.get("State") in ready_states]
        # Also pull LastRunTime / LastTaskResult to prove jobs fire, not just register.
        detail = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-ScheduledTask -TaskName 'CaliberShadow*' | ForEach-Object { "
                    "$i = $_ | Get-ScheduledTaskInfo; "
                    "[pscustomobject]@{ TaskName=$_.TaskName; State=[int]$_.State; "
                    "LastRunTime=$i.LastRunTime; LastTaskResult=$i.LastTaskResult; "
                    "NextRunTime=$i.NextRunTime } "
                    "} | ConvertTo-Json -Compress"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        run_info = []
        if detail.returncode == 0 and detail.stdout.strip():
            raw_d = json.loads(detail.stdout)
            run_info = raw_d if isinstance(raw_d, list) else [raw_d]
        never_run = [
            r["TaskName"]
            for r in run_info
            if not r.get("LastRunTime")
            or str(r.get("LastRunTime")).startswith("0001-")
            or str(r.get("LastRunTime")).startswith("12/30/1899")
        ]
        # 0 = success; 267009 (0x41301) = SCHED_S_TASK_RUNNING — not a failure.
        ok_results = {None, 0, "0", 267009, "267009", 0x41301}
        failed_last = [
            {"TaskName": r["TaskName"], "LastTaskResult": r.get("LastTaskResult")}
            for r in run_info
            if r.get("LastTaskResult") not in ok_results
        ]
        blocker = None
        if len(enabled) < 4:
            blocker = "Scheduler tasks missing or disabled — run scripts/register_shadow_tasks.ps1"
        elif never_run:
            blocker = f"Tasks registered but never ran: {never_run}"
        elif failed_last:
            blocker = f"Tasks last run failed: {failed_last}"
        return {
            "registered": len(tasks),
            "enabled_or_running": len(enabled),
            "tasks": run_info or tasks,
            "actually_registered": len(tasks) > 0,
            "never_run": never_run,
            "failed_last_run": failed_last,
            "blocker": blocker,
        }
    except Exception as exc:  # noqa: BLE001
        return {"registered": 0, "error": str(exc), "blocker": "Could not query Task Scheduler"}


def build_alerts(session: Session, *, coverage: dict[str, Any], health: dict[str, Any], scheduler: dict[str, Any]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    jobs = health.get("job_execution") or {}
    if jobs.get("failures"):
        alerts.append({"severity": "high", "code": "FAILED_JOBS", "detail": f"failures={jobs.get('failures')}", "by_job": jobs.get("by_job")})
    age = jobs.get("minutes_since_last_cycle")
    if age is not None and age > 180:
        alerts.append({"severity": "high", "code": "STALE_SHADOW_CYCLE", "detail": f"minutes_since_last_cycle={age:.1f}"})
    if jobs.get("expected_jobs_missing_history"):
        alerts.append({"severity": "medium", "code": "MISSING_JOB_HISTORY", "detail": jobs["expected_jobs_missing_history"]})
    odds = health.get("odds_freshness") or {}
    if (odds.get("near_kickoff_stale_rows") or 0) > 0:
        alerts.append({"severity": "high", "code": "STALE_ODDS_NEAR_KICKOFF", "detail": odds.get("near_kickoff_stale_rows"), "by_market": odds.get("stale_by_market")})
    missing_by = coverage.get("missing_by_family") or {}
    core_missing = int(missing_by.get("m3") or 0) + int(missing_by.get("m5") or 0) + int(missing_by.get("m6") or 0)
    sim_missing = int(missing_by.get("atd_sim_v1") or 0)
    if core_missing > 0:
        alerts.append(
            {
                "severity": "high",
                "code": "MISSING_CORE_MODEL_FREEZES",
                "detail": core_missing,
                "by_family": {k: missing_by.get(k) for k in ("m3", "m5", "m6")},
                "examples": [m for m in (coverage.get("missing_examples") or []) if m.get("family") in {"m3", "m5", "m6"}][:20],
            }
        )
    if sim_missing > 0:
        alerts.append(
            {
                "severity": "medium",
                "code": "MISSING_SIM_FREEZES",
                "detail": sim_missing,
                "note": "atd_sim_v1 only freezes players present in correlated sim roster; depth/priced players without sim share are expected gaps",
                "examples": [m for m in (coverage.get("missing_examples") or []) if m.get("family") == "atd_sim_v1"][:10],
            }
        )
    if coverage.get("field_issue_rows"):
        alerts.append({"severity": "medium", "code": "FORECAST_FIELD_ISSUES", "detail": coverage.get("field_issue_rows")})
    if scheduler.get("blocker"):
        alerts.append({"severity": "high", "code": "SCHEDULER_NOT_OPERATIONAL", "detail": scheduler.get("blocker")})
    # Prove ATD capture rides the scheduled cycle (shadow_capture → atd → settle), not only manual weekend runs.
    by_job = ((health.get("job_execution") or {}).get("by_job")) or {}
    if "atd_candidate_capture" not in by_job:
        alerts.append(
            {
                "severity": "high",
                "code": "ATD_CAPTURE_NOT_IN_JOB_HISTORY",
                "detail": "atd_candidate_capture never recorded — cycle wiring not yet proven",
            }
        )
    else:
        # Heuristic: a scheduled cycle leaves shadow_capture then atd_candidate_capture within ~10 minutes.
        capture_at = (by_job.get("shadow_capture") or {}).get("last_started_at")
        atd_at = (by_job.get("atd_candidate_capture") or {}).get("last_started_at")
        if capture_at is not None and atd_at is not None:
            c = as_utc(capture_at)
            a = as_utc(atd_at)
            if c and a and not (0 <= (a - c).total_seconds() <= 600):
                alerts.append(
                    {
                        "severity": "medium",
                        "code": "ATD_CAPTURE_NOT_YET_PROVEN_ON_SCHEDULED_CYCLE",
                        "detail": {
                            "last_shadow_capture": str(c),
                            "last_atd_candidate_capture": str(a),
                            "note": "ATD is wired into run_shadow_cycle; await next CaliberShadowCycle to auto-prove",
                        },
                    }
                )

    # Missing settlements on finished games with open shadows.
    missing_settle = session.execute(
        text(
            """
            SELECT COUNT(*) AS n
            FROM shadow_predictions s
            JOIN games g ON g.game_id = s.game_id
            LEFT JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
            WHERE s.market_id = 'anytime_td'
              AND lower(g.status) IN ('final', 'post')
              AND st.shadow_id IS NULL
              AND s.forecast_as_of < g.kickoff_utc
            """
        )
    ).scalar()
    if int(missing_settle or 0) > 0:
        alerts.append({"severity": "high", "code": "MISSING_SETTLEMENTS_ON_FINALS", "detail": int(missing_settle)})

    # FBS 2026 stats gap (data priority, not invented).
    fbs_2026 = session.execute(
        text(
            """
            SELECT COUNT(*) FROM player_game_stats pgs
            JOIN games g ON g.game_id = pgs.game_id
            WHERE g.league_id = 'fbs' AND g.kickoff_utc >= '2026-08-01'
            """
        )
    ).scalar()
    if int(fbs_2026 or 0) == 0:
        alerts.append(
            {
                "severity": "high",
                "code": "FBS_2026_PLAYER_STATS_MISSING",
                "detail": "0 player_game_stats rows for FBS kickoffs since 2026-08-01 — next data priority alongside live tracking",
            }
        )
    return alerts


def run_live_weekend_validation(
    session: Session,
    *,
    freeze_now: bool = True,
    days: int = 4,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    report: dict[str, Any] = {
        "title": "Live Anytime TD Weekend Validation",
        "generated_at": now.isoformat(),
        "play_enabled": False,
        "validated_play": False,
        "research_only": True,
        "manufacture_settlements": False,
        "develop_new_models": False,
    }

    if freeze_now:
        print("[live-weekend] freezing ATD candidates for weekend slate…", flush=True)
        report["freeze_step"] = job_atd_candidates(session)
        session.commit()
        print("[live-weekend] freezing CFB board blend for live proof…", flush=True)
        from spe.shadow.fbs_board_live_proof import (
            build_board_freeze,
            ingest_board_freeze_into_shadow,
        )

        board_freeze = build_board_freeze(session, now=now)
        report["fbs_board_freeze"] = {
            "freeze_id": board_freeze.get("freeze_id"),
            "n_games": board_freeze.get("n_games"),
            "n_prekick_players": board_freeze.get("n_prekick_players"),
            "ingest": ingest_board_freeze_into_shadow(session, freeze=board_freeze, now=now),
        }
        session.commit()
        print("[live-weekend] settle pass (finals only; no invented zeros)…", flush=True)
        report["settle_step"] = job_settle(session)
        session.commit()

    games = weekend_games(session, now=now, days=days)
    report["weekend_slate"] = {
        "n_games": len(games),
        "by_league": _count_by(games, "league_id"),
        "by_day": _count_by([{**g, "day": str(as_utc(g["kickoff_utc"]))[:10]} for g in games], "day"),
        "games_head": [
            {"game_id": g["game_id"], "league_id": g["league_id"], "kickoff_utc": str(g["kickoff_utc"])}
            for g in games[:12]
        ],
    }
    report["freeze_coverage"] = freeze_coverage(session, games)
    report["open_row_verification"] = verify_open_candidate_rows(session)
    report["deduped_evaluation_units"] = deduped_eval_units(session)
    report["operations_health"] = operations_health(session, now)
    report["scheduler"] = scheduler_status()
    report["dry_run"] = run_settlement_dry_run()
    report["alerts"] = build_alerts(
        session,
        coverage=report["freeze_coverage"],
        health=report["operations_health"],
        scheduler=report["scheduler"],
    )
    report["blockers_requiring_intervention"] = [
        a for a in report["alerts"] if a.get("severity") == "high"
    ]
    report["settlement_rules_confirmed"] = {
        "missing_box_score": "blocked — not treated as zero TDs",
        "confirmed_inactive_pregame": "void",
        "identical_outcomes": "compare families only on deduped units with shared settled result",
        "duplicate_snapshots": "collapsed via latest pre-kickoff freeze per player/game/family",
    }
    report["next_data_priority"] = {
        "item": "Ingest FBS 2026 player game statistics into historical DB",
        "why": "College models cannot use current-season usage until boxes exist",
        "alongside": "Continue live shadow tracking this weekend",
    }

    path = report_dir() / "live_weekend_validation_report.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    return report


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = str(r.get(key))
        out[k] = out.get(k, 0) + 1
    return out


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return as_utc(value) or value
    if isinstance(value, str):
        return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00"))) or datetime.now(UTC)
    return datetime.min.replace(tzinfo=UTC)
