"""Ingest worker. Cadence is a budget, not a scrape loop.

Private prototype:
- odds 15–30 min pregame within credit ceiling
- injuries 30–60 min gameday
- weather 2–3 h, plus 90 min pregame
- features daily + 90 min pregame
Never scrape protected sportsbooks.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages" / "core"))
# Resolve relative DATABASE_URL / PARQUET_DIR against the repo root.
if Path.cwd().resolve() != ROOT:
    import os

    os.chdir(ROOT)

from spe.adapters.registry import get_adapter  # noqa: E402
from spe.config import settings  # noqa: E402
from spe.db.session import init_db, set_busy_timeout, session_factory  # noqa: E402
from spe.ingest.espn_scoreboard import ingest_football_scoreboard  # noqa: E402
from spe.ingest.cfbd_historical import DEFAULT_SEASONS as FBS_SEASONS  # noqa: E402
from spe.ingest.cfbd_historical import ingest_fbs_history  # noqa: E402
from spe.ingest.coverage import coverage_report  # noqa: E402
from spe.ingest.nflverse_historical import DEFAULT_SEASONS as NFL_SEASONS  # noqa: E402
from spe.ingest.nflverse_historical import ingest_nfl_history  # noqa: E402
from spe.ingest.nws_live import ingest_nws_forecasts  # noqa: E402
from spe.ingest.cfbd_players import ingest_fbs_players  # noqa: E402
from spe.ingest.odds_historical import ingest_odds_history  # noqa: E402
from spe.ingest.odds_live import ingest_odds_live  # noqa: E402  # noqa: E402
from spe.ingest.snapshots import append_raw, remaining_credits  # noqa: E402
from spe.ingest.weather_historical import record_weather_coverage  # noqa: E402


def cmd_research(limit: int, league: str | None = None, slate: bool = False) -> None:
    init_db()
    from spe.intelligence.client import OpenAIResponses
    from spe.intelligence.research import research_board, research_slate

    s = session_factory()
    try:
        if slate:
            rows = research_slate(s, OpenAIResponses(), per_league=limit)
        else:
            rows = research_board(s, OpenAIResponses(), limit=limit, league_id=league)
        s.commit()
        print(json.dumps(rows, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_research_reviews(limit: int) -> None:
    init_db()
    from spe.intelligence.review import review_backtests

    s = session_factory()
    try:
        written = review_backtests(s, limit=limit)
        s.commit()
        print(json.dumps({"reviews_written": written, "applied": False, "play_enabled": False}))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_settlement_improve(limit: int, league: str | None = None, ai: bool = False) -> None:
    init_db()
    from spe.intelligence.improve import improve_with_ai, review_settled_misses

    s = session_factory()
    try:
        if ai:
            from spe.intelligence.client import OpenAIResponses

            out = improve_with_ai(s, OpenAIResponses(), limit=limit, league_id=league)
        else:
            out = review_settled_misses(s, limit=limit, league_id=league)
        s.commit()
        print(json.dumps(out, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_monitor(limit: int, league: str | None = None, force: bool = False) -> None:
    init_db()
    from spe.intelligence.client import OpenAIResponses
    from spe.intelligence.monitor import monitor_board

    s = session_factory()
    try:
        rows = monitor_board(s, OpenAIResponses(), limit=limit, league_id=league, force=force)
        s.commit()
        print(json.dumps({"play_enabled": False, "rows": rows}, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_prediction_review(limit: int, league: str | None = None) -> None:
    init_db()
    from spe.intelligence.client import OpenAIResponses
    from spe.intelligence.review import review_board

    s = session_factory()
    try:
        rows = review_board(s, OpenAIResponses(), limit=limit, league_id=league)
        s.commit()
        print(json.dumps({"play_enabled": False, "rows": rows}, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_health() -> None:
    for pid in ("cfbd", "nflverse", "odds_api", "nws", "api_sports", "noaa_cdo"):
        print(get_adapter(pid).health())


def cmd_odds_budget() -> None:
    init_db()
    s = session_factory()
    left = remaining_credits(s, "odds_api", settings.odds_credit_budget_per_day)
    print(
        {
            "provider": "odds_api",
            "budget": settings.odds_credit_budget_per_day,
            "remaining": left,
            "markets": settings.odds_market_list,
            "note": "Measure credits empirically before scheduling all-market pulls.",
        }
    )
    s.close()


def cmd_ingest_odds_fixture() -> None:
    """Record a fixture snapshot without spending live credits."""
    init_db()
    adapter = get_adapter("odds_api")
    s = session_factory()
    fixture = {
        "id": "demo-event",
        "commence_time": datetime.now(UTC).isoformat(),
        "home_team": "Kansas City Chiefs",
        "away_team": "Buffalo Bills",
        "bookmakers": [],
    }
    result = adapter.fetch_event_odds("nfl", "demo-event", fixture=fixture)  # type: ignore[attr-defined]
    append_raw(
        s,
        provider="odds_api",
        endpoint=result.endpoint,
        provider_record_id="demo-event",
        payload={"records": result.records},
        observed_at=result.observed_at,
        credit_cost=0,
    )
    s.commit()
    s.close()
    print("Wrote fixture odds snapshot (0 credits).")


def cmd_ingest_nfl(start: int, end: int, pbp: bool, refresh: bool) -> None:
    init_db()
    s = session_factory()
    try:
        result = ingest_nfl_history(
            s,
            seasons=list(range(start, end + 1)),
            include_pbp=pbp,
            refresh=refresh,
        )
        result["nws"] = ingest_nws_forecasts(s)
        record_weather_coverage(s)
        ingest_odds_history(s)
        s.commit()
        print(json.dumps(result, indent=2))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_ingest_cfbd(start: int, end: int, plays: bool, players: bool) -> None:
    init_db()
    s = session_factory()
    try:
        result: dict = {}
        if plays or not players:
            result = ingest_fbs_history(
                s,
                seasons=list(range(start, end + 1)),
                include_plays=plays,
            )
        if players:
            result["players"] = ingest_fbs_players(s, seasons=list(range(start, end + 1)))
        ingest_odds_history(s)
        record_weather_coverage(s)
        s.commit()
        print(json.dumps(result, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_publish_model_forecasts() -> None:
    init_db()
    from spe.ingest.model_forecasts import publish_model_forecasts

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = publish_model_forecasts(s)
        s.commit()
        print(json.dumps(result, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_publish_slate_td(date: str | None) -> None:
    """Retired scoring-rate writer. This command now serves the trained models."""
    del date
    cmd_publish_model_forecasts()


def cmd_ingest_odds_live(
    hours: int | None,
    leagues: tuple[str, ...] = ("nfl", "fbs"),
    *,
    publish_forecasts: bool = True,
) -> None:
    init_db()
    s = session_factory()
    result: dict = {}
    try:
        set_busy_timeout(s, 120000)
        result = ingest_odds_live(
            s,
            horizon_hours=hours,
            leagues=leagues,
            publish_forecasts=publish_forecasts,
        )
        s.commit()
        print(json.dumps(result, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
    # Website boards read a saved props file. Rewrite it after prices/forecasts move.
    if int(result.get("ticks_upserted") or 0) > 0 or int(result.get("events_fetched") or 0) > 0:
        from spe.props.board_cache import refresh_near_slate

        board = session_factory()
        try:
            set_busy_timeout(board, 300000)
            warmed = refresh_near_slate(board)
            print(json.dumps({"props_board_cached": warmed}, default=str))
        except Exception as exc:
            print(json.dumps({"props_board_cached": 0, "props_board_error": type(exc).__name__}))
        finally:
            board.close()


def cmd_compare_research_models() -> None:
    init_db()
    from spe.models.advanced import run_comparison

    s = session_factory()
    try:
        result = run_comparison(s)
        print(json.dumps({"recommendation": result["recommendation"], "play_enabled": result["play_enabled"], "promoted": result["promoted"]}, indent=2))
    finally:
        s.close()


def cmd_freeze_m4(league: str) -> None:
    init_db()
    from spe.shadow.m4_artifact import export_m4_artifacts

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = export_m4_artifacts(s, league)
        s.commit()
        print(json.dumps(result, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_shadow_capture() -> None:
    init_db()
    from spe.shadow.jobs import job_capture

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = job_capture(s)
        s.commit()
        print(json.dumps(result, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_shadow_settle() -> None:
    init_db()
    from spe.shadow.jobs import job_settle

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = job_settle(s)
        s.commit()
        print(json.dumps(result, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_shadow_cycle(skip_odds: bool, skip_publish: bool) -> None:
    init_db()
    from spe.shadow.jobs import run_shadow_cycle

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_shadow_cycle(s, refresh_odds=not skip_odds, publish=not skip_publish)
        print(json.dumps(
            {
                "play_enabled": result["play_enabled"],
                "totals": result["track_record"]["totals"],
                "abstain_reason_counts": result["track_record"]["abstain_reason_counts"],
                "steps": [
                    {"job": step["job_name"], "status": step["status"], "error": step.get("error")}
                    for step in result["steps"]
                ],
            },
            indent=2,
            default=str,
        ))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_train_models(league: str, train_end: str, test_start: str, test_end: str) -> None:
    init_db()
    from spe.models.train import train_league_models

    s = session_factory()
    try:
        result = train_league_models(
            s,
            league,
            train_end=datetime.fromisoformat(train_end).replace(tzinfo=UTC),
            test_start=datetime.fromisoformat(test_start).replace(tzinfo=UTC),
            test_end=datetime.fromisoformat(test_end).replace(tzinfo=UTC),
        )
        s.commit()
        print(json.dumps(result, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_simulate_check(league: str) -> None:
    init_db()
    from spe.simulate.from_models import simulate_from_models

    s = session_factory()
    try:
        result = simulate_from_models(s, league)
        s.commit()
        print(json.dumps(result, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_ingest_weather(hours: int) -> None:
    init_db()
    s = session_factory()
    try:
        result = ingest_nws_forecasts(s, horizon_hours=hours)
        record_weather_coverage(s)
        s.commit()
        print(json.dumps(result, indent=2))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_ingest_free(start: int, end: int, refresh: bool, news: bool) -> None:
    init_db()
    from spe.analytics.usage import compute_usage
    from spe.ingest.gdelt_news import ingest_gdelt_news
    from spe.ingest.nflverse_extra import ingest_nfl_extras
    from spe.ingest.noaa_cdo import ingest_noaa_observations
    from spe.ingest.open_meteo_history import ingest_historical_forecasts
    from spe.ingest.quality import integration_quality

    s = session_factory()
    try:
        extra = ingest_nfl_extras(s, seasons=list(range(start, end + 1)), refresh=refresh)
        usage = compute_usage(s)
        weather = ingest_historical_forecasts(s)
        noaa = ingest_noaa_observations(s)
        gdelt = ingest_gdelt_news(s) if news else {"status": "skipped"}
        s.commit()
        quality = integration_quality(s)
        print(json.dumps({"extra": extra, "usage": usage, "weather": weather, "noaa": noaa, "gdelt": gdelt, "quality": quality}, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_ingest_scoreboard(year: int) -> None:
    init_db()
    s = session_factory()
    try:
        result = ingest_football_scoreboard(s, year)
        s.commit()
        print(json.dumps(result, indent=2))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_coverage() -> None:
    init_db()
    s = session_factory()
    try:
        print(json.dumps(coverage_report(s), indent=2, default=str))
    finally:
        s.close()


def cmd_refresh_injuries(year: int) -> None:
    """Refresh nflverse injuries for the active season. Does not invent healthy players."""
    init_db()
    from spe.ingest.nflverse_historical import ingest_nfl_history

    s = session_factory()
    try:
        result = ingest_nfl_history(
            s,
            seasons=[year],
            include_pbp=False,
            refresh=True,
            injuries_only=True,
        )
        s.commit()
        print(json.dumps(result, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_refresh_fbs_injuries(year: int) -> None:
    """Pull API-Sports FBS injuries. Missing/plan-blocked reports stay unknown."""
    init_db()
    from spe.ingest.api_sports_injuries import ingest_api_sports_injuries
    from spe.shadow.jobs import run_job

    s = session_factory()
    try:
        result = run_job(
            s,
            "fbs_injury_refresh",
            lambda session: ingest_api_sports_injuries(session, league_id="fbs", season=year),
        )
        s.commit()
        print(json.dumps(result, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_refresh_inactives() -> None:
    """Pull ESPN inactive lists near NFL kickoff. Unpublished lists stay unknown."""
    init_db()
    from spe.ingest.espn_availability import ingest_espn_availability
    from spe.shadow.jobs import run_job

    s = session_factory()
    try:
        set_busy_timeout(s, 120000)
        result = run_job(s, "gameday_availability", ingest_espn_availability)
        s.commit()
        print(json.dumps(result, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_ingest_portal(year: int) -> None:
    """Move FBS portal athletes onto the destination roster."""
    init_db()
    from spe.ingest.cfbd_portal import ingest_cfbd_portal

    s = session_factory()
    try:
        set_busy_timeout(s, 120000)
        result = ingest_cfbd_portal(s, year=year)
        s.commit()
        print(json.dumps(result, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_backfill_opportunity() -> None:
    """Write red-zone and goal-line counts from stored NFL plays."""
    init_db()
    from spe.ingest.nflverse_historical import backfill_opportunity_from_plays

    s = session_factory()
    try:
        set_busy_timeout(s, 120000)
        result = backfill_opportunity_from_plays(s)
        s.commit()
        print(json.dumps(result, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_ops_health() -> None:
    init_db()
    from spe.ops.freshness import operations_health

    s = session_factory()
    try:
        print(json.dumps(operations_health(s), indent=2, default=str))
    finally:
        s.close()


def cmd_repair_phase1(game_ids: list[str] | None, horizon_hours: int = 24 * 14) -> None:
    """Canonical games, roster sync, player maps, upcoming features. Validated Play stays off."""
    init_db()
    from spe.integrity.phase1 import run_phase1_repair

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_phase1_repair(
            s,
            verify_game_ids=game_ids or None,
            horizon_hours=horizon_hours,
        )
        s.commit()
        out = ROOT / "data" / "phase1_repair_report.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(json.dumps(result, indent=2, default=str))
        print(f"WROTE {out}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_phase1_td_integrity(horizon_hours: int) -> None:
    cmd_repair_phase1(None, horizon_hours=horizon_hours)


def cmd_research_player_td() -> None:
    """Agent-2 individual Anytime TD study. Isolated DB/artifacts. Does not touch team_td or promote."""
    init_db()
    from spe.models.player_td.study import run_player_td_study

    result = run_player_td_study(persist_report=True)
    summary = result.get("summary") or {}
    print(
        json.dumps(
            {
                "version": "m6_player_td",
                "play_enabled": False,
                "promoted": False,
                "research_only": True,
                "report_path": result.get("report_path"),
                "by_league": {
                    lg: {
                        "features_improved": body.get("features_improved"),
                        "selected_family": body.get("selected_family"),
                        "final_shadow": body.get("final_shadow"),
                        "artifact": body.get("artifact"),
                    }
                    for lg, body in (summary.get("by_league") or {}).items()
                },
            },
            indent=2,
            default=str,
        )
    )


def cmd_repair_phase2(game_ids: list[str] | None = None) -> None:
    """Rolling Anytime TD (M5). Preserves M3/M4. Research-only. Validated Play stays off."""
    init_db()
    from spe.integrity.phase2 import run_phase2

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_phase2(s, verify_game_ids=game_ids or None)
        s.commit()
        out = ROOT / "data" / "phase2_report.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(json.dumps(
            {
                "phase": 2,
                "play_enabled": False,
                "validated_play": False,
                "promoted": False,
                "training_data_cutoff": result.get("training_data_cutoff"),
                "current_season_coverage": result.get("current_season_coverage"),
                "eligibility_confirmed_n": (result.get("player_eligibility") or {})
                .get("roster_week_2026", {})
                .get("confirmed_ineligible_n"),
                "forecasts_written": (result.get("forecast_examples") or {}).get("written"),
                "mcmillan_2026": (result.get("mcmillan") or {}).get("uses_2026_history"),
                "mcmillan_forecast": (result.get("mcmillan") or {}).get("forecast"),
                "phase3_readiness": result.get("phase3_readiness"),
                "report_path": str(out),
            },
            indent=2,
            default=str,
        ))
        print(f"WROTE {out}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_phase2_team_td(horizon_hours: int = 24 * 14, skip_train: bool = False, skip_score: bool = False) -> None:
    """Team offensive TD opportunity engine. Preserves M3/M4. Research-only. Play stays off."""
    init_db()
    from spe.models.team_td.score import score_upcoming_team_td
    from spe.models.team_td.train import report_path, train_team_td_models

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result: dict = {
            "phase": 2,
            "engine": "team_td_opportunity",
            "play_enabled": False,
            "promoted": False,
            "validated_play": False,
            "research_only": True,
        }
        if not skip_train:
            result["train"] = train_team_td_models(s, persist=True)
        if not skip_score:
            live = score_upcoming_team_td(s, horizon_hours=horizon_hours)
            # Compact print: drop full row payloads.
            result["live"] = {
                "as_of": live.get("as_of"),
                "leakage_checks": live.get("leakage_checks"),
                "examples": live.get("examples", [])[:4],
                "leagues": {
                    lg: {k: v for k, v in body.items() if k != "rows"}
                    for lg, body in live.get("leagues", {}).items()
                },
                "live_path": live.get("live_path"),
                "report_path": live.get("report_path"),
            }
        s.commit()
        print(json.dumps(result, indent=2, default=str))
        print(f"WROTE {report_path()}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_live_atd_weekend(skip_freeze: bool = False, days: int = 4) -> None:
    """Weekend live Anytime TD ops check. No new models. Does not manufacture settlements. Play stays off."""
    init_db()
    from spe.shadow.live_weekend import run_live_weekend_validation

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_live_weekend_validation(s, freeze_now=not skip_freeze, days=days)
        s.commit()
        print(
            json.dumps(
                {
                    "command": "live-atd-weekend",
                    "play_enabled": False,
                    "validated_play": False,
                    "research_only": True,
                    "manufacture_settlements": False,
                    "weekend_games": (result.get("weekend_slate") or {}).get("n_games"),
                    "freeze_coverage": {
                        k: (result.get("freeze_coverage") or {}).get(k)
                        for k in (
                            "n_priced_anytime_outcomes",
                            "frozen_ok",
                            "missing_family_forecasts",
                            "field_issue_rows",
                            "by_league_family",
                        )
                    },
                    "scheduler": result.get("scheduler"),
                    "dry_run_pass": (result.get("dry_run") or {}).get("all_pass"),
                    "alerts": result.get("alerts"),
                    "blockers": result.get("blockers_requiring_intervention"),
                    "next_data_priority": result.get("next_data_priority"),
                    "report_path": result.get("report_path"),
                },
                indent=2,
                default=str,
            )
        )
        print(f"WROTE {result.get('report_path')}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_urgent_live_weekend(days: int = 4, skip_odds: bool = False) -> None:
    """Urgent live-weekend readiness remediation. No new models. Play stays off."""
    init_db()
    from spe.shadow.urgent_readiness import run_urgent_readiness

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_urgent_readiness(s, days=days, skip_odds=skip_odds)
        s.commit()
        print(
            json.dumps(
                {
                    "command": "urgent-live-weekend",
                    "play_enabled": False,
                    "validated_play": False,
                    "executive_summary": result.get("executive_summary"),
                    "report_path": result.get("report_path"),
                },
                indent=2,
                default=str,
            )
        )
        print(f"WROTE {result.get('report_path')}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_weekend_atd_results() -> None:
    """Post-weekend evaluation from settled pre-kickoff freezes only."""
    init_db()
    from spe.shadow.weekend_results import build_weekend_results_report

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = build_weekend_results_report(s)
        print(
            json.dumps(
                {
                    "command": "weekend-atd-results",
                    "play_enabled": False,
                    "q1_prekickoff_predictions": result.get("q1_prekickoff_predictions"),
                    "q2_successfully_settled": result.get("q2_successfully_settled"),
                    "q3_accuracy_by_model": result.get("q3_accuracy_by_model"),
                    "q4_calibration_by_model": result.get("q4_calibration_by_model"),
                    "q5_value_vs_book": result.get("q5_value_vs_book"),
                    "insufficient_settlements": result.get("insufficient_settlements"),
                    "report_path": result.get("report_path"),
                },
                indent=2,
                default=str,
            )
        )
        print(f"WROTE {result.get('report_path')}", flush=True)
    finally:
        s.close()


def cmd_real_world_atd_validation() -> None:
    """FBS 2026 + real settlement + model/book comparison. No new models. Play stays off."""
    init_db()
    from spe.shadow.real_world_validation import run_real_world_validation

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_real_world_validation(s)
        s.commit()
        print(
            json.dumps(
                {
                    "command": "real-world-atd-validation",
                    "play_enabled": False,
                    "validated_play": False,
                    "phase6_answers": result.get("phase6_answers"),
                    "phase1_fixed": (result.get("phase1_fbs_2026") or {}).get("fixed"),
                    "settled_summary": (result.get("phase2_settlement") or {}).get("settle_step"),
                    "graded_n": (result.get("phase3_model_accuracy") or {}).get("n_settled_win_loss"),
                    "report_path": result.get("report_path"),
                },
                indent=2,
                default=str,
            )
        )
        print(f"WROTE {result.get('report_path')}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_atd_accuracy_optimization(skip_matchup_test: bool = False) -> None:
    """Prospective accuracy optimization report. No retrain/promote. Play stays off."""
    init_db()
    from spe.shadow.accuracy_optimization import run_accuracy_optimization

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_accuracy_optimization(s, run_matchup_test=not skip_matchup_test)
        s.commit()
        print(
            json.dumps(
                {
                    "command": "atd-accuracy-optimization",
                    "play_enabled": False,
                    "validated_play": False,
                    "answers": result.get("answers"),
                    "graded": (result.get("answers") or {}).get("graded_prospective_units"),
                    "settlement": {
                        "before": (result.get("settlement") or {}).get("open_on_finals_before"),
                        "after": (result.get("settlement") or {}).get("open_on_finals_after"),
                        "step": ((result.get("settlement") or {}).get("settle_step") or {}).get("summary"),
                    },
                    "report_path": result.get("report_path"),
                },
                indent=2,
                default=str,
            )
        )
        print(f"WROTE {result.get('report_path')}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_atd_calibration_audit(run_matchup_test: bool = False) -> None:
    """Live accuracy & calibration audit. Models unchanged. Play stays off."""
    init_db()
    from spe.shadow.calibration_audit import run_calibration_audit

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_calibration_audit(s, run_matchup_test=run_matchup_test)
        s.commit()
        print(
            json.dumps(
                {
                    "command": "atd-calibration-audit",
                    "play_enabled": False,
                    "validated_play": False,
                    "models_unchanged": True,
                    "answers": result.get("answers"),
                    "report_path": result.get("report_path"),
                },
                indent=2,
                default=str,
            )
        )
        print(f"WROTE {result.get('report_path')}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_atd_live_validation(skip_capture: bool = False) -> None:
    """Pregame capture audit, settle, fair model compare. Models unchanged. Play stays off."""
    init_db()
    from spe.shadow.live_validation import run_live_validation

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_live_validation(s, expand_capture=not skip_capture)
        s.commit()
        print(
            json.dumps(
                {
                    "command": "atd-live-validation",
                    "play_enabled": False,
                    "validated_play": False,
                    "models_unchanged": True,
                    "answers": result.get("answers"),
                    "report_path": result.get("report_path"),
                },
                indent=2,
                default=str,
            )
        )
        print(f"WROTE {result.get('report_path')}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_atd_prospective_readiness() -> None:
    """Capture gaps, blocked settle retry, matched scoreboard. Models unchanged. Play stays off."""
    init_db()
    from spe.shadow.prospective_readiness import run_prospective_readiness

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_prospective_readiness(s)
        s.commit()
        print(
            json.dumps(
                {
                    "command": "atd-prospective-readiness",
                    "play_enabled": False,
                    "validated_play": False,
                    "models_unchanged": True,
                    "answers": result.get("answers"),
                    "blocked_after": (result.get("blocked_after_retry") or {}).get("blocked_family_rows"),
                    "gaps_after": (result.get("gaps_after") or {}).get("missing_counts"),
                    "report_path": result.get("report_path"),
                },
                indent=2,
                default=str,
            )
        )
        print(f"WROTE {result.get('report_path')}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_atd_prospective_closure() -> None:
    """Bounded closure: M4 provenance, 15 exclusions, blocked triage, C_recent handoff."""
    init_db()
    from spe.shadow.prospective_closure import run_prospective_closure

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_prospective_closure(s)
        s.commit()
        print(
            json.dumps(
                {
                    "command": "atd-prospective-closure",
                    "play_enabled": False,
                    "validated_play": False,
                    "models_unchanged": True,
                    "lead": result.get("lead"),
                    "m4_label": (result.get("m4_provenance") or {}).get("provenance_label"),
                    "exclusions_n": (result.get("exclusions_15") or {}).get("cross_family_exclusions_n"),
                    "blocked_still": (result.get("blocked_triage") or {}).get("still_blocked"),
                    "c_recent_paired": (result.get("agent2_cohort") or {}).get("registered_and_paired"),
                    "report_path": result.get("report_path"),
                },
                indent=2,
                default=str,
            )
        )
        print(f"WROTE {result.get('report_path')}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_atd_prospective_collection() -> None:
    """Scheduled collection: capture+settle, Agent2 821-pair ledger. No odds. Play stays off."""
    init_db()
    from spe.shadow.prospective_collection import run_prospective_collection

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_prospective_collection(s)
        s.commit()
        print(
            json.dumps(
                {
                    "command": "atd-prospective-collection",
                    "play_enabled": False,
                    "validated_play": False,
                    "models_unchanged": True,
                    "status": result.get("status"),
                    "lead": result.get("lead"),
                    "new_unique_player_games": (result.get("settled") or {}).get(
                        "new_unique_player_games_since_prior"
                    ),
                    "agent2_accounting": (result.get("agent2_pair_reconciliation") or {}).get("accounting"),
                    "blocked_player_games": (result.get("blocked") or {}).get("blocked_player_games"),
                    "failures": result.get("failures_to_escalate"),
                    "report_path": result.get("report_path"),
                },
                indent=2,
                default=str,
            )
        )
        print(f"WROTE {result.get('report_path')}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_cfb_atd_live_proof(skip_legacy: bool = False) -> None:
    """Freeze CFB board blend pre-kickoff, settle finals, update live-proof ledger. Play stays off."""
    init_db()
    from spe.shadow.fbs_board_live_proof import run_fbs_board_live_proof

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_fbs_board_live_proof(
            s,
            also_capture_legacy_candidates=not skip_legacy,
        )
        s.commit()
        proof = result.get("live_proof") or {}
        print(
            json.dumps(
                {
                    "command": "cfb-atd-live-proof",
                    "play_enabled": False,
                    "validated_play": False,
                    "research_only": True,
                    "freeze": result.get("freeze"),
                    "shadow_ingest": result.get("shadow_ingest"),
                    "open_prekickoff_freezes": proof.get("open_prekickoff_freezes"),
                    "settled_win_loss": proof.get("settled_win_loss"),
                    "unique_settled": proof.get("unique_settled"),
                    "report_path": result.get("report_path"),
                },
                indent=2,
                default=str,
            )
        )
        print(f"WROTE {result.get('report_path')}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_cfb_atd_ops_cycle(skip_boxes: bool = False, skip_legacy: bool = True) -> None:
    """Scoreboard → boxes → freeze/settle → production readiness. Play stays off until gates pass."""
    init_db()
    from spe.shadow.fbs_atd_production_readiness import run_fbs_atd_ops_cycle

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_fbs_atd_ops_cycle(
            s,
            skip_legacy_candidates=skip_legacy,
            ingest_boxes=not skip_boxes,
        )
        s.commit()
        ready = (result.get("readiness") or {}).get("production_ready")
        print(
            json.dumps(
                {
                    "command": "cfb-atd-ops-cycle",
                    "play_enabled": False,
                    "validated_play": False,
                    "production_ready": ready,
                    "hard_failures": (result.get("readiness") or {}).get("hard_failures"),
                    "blend_settled": (result.get("readiness") or {}).get("blend_settled"),
                    "remaining": (result.get("readiness") or {}).get("remaining"),
                    "report_path": result.get("report_path"),
                    "readiness_path": (result.get("readiness") or {}).get("report_path"),
                },
                indent=2,
                default=str,
            )
        )
        print(f"WROTE {result.get('report_path')}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_cfb_atd_production_readiness() -> None:
    """Evaluate CFB ATD production gates. Does not enable Play."""
    init_db()
    from spe.shadow.fbs_atd_production_readiness import evaluate_production_readiness

    s = session_factory()
    try:
        result = evaluate_production_readiness(s)
        print(json.dumps(result, indent=2, default=str))
        print(f"WROTE {result.get('report_path')}", flush=True)
    finally:
        s.close()


def cmd_prove_atd_accuracy(
    skip_paired: bool = False,
    skip_shadow: bool = False,
    skip_sportsbook: bool = False,
    draw_count: int = 1200,
    n_boot: int = 800,
    shadow_limit: int = 8,
) -> None:
    """Prove Anytime TD accuracy with correctly labeled evidence. No new models. Play stays off."""
    from spe.simulate.correlated_atd.run_proof import run_proof_accuracy

    result = run_proof_accuracy(
        skip_paired=skip_paired,
        skip_shadow=skip_shadow,
        skip_sportsbook=skip_sportsbook,
        draw_count=draw_count,
        n_boot=n_boot,
        shadow_limit=shadow_limit,
    )
    print(
        json.dumps(
            {
                "phase": "prove_atd_accuracy",
                "play_enabled": False,
                "promoted": False,
                "validated_play": False,
                "develop_new_models": False,
                "promotion_decision": "DO_NOT_PROMOTE",
                "summary": result.get("summary"),
                "remaining_blockers": result.get("remaining_blockers"),
                "report_path": result.get("report_path"),
                "protected_artifacts_unchanged": result.get("protected_artifacts_unchanged"),
            },
            indent=2,
            default=str,
        )
    )
    print(f"WROTE {result.get('report_path')}", flush=True)


def cmd_phase5_atd_validate(
    skip_expanded: bool = False,
    skip_fbs: bool = False,
    skip_matchup: bool = False,
    skip_shadow: bool = False,
    skip_sportsbook: bool = False,
    examined_games: int = 100,
    prospective_games: int = 60,
    draw_count: int = 1500,
    shadow_limit: int = 6,
) -> None:
    """Phase 5 Anytime TD validation. Does not promote. Validated Play stays off."""
    from spe.simulate.correlated_atd.run_phase5 import run_phase5

    result = run_phase5(
        skip_expanded=skip_expanded,
        skip_fbs=skip_fbs,
        skip_matchup=skip_matchup,
        skip_shadow=skip_shadow,
        skip_sportsbook=skip_sportsbook,
        examined_games=examined_games,
        prospective_games=prospective_games,
        draw_count=draw_count,
        shadow_limit_per_league=shadow_limit,
    )
    print(
        json.dumps(
            {
                "phase": 5,
                "play_enabled": False,
                "promoted": False,
                "validated_play": False,
                "research_only": True,
                "promotion_decision": "DO_NOT_PROMOTE",
                "summary": result.get("summary"),
                "report_path": result.get("report_path"),
                "protected_artifacts_unchanged": result.get("protected_artifacts_unchanged"),
                "remaining_blockers": result.get("remaining_blockers_before_promotion"),
            },
            indent=2,
            default=str,
        )
    )
    print(f"WROTE {result.get('report_path')}", flush=True)


def cmd_phase4_atd_sim(
    skip_backtest: bool = False,
    skip_examples: bool = False,
    shadow_days: int = 45,
    max_games: int = 80,
    example_limit: int = 2,
) -> None:
    """Correlated Team TD × M6 Anytime TD simulation. Isolated. Research-only. Play stays off."""
    from spe.simulate.correlated_atd.run import run_phase4

    result = run_phase4(
        skip_backtest=skip_backtest,
        skip_examples=skip_examples,
        shadow_days=shadow_days,
        max_games=max_games,
        example_limit=example_limit,
    )
    compact = {
        "phase": 4,
        "version": result.get("version"),
        "play_enabled": False,
        "promoted": False,
        "validated_play": False,
        "research_only": True,
        "mechanics_all_passed": ((result.get("mechanics_validation") or {}).get("checks") or {}).get("all_passed"),
        "nfl_sim_brier": ((result.get("backtests") or {}).get("nfl") or {}).get("atd_sim_v1", {}).get("brier")
        if isinstance((result.get("backtests") or {}).get("nfl"), dict)
        else None,
        "fbs_sim_brier": ((result.get("backtests") or {}).get("fbs") or {}).get("atd_sim_v1", {}).get("brier")
        if isinstance((result.get("backtests") or {}).get("fbs"), dict)
        else None,
        "n_example_games": (result.get("examples") or {}).get("n_games"),
        "report_path": result.get("report_path"),
        "examples_path": result.get("examples_path"),
        "protected_artifacts_unchanged": result.get("protected_artifacts_unchanged"),
        "recommendation": (result.get("recommendation_next_milestone") or {}).get("milestone"),
    }
    print(json.dumps(compact, indent=2, default=str))
    print(f"WROTE {result.get('report_path')}", flush=True)


def cmd_atd_integration_research(max_games: int = 80, draw_count: int = 1500) -> None:
    """Fair comparison of M3–M6, Player TD v1, and correlated sim. Research-only."""
    init_db()
    from spe.research.atd_integration import report_path, run_integration_research

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_integration_research(s, max_games=max_games, draw_count=draw_count)
        # Do not commit forecast writes — research uses reads + isolated report files.
        s.rollback()
        compact = {
            "status": result.get("status"),
            "protected_artifacts_unchanged": result.get("protected_artifacts_unchanged"),
            "best_nfl": result.get("3_best_nfl_touchdown_model"),
            "best_nfl_single": result.get("3b_best_nfl_single"),
            "best_fbs": result.get("4_best_fbs_touchdown_model"),
            "best_fbs_single": result.get("4b_best_fbs_single"),
            "ensemble_improves": result.get("5_ensemble_improves"),
            "report_path": result.get("report_path") or str(report_path()),
            "nfl_ranking": ((result.get("2_fair_comparison_results") or {}).get("nfl") or {}).get("ranking", [])[:8],
            "fbs_ranking": ((result.get("2_fair_comparison_results") or {}).get("fbs") or {}).get("ranking", [])[:8],
        }
        print(json.dumps(compact, indent=2, default=str))
        print(f"WROTE {compact['report_path']}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_m5_fbs_research_refit() -> None:
    """Verify FBS feature parity, then isolated M5 research refit A/B/C. No production overwrite."""
    init_db()
    from spe.research.m5_fbs_refit import run_m5_fbs_refit_milestone

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_m5_fbs_refit_milestone(s)
        s.rollback()
        print(
            json.dumps(
                {
                    "status": result.get("status"),
                    "gates_pass": result.get("gates_pass"),
                    "gates": result.get("gates"),
                    "final_answers": result.get("final_answers"),
                    "candidate": (result.get("refit") or {}).get("candidate_for_shadow"),
                    "report_path": result.get("report_path"),
                    "play_enabled": False,
                    "promoted": False,
                    "protected_m5_untouched": True,
                },
                indent=2,
                default=str,
            )
        )
        print(f"WROTE {result.get('report_path')}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_m5_fbs_lock_handoff() -> None:
    """Eligibility + loader checks, paired M5/C_recent freeze, lock candidate, Agent 1 handoff."""
    init_db()
    from spe.research.m5_fbs_lock_handoff import run_lock_and_handoff

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_lock_and_handoff(s)
        # Commit only append-only shadow ingest rows when present.
        if (result.get("shadow_ingest") or {}).get("written", 0) > 0:
            s.commit()
        else:
            s.rollback()
        print(
            json.dumps(
                {
                    "status": result.get("status"),
                    "gates_pass": result.get("gates_pass"),
                    "eligibility_pass": (result.get("eligibility") or {}).get("pass"),
                    "loader_pass": (result.get("loader") or {}).get("pass"),
                    "paired_freeze": result.get("paired_freeze"),
                    "lock_path": (result.get("lock") or {}).get("path"),
                    "agent1_handoff": (result.get("agent1_handoff") or {}).get("path"),
                    "shadow_ingest": {
                        k: (result.get("shadow_ingest") or {}).get(k)
                        for k in ("written", "unchanged", "skipped_no_price", "by_model")
                    },
                    "play_enabled": False,
                    "promoted": False,
                    "further_tuning": False,
                    "report_path": result.get("report_path"),
                },
                indent=2,
                default=str,
            )
        )
        print(f"WROTE {result.get('report_path')}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_m5_fbs_reconcile_pairs() -> None:
    """Reconcile locked paired freeze (priced/unpriced) for Agent 1. No tuning/promote."""
    init_db()
    from spe.research.m5_fbs_pair_reconcile import run_reconcile

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_reconcile(s)
        s.rollback()  # read-only reconciliation
        print(json.dumps(result.get("summary"), indent=2, default=str))
        print(f"WROTE {result.get('report_path')}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_atd_accuracy_milestone(
    skip_sparse: bool = False,
    skip_ingest: bool = False,
    skip_settle: bool = False,
) -> None:
    """FBS 2026 stats verify, sparse fallback OOS, prospective settle, model compare. No promotion."""
    init_db()
    from spe.research.atd_accuracy import report_path, run_accuracy_milestone

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_accuracy_milestone(
            s,
            ingest_shadow=not skip_ingest,
            run_settle=not skip_settle,
            eval_sparse=not skip_sparse,
        )
        compact = {
            "status": result.get("status"),
            "play_enabled": result.get("play_enabled"),
            "promoted": result.get("promoted"),
            "data_fixed": result.get("1_data_problems_fixed"),
            "sparse_improved": result.get("2_sparse_player_accuracy_improved"),
            "fbs_2026_flowing": result.get("3_fbs_2026_stats_flowing"),
            "settled": result.get("4_genuine_pregame_settled"),
            "best_models": result.get("5_best_model_identical_games"),
            "biggest_obstacle": result.get("6_biggest_remaining_obstacle"),
            "blockers": result.get("blockers_before_promotion"),
            "report_path": result.get("report_path") or str(report_path()),
        }
        print(json.dumps(compact, indent=2, default=str))
        print(f"WROTE {compact['report_path']}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_atd_reliability_guardrails(skip_settle: bool = False) -> None:
    """Disagreement flags, sparse-history tests, research warnings, finder payload. No promotion."""
    init_db()
    from spe.research.atd_guardrails import report_path, run_guardrails

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_guardrails(s, run_settle=not skip_settle)
        compact = {
            "status": result.get("status"),
            "play_enabled": result.get("play_enabled"),
            "promoted": result.get("promoted"),
            "official_probabilities_unchanged": result.get("official_probabilities_unchanged"),
            "disagreements": result.get("1_model_disagreements"),
            "sparse_focus": [
                {
                    "player": c.get("player_name"),
                    "sample_size": c.get("sample_size"),
                    "diagnosis": c.get("diagnosis"),
                    "p_v1": c.get("p_player_td_v1"),
                    "p_gbm": c.get("p_m6_hist_gbm"),
                    "fallback_gbm": ((c.get("hierarchical_fallback_tests") or {}).get("m6_hist_gbm") or {}),
                }
                for c in ((result.get("2_sparse_history") or {}).get("focus_players") or [])
            ],
            "warnings": (result.get("3_warnings_added") or {}).get("counts"),
            "finder": result.get("4_finder"),
            "settlement": {
                "freeze_ok": ((result.get("5_prospective_tracking") or {}).get("freeze") or {}).get("n_ok"),
                "settle": ((result.get("5_prospective_tracking") or {}).get("settle") or {}),
                "accuracy_note": ((result.get("5_prospective_tracking") or {}).get("accuracy") or {}).get("note"),
            },
            "blockers": result.get("6_remaining_blockers"),
            "report_path": result.get("report_path") or str(report_path()),
        }
        print(json.dumps(compact, indent=2, default=str))
        print(f"WROTE {compact['report_path']}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_verify_atd_research(
    skip_ingest: bool = False,
    skip_settle: bool = False,
) -> None:
    """Disagreement audit, reliability flags, freeze→shadow ingest, settlement. No promotion."""
    init_db()
    from spe.research.atd_verify import report_path, run_verification

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_verification(
            s,
            ingest_shadow=not skip_ingest,
            run_settle=not skip_settle,
        )
        compact = {
            "status": result.get("status"),
            "play_enabled": result.get("play_enabled"),
            "promoted": result.get("promoted"),
            "n_disagreements": (result.get("1_largest_model_disagreements") or {}).get("n"),
            "top_disagreements": (result.get("1_largest_model_disagreements") or {}).get("top", [])[:8],
            "chase": (
                {
                    k: ((result.get("2_causes_of_disagreements") or {}).get("chase_example") or {}).get(k)
                    for k in (
                        "player_name",
                        "p_player_td_v1",
                        "p_m6_hist_gbm",
                        "delta_gbm_minus_v1",
                        "root_cause",
                        "root_detail",
                    )
                }
                if (result.get("2_causes_of_disagreements") or {}).get("chase_example")
                else None
            ),
            "reliability": (result.get("4_forecast_reliability_assessment") or {}).get("by_level"),
            "freeze": {
                "n_games": (result.get("5_prospective_freeze_verification") or {}).get("n_games"),
                "n_ok": (result.get("5_prospective_freeze_verification") or {}).get("n_ok"),
                "n_with_issues": (result.get("5_prospective_freeze_verification") or {}).get("n_with_issues"),
            },
            "settlement": result.get("6_settlement_status"),
            "accuracy": result.get("7_actual_accuracy_results"),
            "blockers": result.get("8_remaining_blockers_before_promotion"),
            "report_path": result.get("report_path") or str(report_path()),
        }
        print(json.dumps(compact, indent=2, default=str))
        print(f"WROTE {compact['report_path']}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_unify_atd_research(
    horizon_hours: int = 24 * 14,
    skip_serialize: bool = False,
    skip_forecast: bool = False,
    skip_prospective: bool = False,
    max_games: int = 3,
) -> None:
    """Unify ATD features, serialize M6 HistGBM, research forecasts, prospective freeze. No promotion."""
    init_db()
    from spe.research.atd_unify import report_path, run_unification

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result = run_unification(
            s,
            horizon_hours=horizon_hours,
            skip_serialize=skip_serialize,
            skip_forecast=skip_forecast,
            skip_prospective=skip_prospective,
            max_games_per_league=max_games,
        )
        s.rollback()
        compact = {
            "status": result.get("status"),
            "protected_artifacts_unchanged": result.get("protected_artifacts_unchanged"),
            "served_m6_shrink_unchanged": result.get("served_m6_shrink_unchanged"),
            "play_enabled": result.get("play_enabled"),
            "promoted": result.get("promoted"),
            "architecture": result.get("1_unified_architecture"),
            "serialized": result.get("3_serialized_model_verification"),
            "nfl_examples": (result.get("4_nfl_research_forecast_examples") or [])[:4],
            "fbs_examples": (result.get("5_fbs_research_forecast_examples") or [])[:4],
            "remaining_bugs": result.get("6_remaining_bugs"),
            "prospective": result.get("7_prospective_tracking_readiness"),
            "next_milestone": result.get("8_recommended_next_milestone"),
            "report_path": result.get("report_path") or str(report_path()),
        }
        print(json.dumps(compact, indent=2, default=str))
        print(f"WROTE {compact['report_path']}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_phase3_player_td(
    horizon_hours: int = 24 * 14,
    skip_train: bool = False,
    skip_score: bool = False,
    game_ids: list[str] | None = None,
) -> None:
    """Player Anytime TD opportunity engine. Preserves M3/M4. Research-only. Play stays off."""
    init_db()
    from spe.models.player_td.score import score_upcoming_player_td
    from spe.models.player_td.train import report_path, train_player_td_models

    s = session_factory()
    try:
        set_busy_timeout(s, 300000)
        result: dict = {
            "phase": 3,
            "engine": "player_td_opportunity",
            "play_enabled": False,
            "promoted": False,
            "validated_play": False,
            "research_only": True,
        }
        if not skip_train:
            result["train"] = train_player_td_models(s, persist=True)
        if not skip_score:
            live = score_upcoming_player_td(
                s,
                game_ids=game_ids or None,
                horizon_hours=horizon_hours,
            )
            result["live"] = {
                "as_of": live.get("as_of"),
                "leakage_checks": live.get("leakage_checks"),
                "n_games": len(live.get("games") or []),
                "games": [
                    {
                        "game_id": g.get("game_id"),
                        "matchup": f"{g.get('away')} @ {g.get('home')}",
                        "n_players": len(g.get("players") or []),
                        "top": (g.get("players") or [])[:5],
                    }
                    for g in live.get("games") or []
                ],
                "live_path": live.get("live_path"),
                "report_path": live.get("report_path"),
            }
        s.commit()
        print(json.dumps(result, indent=2, default=str))
        print(f"WROTE {report_path()}", flush=True)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_compute_analytics(league: str, snapshots: bool) -> None:
    init_db()
    from spe.analytics.persist import compute_and_store

    s = session_factory()
    try:
        result = compute_and_store(s, league, include_snapshots=snapshots)
        s.commit()
        print(json.dumps(result, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_ats_backfill() -> None:
    init_db()
    from spe.ingest.ats_scores import backfill_ats_labels, ensure_point_spread_market

    s = session_factory()
    try:
        ensure_point_spread_market(s)
        out = backfill_ats_labels(s)
        s.commit()
        print(json.dumps(out, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_ats_backtest(league: str) -> None:
    init_db()
    from spe.models.ats.backtest import run_ats_backtest

    s = session_factory()
    try:
        leagues = ["nfl", "fbs"] if league == "all" else [league]
        reports = {}
        for lg in leagues:
            print(f"ATS_BACKTEST_START {lg}", flush=True)
            reports[lg] = run_ats_backtest(s, lg)
            s.commit()
            print(f"ATS_BACKTEST_DONE {lg} status={reports[lg].get('status')}", flush=True)
        print(json.dumps(reports, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_ats_predict(league: str | None = None) -> None:
    init_db()
    from spe.models.ats.predict import predict_upcoming_ats

    s = session_factory()
    try:
        out = predict_upcoming_ats(s, league_id=league)
        print(json.dumps({"n": len(out.get("rows") or []), "summaries": out.get("summaries"), "sample": (out.get("rows") or [])[:5]}, indent=2, default=str))
    finally:
        s.close()


def cmd_ingest_odds_spreads(
    max_events: int | None = None,
    leagues: tuple[str, ...] = ("nfl", "fbs"),
) -> None:
    init_db()
    from spe.ingest.odds_spreads import ingest_odds_spreads

    s = session_factory()
    out: dict | None = None
    try:
        set_busy_timeout(s, 120000)
        out = ingest_odds_spreads(s, max_events=max_events, leagues=leagues)
        s.commit()
        print(json.dumps(out, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
    # The site serves a saved card. New spreads stay invisible until that file is rewritten.
    if out and int(out.get("events_fetched") or 0) > 0:
        from spe.models.ats.board_cache import refresh_board

        board = session_factory()
        try:
            set_busy_timeout(board, 120000)
            saved = refresh_board(board)
            print(json.dumps({"board_cached": len(saved.get("rows") or [])}))
        except Exception as exc:
            print(json.dumps({"board_cached": 0, "board_error": type(exc).__name__}))
        finally:
            board.close()


def cmd_ats_shadow_capture() -> None:
    init_db()
    from spe.shadow.ats import capture_ats_predictions

    s = session_factory()
    try:
        out = capture_ats_predictions(s)
        s.commit()
        print(json.dumps(out, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_ats_shadow_settle() -> None:
    init_db()
    from spe.shadow.ats import settle_ats_predictions

    s = session_factory()
    try:
        out = settle_ats_predictions(s)
        s.commit()
        print(json.dumps(out, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cmd_ats_cycle(skip_odds: bool = False) -> None:
    """Backfill labels if needed, optional live spreads, predict, freeze, settle."""
    init_db()
    cmd_ats_backfill()
    if not skip_odds:
        cmd_ingest_odds_spreads(max_events=12)
    cmd_ats_shadow_capture()
    cmd_ats_shadow_settle()
    from spe.db.session import session_factory, set_busy_timeout
    from spe.models.ats.board_cache import refresh_board

    board_session = session_factory()
    try:
        saved = refresh_board(board_session)
        print(json.dumps({"board_cached": len(saved.get("rows") or [])}, default=str))
    finally:
        board_session.close()
    from spe.shadow.ats import ats_track_record

    s = session_factory()
    try:
        print(json.dumps(ats_track_record(s), indent=2, default=str))
    finally:
        s.close()


def cmd_backtest(league: str, train_end: str, test_start: str, test_end: str) -> None:
    init_db()
    from spe.eval.backtest import run_baseline_anytime_td

    s = session_factory()
    try:
        result = run_baseline_anytime_td(
            s,
            league,
            train_end=datetime.fromisoformat(train_end).replace(tzinfo=UTC),
            test_start=datetime.fromisoformat(test_start).replace(tzinfo=UTC),
            test_end=datetime.fromisoformat(test_end).replace(tzinfo=UTC),
        )
        s.commit()
        print(json.dumps(result, indent=2, default=str))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "production-cycle":
        parser = argparse.ArgumentParser()
        parser.add_argument("command")
        parser.add_argument("--lane", choices=("odds", "inputs", "settlement"), required=True)
        args = parser.parse_args()
        from spe.publishing.pipeline import production_cycle
        print(json.dumps(production_cycle(args.lane), default=str))
        return
    p = argparse.ArgumentParser(description="Caliber ingest worker")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("health")
    sub.add_parser("odds-budget")
    sub.add_parser("ingest-odds-fixture")
    sub.add_parser("coverage")
    free = sub.add_parser("ingest-free", help="Participation, NGS, rosters, depth charts, forecasts, NOAA, GDELT")
    free.add_argument("--start", type=int, default=2016)
    free.add_argument("--end", type=int, default=2025)
    free.add_argument("--refresh", action="store_true")
    free.add_argument("--skip-news", action="store_true")
    wx = sub.add_parser("ingest-weather", help="NWS stadium forecast, wind, precipitation, alerts")
    wx.add_argument("--hours", type=int, default=24 * 8, help="Forecast horizon in hours")

    nfl = sub.add_parser("ingest-nfl", help="Download and store nflverse historical seasons")
    nfl.add_argument("--start", type=int, default=min(NFL_SEASONS))
    nfl.add_argument("--end", type=int, default=max(NFL_SEASONS))
    nfl.add_argument("--pbp", action="store_true", help="Also ingest play-by-play (large)")
    nfl.add_argument("--refresh", action="store_true", help="Re-download cached parquet")

    board = sub.add_parser("ingest-scoreboard", help="Load the ESPN NFL and FBS schedule onto the board")
    board.add_argument("--year", type=int, default=2026)

    fbs = sub.add_parser("ingest-cfbd", help="Ingest NCAA FBS history via CollegeFootballData")
    fbs.add_argument("--start", type=int, default=min(FBS_SEASONS))
    fbs.add_argument("--end", type=int, default=max(FBS_SEASONS))
    fbs.add_argument("--plays", action="store_true", help="Ingest weekly PBP (counted against free-key budget)")
    fbs.add_argument("--players", action="store_true", help="Ingest roster, box scores, season PPA/usage")

    odds = sub.add_parser("ingest-odds-live", help="Pull current NFL/FBS player props within the credit budget")
    odds.add_argument("--hours", type=int, default=None, help="Only games kicking off inside this many hours")
    odds.add_argument(
        "--leagues",
        default="nfl,fbs",
        help="Comma-separated leagues to refresh (default nfl,fbs). Example: --leagues nfl",
    )
    odds.add_argument(
        "--skip-forecasts",
        action="store_true",
        help="Save prices only. Leave model forecast publishing to the shadow cycle.",
    )
    sub.add_parser(
        "publish-model-forecasts",
        help="Append research forecasts from the trained NFL and FBS models. Does not edit odds ticks.",
    )
    scorers_old = sub.add_parser(
        "publish-slate-td",
        help="Retired. Serves the trained models instead of the scoring-rate path.",
    )
    scorers_old.add_argument("--date", default=None, help="Ignored. Kept so old commands do not fail.")

    sub.add_parser(
        "compare-research-models",
        help="Backtest research features against the M3 feature set. Does not promote or enable Play",
    )
    freeze = sub.add_parser("freeze-m4", help="Freeze M4 research betas from m4_comparison.json. Does not promote.")
    freeze.add_argument("--league", choices=["nfl", "fbs"], required=True)
    sub.add_parser("shadow-capture", help="Append prospective M3/M4 predictions at current sportsbook prices")
    sub.add_parser("shadow-settle", help="Settle shadow predictions for finished games without inventing zeros")
    cycle = sub.add_parser("shadow-cycle", help="Odds refresh, forecasts, capture, settle, report. Restart-safe.")
    cycle.add_argument("--skip-odds", action="store_true")
    cycle.add_argument("--skip-publish", action="store_true")
    sub.add_parser("ats-backfill", help="Persist final scores + NFL consensus closing spreads for ATS labels")
    ats_bt = sub.add_parser("ats-backtest", help="Chronological ATS margin/cover backtest. Research-only; no promotion.")
    ats_bt.add_argument("--league", choices=["nfl", "fbs", "all"], default="all")
    ats_pred = sub.add_parser("ats-predict", help="Score upcoming ATS board from frozen research models")
    ats_pred.add_argument("--league", choices=["nfl", "fbs", "all"], default="all")
    spreads = sub.add_parser("ingest-odds-spreads", help="Pull live NFL/FBS point spreads within the credit budget")
    spreads.add_argument("--max-events", type=int, default=12)
    spreads.add_argument(
        "--leagues",
        default="nfl,fbs",
        help="Comma-separated leagues to refresh (default nfl,fbs). Example: --leagues nfl",
    )
    sub.add_parser("ats-shadow-capture", help="Freeze ATS research predictions before kickoff")
    sub.add_parser("ats-shadow-settle", help="Grade frozen ATS predictions after finals")
    ats_cycle = sub.add_parser("ats-cycle", help="ATS backfill, optional spreads, freeze, settle. Play stays off.")
    ats_cycle.add_argument("--skip-odds", action="store_true")
    inj = sub.add_parser("refresh-injuries", help="Refresh nflverse injury reports for a season (injuries only)")
    inj.add_argument("--year", type=int, default=datetime.now(UTC).year)
    fbs_inj = sub.add_parser(
        "refresh-fbs-injuries",
        help="Ingest API-Sports FBS injuries. Never treats missing reports as healthy.",
    )
    fbs_inj.add_argument("--year", type=int, default=datetime.now(UTC).year)
    sub.add_parser(
        "refresh-inactives",
        help="ESPN NFL inactive lists inside the kickoff window. Missing lists stay unknown.",
    )
    portal = sub.add_parser("ingest-portal", help="CFBD transfer portal roster moves for one season")
    portal.add_argument("--year", type=int, default=datetime.now(UTC).year)
    sub.add_parser(
        "backfill-opportunity",
        help="Fill NFL red-zone and goal-line counts from stored play-by-play",
    )
    sub.add_parser("ops-health", help="Injury coverage, odds freshness, and job execution health")
    phase1 = sub.add_parser(
        "phase1-td-integrity",
        help="Anytime TD Phase 1: canonical games, rosters, player maps, upcoming features. Validated Play stays off.",
    )
    phase1.add_argument("--horizon-hours", type=int, default=24 * 14)
    phase1.add_argument(
        "--game-id",
        action="append",
        default=[],
        help="Optional game id to verify (repeatable). Defaults to next NFL and FBS upcoming games.",
    )
    repair = sub.add_parser(
        "repair-phase1",
        help="Alias for phase1-td-integrity",
    )
    repair.add_argument("--horizon-hours", type=int, default=24 * 14)
    repair.add_argument("--game-id", action="append", default=[])
    phase2 = sub.add_parser(
        "repair-phase2",
        help="Legacy Phase 2 rolling player Anytime TD (M5). Preserves M3/M4. Research-only. Play stays off.",
    )
    phase2.add_argument("--game-id", action="append", default=[], help="Optional verification game ids")
    sub.add_parser(
        "research-player-td",
        help="Agent-2 individual Anytime TD research (m6_player_td). Isolated artifacts/DB. Does not touch team_td or promote.",
    )
    team_td = sub.add_parser(
        "phase2-team-td",
        help="Anytime TD Phase 2 team opportunity engine: possessions, scoring ops, TD distributions. Research-only.",
    )
    team_td.add_argument("--horizon-hours", type=int, default=24 * 14)
    team_td.add_argument("--skip-train", action="store_true")
    team_td.add_argument("--skip-score", action="store_true")
    phase3 = sub.add_parser(
        "phase3-player-td",
        help="Anytime TD Phase 3 player opportunity engine: allocate team TDs to eligible players. Research-only.",
    )
    phase3.add_argument("--horizon-hours", type=int, default=24 * 14)
    phase3.add_argument("--skip-train", action="store_true")
    phase3.add_argument("--skip-score", action="store_true")
    phase3.add_argument("--game-id", action="append", default=[], help="Optional game id (repeatable)")
    integ = sub.add_parser(
        "atd-integration-research",
        help="Fair compare M3/M4/M5/M6/Player TD v1/correlated sim + ensembles. Research-only; no promotion.",
    )
    integ.add_argument("--max-games", type=int, default=80)
    integ.add_argument("--draw-count", type=int, default=1500)
    unify = sub.add_parser(
        "unify-atd-research",
        help="Unify ATD feature pipeline, serialize M6 HistGBM, research forecasts, prospective freeze. No promotion.",
    )
    unify.add_argument("--horizon-hours", type=int, default=24 * 14)
    unify.add_argument("--max-games", type=int, default=3, help="Max upcoming games per league for inference checks")
    unify.add_argument("--skip-serialize", action="store_true")
    unify.add_argument("--skip-forecast", action="store_true")
    unify.add_argument("--skip-prospective", action="store_true")
    verify = sub.add_parser(
        "verify-atd-research",
        help="ATD verification: disagreements, reliability, freeze→shadow, settlement. No promotion.",
    )
    verify.add_argument("--skip-ingest", action="store_true", help="Do not write freeze rows into shadow_predictions")
    verify.add_argument("--skip-settle", action="store_true", help="Do not run shadow settlement")
    guard = sub.add_parser(
        "atd-reliability-guardrails",
        help="ATD reliability: disagreement flags, sparse-history tests, finder warnings. No promotion.",
    )
    guard.add_argument("--skip-settle", action="store_true")
    sub.add_parser(
        "m5-fbs-research-refit",
        help="Feature-parity checks then isolated FBS M5 research refit A/B/C. Does not overwrite fbs_m5.json.",
    )
    sub.add_parser(
        "m5-fbs-lock-handoff",
        help="Verify eligibility/loader, paired M5 vs C_recent freeze, lock candidate, hand to Agent 1. No promote.",
    )
    sub.add_parser(
        "m5-fbs-reconcile-pairs",
        help="Reconcile all locked M5/C_recent pairs (priced+unpriced) into Agent 1 mapping. No promote/tuning.",
    )
    acc = sub.add_parser(
        "atd-accuracy-milestone",
        help="ATD accuracy: FBS 2026 stats verify, sparse OOS fallbacks, settle, model compare. No promotion.",
    )
    acc.add_argument("--skip-sparse", action="store_true")
    acc.add_argument("--skip-ingest", action="store_true")
    acc.add_argument("--skip-settle", action="store_true")
    phase4 = sub.add_parser(
        "phase4-atd-sim",
        help="Phase 4 correlated Anytime TD simulation (Team TD × M6). Research-only. Isolated artifacts. Play stays off.",
    )
    phase4.add_argument("--skip-backtest", action="store_true")
    phase4.add_argument("--skip-examples", action="store_true")
    phase4.add_argument("--shadow-days", type=int, default=45)
    phase4.add_argument("--max-games", type=int, default=80)
    phase4.add_argument("--example-limit", type=int, default=2)
    phase5 = sub.add_parser(
        "phase5-atd-validate",
        help="Phase 5 Anytime TD validation: expanded sample, FBS diagnosis, matchups, shadow, sportsbooks. Research-only.",
    )
    phase5.add_argument("--skip-expanded", action="store_true")
    phase5.add_argument("--skip-fbs", action="store_true")
    phase5.add_argument("--skip-matchup", action="store_true")
    phase5.add_argument("--skip-shadow", action="store_true")
    phase5.add_argument("--skip-sportsbook", action="store_true")
    phase5.add_argument("--examined-games", type=int, default=100)
    phase5.add_argument("--prospective-games", type=int, default=60)
    phase5.add_argument("--draw-count", type=int, default=1500)
    phase5.add_argument("--shadow-limit", type=int, default=6)
    live_w = sub.add_parser(
        "live-atd-weekend",
        help="Verify live Anytime TD shadow ops for this weekend (freeze/settle/alerts/dry-run). No new models. Play stays off.",
    )
    live_w.add_argument("--skip-freeze", action="store_true", help="Audit only; do not append new freezes")
    live_w.add_argument("--days", type=int, default=4)
    urgent = sub.add_parser(
        "urgent-live-weekend",
        help="Urgent readiness: refresh stale odds, complete pregame freezes, verify automation, classify sim gaps, audit FBS 2026, settle finals. Play stays off.",
    )
    urgent.add_argument("--days", type=int, default=4)
    urgent.add_argument("--skip-odds", action="store_true")
    weekend_res = sub.add_parser(
        "weekend-atd-results",
        help="Post-weekend Anytime TD results: accuracy, calibration, value, disagreements. Deduped pre-kickoff only. No promotion.",
    )
    rwv = sub.add_parser(
        "real-world-atd-validation",
        help="Milestone: FBS 2026 stats check, real settlement, model/book comparison, disagreements. No new models. Play stays off.",
    )
    acc = sub.add_parser(
        "atd-accuracy-optimization",
        help="Accuracy optimization: settle, error autopsy, matchup audit, position splits, EV analysis. No retrain/promote. Play stays off.",
    )
    acc.add_argument("--skip-matchup-test", action="store_true")
    cal = sub.add_parser(
        "atd-calibration-audit",
        help="Live accuracy & calibration audit: underconfidence cohorts, EV math, fair book compare. Models unchanged. Play stays off.",
    )
    cal.add_argument("--run-matchup-test", action="store_true", help="Re-run historical matchup ablation (labeled, not live)")
    livev = sub.add_parser(
        "atd-live-validation",
        help="Live ATD validation: pregame capture coverage, settle, fair compare. No retrain/promote. Play stays off.",
    )
    livev.add_argument("--skip-capture", action="store_true", help="Audit/settle/compare only; do not expand freezes")
    sub.add_parser(
        "atd-prospective-readiness",
        help="Prospective readiness: exact capture gaps, blocked retry, matched scoreboard. Play stays off.",
    )
    sub.add_parser(
        "atd-prospective-closure",
        help="Bounded closure: M4 provenance, 15 exclusions, blocked triage, C_recent handoff. Play stays off.",
    )
    sub.add_parser(
        "atd-prospective-collection",
        help="Scheduled collection: capture+settle (no odds), Agent2 821-pair reconciliation. Play stays off.",
    )
    cfb_proof = sub.add_parser(
        "cfb-atd-live-proof",
        help="Freeze CFB Anytime TD board blend pre-kickoff, settle finals, update live-proof ledger. Play stays off.",
    )
    cfb_proof.add_argument(
        "--skip-legacy",
        action="store_true",
        help="Skip m3/m5/m6/sim candidate capture; only freeze board blend/C_recent/v1",
    )
    cfb_ops = sub.add_parser(
        "cfb-atd-ops-cycle",
        help="Scoreboard → CFBD boxes → freeze/settle → production readiness. Play stays off until gates pass.",
    )
    cfb_ops.add_argument("--skip-boxes", action="store_true")
    cfb_ops.add_argument("--include-legacy", action="store_true", help="Also capture m3/m5/m6/sim candidates")
    sub.add_parser(
        "cfb-atd-production-readiness",
        help="Evaluate CFB Anytime TD production gates. Does not enable Play.",
    )
    proof = sub.add_parser(
        "prove-atd-accuracy",
        help="Prove Anytime TD accuracy: audit prospective labels, paired CIs, live shadow, settlements. No new models. No promote.",
    )
    proof.add_argument("--skip-paired", action="store_true")
    proof.add_argument("--skip-shadow", action="store_true")
    proof.add_argument("--skip-sportsbook", action="store_true")
    proof.add_argument("--draw-count", type=int, default=1200)
    proof.add_argument("--n-boot", type=int, default=800)
    proof.add_argument("--shadow-limit", type=int, default=8)
    tr = sub.add_parser("train-models", help="Train league-specific M3 models (does not enable Play)")
    tr.add_argument("--league", choices=["nfl", "fbs"], required=True)
    tr.add_argument("--train-end", default="2024-08-01")
    tr.add_argument("--test-start", default="2024-08-01")
    tr.add_argument("--test-end", default="2025-02-15")

    sim = sub.add_parser("simulate-check", help="Research-only correlated sim calibration check")
    sim.add_argument("--league", choices=["nfl", "fbs"], required=True)

    an = sub.add_parser("compute-analytics", help="Compute team/player analytics and pregame snapshots")
    an.add_argument("--league", choices=["nfl", "fbs"], required=True)
    an.add_argument("--no-snapshots", action="store_true")

    bt = sub.add_parser("backtest-baseline", help="Chronological naive anytime-TD backtest (not a validated model)")
    bt.add_argument("--league", choices=["nfl", "fbs"], required=True)
    bt.add_argument("--train-end", default="2023-09-01")
    bt.add_argument("--test-start", default="2023-09-01")
    bt.add_argument("--test-end", default="2024-02-15")

    research = sub.add_parser("research-board", help="Cache AI game research and opportunity detection")
    research.add_argument("--limit", type=int, default=1, help="Games per run (per league when --slate)")
    research.add_argument("--league", choices=["nfl", "fbs"], default=None, help="Limit to one league")
    research.add_argument(
        "--slate",
        action="store_true",
        help="Research NFL and FBS separately with --limit games each",
    )
    monitor = sub.add_parser("research-monitor", help="Re-research only when verified context changed")
    monitor.add_argument("--limit", type=int, default=2)
    monitor.add_argument("--league", choices=["nfl", "fbs"], default=None)
    monitor.add_argument("--force", action="store_true", help="Ignore fingerprint cache")
    pred_review = sub.add_parser("prediction-review", help="AI review of Python forecasts (cannot override p)")
    pred_review.add_argument("--limit", type=int, default=1)
    pred_review.add_argument("--league", choices=["nfl", "fbs"], default=None)
    reviews = sub.add_parser("research-reviews", help="Suggest backtest experiments without editing the model")
    reviews.add_argument("--limit", type=int, default=20)
    improve = sub.add_parser(
        "settlement-improve",
        help="Phase 7 miss analysis. Writes hypotheses only; never retrains or enables Play",
    )
    improve.add_argument("--limit", type=int, default=20)
    improve.add_argument("--league", choices=["nfl", "fbs"], default=None)
    improve.add_argument("--ai", action="store_true", help="Ask OpenAI for additional testable hypotheses")

    args = p.parse_args()
    if args.command == "health":
        cmd_health()
    elif args.command == "odds-budget":
        cmd_odds_budget()
    elif args.command == "ingest-odds-fixture":
        cmd_ingest_odds_fixture()
    elif args.command == "coverage":
        cmd_coverage()
    elif args.command == "ingest-free":
        cmd_ingest_free(args.start, args.end, args.refresh, news=not args.skip_news)
    elif args.command == "ingest-weather":
        cmd_ingest_weather(args.hours)
    elif args.command == "ingest-nfl":
        cmd_ingest_nfl(args.start, args.end, args.pbp, args.refresh)
    elif args.command == "ingest-scoreboard":
        cmd_ingest_scoreboard(args.year)
    elif args.command == "ingest-cfbd":
        cmd_ingest_cfbd(args.start, args.end, args.plays, args.players)
    elif args.command == "ingest-odds-live":
        live_leagues = tuple(x.strip() for x in str(args.leagues).split(",") if x.strip())
        cmd_ingest_odds_live(
            args.hours,
            live_leagues or ("nfl", "fbs"),
            publish_forecasts=not args.skip_forecasts,
        )
    elif args.command == "publish-model-forecasts":
        cmd_publish_model_forecasts()
    elif args.command == "publish-slate-td":
        cmd_publish_slate_td(args.date)
    elif args.command == "compare-research-models":
        cmd_compare_research_models()
    elif args.command == "freeze-m4":
        cmd_freeze_m4(args.league)
    elif args.command == "shadow-capture":
        cmd_shadow_capture()
    elif args.command == "shadow-settle":
        cmd_shadow_settle()
    elif args.command == "shadow-cycle":
        cmd_shadow_cycle(args.skip_odds, args.skip_publish)
    elif args.command == "refresh-injuries":
        cmd_refresh_injuries(args.year)
    elif args.command == "refresh-fbs-injuries":
        cmd_refresh_fbs_injuries(args.year)
    elif args.command == "refresh-inactives":
        cmd_refresh_inactives()
    elif args.command == "ingest-portal":
        cmd_ingest_portal(args.year)
    elif args.command == "backfill-opportunity":
        cmd_backfill_opportunity()
    elif args.command == "ops-health":
        cmd_ops_health()
    elif args.command in {"phase1-td-integrity", "repair-phase1"}:
        cmd_repair_phase1(args.game_id or None, horizon_hours=args.horizon_hours)
    elif args.command == "repair-phase2":
        cmd_repair_phase2(args.game_id or None)
    elif args.command == "research-player-td":
        cmd_research_player_td()
    elif args.command == "phase2-team-td":
        cmd_phase2_team_td(
            horizon_hours=args.horizon_hours,
            skip_train=args.skip_train,
            skip_score=args.skip_score,
        )
    elif args.command == "phase3-player-td":
        cmd_phase3_player_td(
            horizon_hours=args.horizon_hours,
            skip_train=args.skip_train,
            skip_score=args.skip_score,
            game_ids=args.game_id or None,
        )
    elif args.command == "atd-integration-research":
        cmd_atd_integration_research(max_games=args.max_games, draw_count=args.draw_count)
    elif args.command == "unify-atd-research":
        cmd_unify_atd_research(
            horizon_hours=args.horizon_hours,
            skip_serialize=args.skip_serialize,
            skip_forecast=args.skip_forecast,
            skip_prospective=args.skip_prospective,
            max_games=args.max_games,
        )
    elif args.command == "verify-atd-research":
        cmd_verify_atd_research(skip_ingest=args.skip_ingest, skip_settle=args.skip_settle)
    elif args.command == "atd-reliability-guardrails":
        cmd_atd_reliability_guardrails(skip_settle=args.skip_settle)
    elif args.command == "m5-fbs-research-refit":
        cmd_m5_fbs_research_refit()
    elif args.command == "m5-fbs-lock-handoff":
        cmd_m5_fbs_lock_handoff()
    elif args.command == "m5-fbs-reconcile-pairs":
        cmd_m5_fbs_reconcile_pairs()
    elif args.command == "atd-accuracy-milestone":
        cmd_atd_accuracy_milestone(
            skip_sparse=args.skip_sparse,
            skip_ingest=args.skip_ingest,
            skip_settle=args.skip_settle,
        )
    elif args.command == "phase4-atd-sim":
        cmd_phase4_atd_sim(
            skip_backtest=args.skip_backtest,
            skip_examples=args.skip_examples,
            shadow_days=args.shadow_days,
            max_games=args.max_games,
            example_limit=args.example_limit,
        )
    elif args.command == "phase5-atd-validate":
        cmd_phase5_atd_validate(
            skip_expanded=args.skip_expanded,
            skip_fbs=args.skip_fbs,
            skip_matchup=args.skip_matchup,
            skip_shadow=args.skip_shadow,
            skip_sportsbook=args.skip_sportsbook,
            examined_games=args.examined_games,
            prospective_games=args.prospective_games,
            draw_count=args.draw_count,
            shadow_limit=args.shadow_limit,
        )
    elif args.command == "live-atd-weekend":
        cmd_live_atd_weekend(skip_freeze=args.skip_freeze, days=args.days)
    elif args.command == "urgent-live-weekend":
        cmd_urgent_live_weekend(days=args.days, skip_odds=args.skip_odds)
    elif args.command == "weekend-atd-results":
        cmd_weekend_atd_results()
    elif args.command == "real-world-atd-validation":
        cmd_real_world_atd_validation()
    elif args.command == "atd-accuracy-optimization":
        cmd_atd_accuracy_optimization(skip_matchup_test=args.skip_matchup_test)
    elif args.command == "atd-calibration-audit":
        cmd_atd_calibration_audit(run_matchup_test=args.run_matchup_test)
    elif args.command == "atd-live-validation":
        cmd_atd_live_validation(skip_capture=args.skip_capture)
    elif args.command == "atd-prospective-readiness":
        cmd_atd_prospective_readiness()
    elif args.command == "atd-prospective-closure":
        cmd_atd_prospective_closure()
    elif args.command == "atd-prospective-collection":
        cmd_atd_prospective_collection()
    elif args.command == "cfb-atd-live-proof":
        cmd_cfb_atd_live_proof(skip_legacy=args.skip_legacy)
    elif args.command == "cfb-atd-ops-cycle":
        cmd_cfb_atd_ops_cycle(skip_boxes=args.skip_boxes, skip_legacy=not args.include_legacy)
    elif args.command == "cfb-atd-production-readiness":
        cmd_cfb_atd_production_readiness()
    elif args.command == "prove-atd-accuracy":
        cmd_prove_atd_accuracy(
            skip_paired=args.skip_paired,
            skip_shadow=args.skip_shadow,
            skip_sportsbook=args.skip_sportsbook,
            draw_count=args.draw_count,
            n_boot=args.n_boot,
            shadow_limit=args.shadow_limit,
        )
    elif args.command == "train-models":
        cmd_train_models(args.league, args.train_end, args.test_start, args.test_end)
    elif args.command == "simulate-check":
        cmd_simulate_check(args.league)
    elif args.command == "compute-analytics":
        cmd_compute_analytics(args.league, snapshots=not args.no_snapshots)
    elif args.command == "backtest-baseline":
        cmd_backtest(args.league, args.train_end, args.test_start, args.test_end)
    elif args.command == "ats-backfill":
        cmd_ats_backfill()
    elif args.command == "ats-backtest":
        cmd_ats_backtest(args.league)
    elif args.command == "ats-predict":
        cmd_ats_predict(None if args.league == "all" else args.league)
    elif args.command == "ingest-odds-spreads":
        leagues = tuple(x.strip() for x in str(args.leagues).split(",") if x.strip())
        cmd_ingest_odds_spreads(max_events=args.max_events, leagues=leagues or ("nfl", "fbs"))
    elif args.command == "ats-shadow-capture":
        cmd_ats_shadow_capture()
    elif args.command == "ats-shadow-settle":
        cmd_ats_shadow_settle()
    elif args.command == "ats-cycle":
        cmd_ats_cycle(skip_odds=args.skip_odds)
    elif args.command == "research-board":
        cmd_research(args.limit, league=args.league, slate=args.slate)
    elif args.command == "research-monitor":
        cmd_monitor(args.limit, league=args.league, force=args.force)
    elif args.command == "prediction-review":
        cmd_prediction_review(args.limit, league=args.league)
    elif args.command == "research-reviews":
        cmd_research_reviews(args.limit)
    elif args.command == "settlement-improve":
        cmd_settlement_improve(args.limit, league=args.league, ai=args.ai)


if __name__ == "__main__":
    main()
