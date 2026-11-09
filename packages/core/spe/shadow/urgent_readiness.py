"""Urgent live-weekend readiness remediation.

No new models. No Validated Play. Does not fabricate odds, forecasts, or settlements.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from spe.config import settings
from spe.db.base import as_utc
from spe.ingest.snapshots import remaining_credits
from spe.ops.freshness import job_execution_health, odds_freshness_health, operations_health
from spe.shadow.dry_run import run_settlement_dry_run
from spe.shadow.jobs import job_atd_candidates, job_settle
from spe.shadow.live_weekend import (
    family_of,
    freeze_coverage,
    scheduler_status,
    weekend_games,
)


def report_dir() -> Path:
    path = Path("data") / "atd_sim_dev" / "live_weekend"
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_near_kickoff_stale_games(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Identify games with stale Anytime TD (or any) prices inside the near-kickoff window."""
    now = now or datetime.now(UTC)
    rows = session.execute(
        text(
            """
            SELECT o.game_id, g.league_id, g.kickoff_utc, g.status, g.data_tier,
                   g.home_team_id, g.away_team_id,
                   t.bookmaker_id, o.market_id, MAX(t.captured_at) AS latest_at
            FROM odds_ticks t
            JOIN outcomes o ON o.outcome_id = t.outcome_id
            JOIN games g ON g.game_id = o.game_id
            WHERE g.data_tier != 'demo'
              AND g.kickoff_utc >= :start
              AND g.kickoff_utc <= :end
            GROUP BY o.game_id, g.league_id, g.kickoff_utc, g.status, g.data_tier,
                     g.home_team_id, g.away_team_id, t.bookmaker_id, o.market_id
            """
        ),
        {
            "start": now - timedelta(hours=3),
            "end": now + timedelta(hours=settings.odds_upcoming_hours),
        },
    ).mappings().all()

    by_game: dict[str, dict[str, Any]] = {}
    for row in rows:
        latest = as_utc(row["latest_at"])
        kick = as_utc(row["kickoff_utc"])
        if latest is None or kick is None:
            continue
        age = (now - latest).total_seconds() / 60.0
        from spe.market.price_age import max_fresh_minutes

        if age <= max_fresh_minutes(kick, now):
            continue
        if (kick - now) > timedelta(hours=6):
            continue
        gid = str(row["game_id"])
        bucket = by_game.setdefault(
            gid,
            {
                "game_id": gid,
                "league_id": row["league_id"],
                "kickoff_utc": kick,
                "status": row["status"],
                "home_team_id": row["home_team_id"],
                "away_team_id": row["away_team_id"],
                "hours_to_kick": (kick - now).total_seconds() / 3600.0,
                "started": kick <= now or str(row["status"]).lower() in {"in", "inprogress", "live", "halftime"},
                "stale_markets": set(),
                "stale_books": set(),
                "max_age_minutes": 0.0,
                "anytime_td_stale": False,
            },
        )
        bucket["stale_markets"].add(str(row["market_id"]))
        bucket["stale_books"].add(str(row["bookmaker_id"]))
        bucket["max_age_minutes"] = max(float(bucket["max_age_minutes"]), age)
        if row["market_id"] == "anytime_td":
            bucket["anytime_td_stale"] = True

    games = []
    for g in by_game.values():
        games.append(
            {
                **g,
                "stale_markets": sorted(g["stale_markets"]),
                "stale_books": sorted(g["stale_books"]),
                "kickoff_utc": g["kickoff_utc"].isoformat() if isinstance(g["kickoff_utc"], datetime) else str(g["kickoff_utc"]),
            }
        )
    games.sort(key=lambda r: (r["started"], r["hours_to_kick"]))
    pregame = [g for g in games if not g["started"]]
    started = [g for g in games if g["started"]]
    return {
        "near_kickoff_stale_game_count": len(games),
        "pregame_stale": len(pregame),
        "already_started_stale": len(started),
        "max_price_age_minutes": settings.max_price_age_minutes,
        "credits_remaining_budget": remaining_credits(session, "odds_api", settings.odds_credit_budget_per_day),
        "games": games,
        "pregame_games": pregame,
        "started_games": started,
    }


def refresh_stale_pregame_odds(session: Session) -> dict[str, Any]:
    """Refresh Odds API for near-kickoff stale games that have not started. Credit-aware."""
    from spe.ingest.odds_live import ingest_odds_live

    now = datetime.now(UTC)
    before = list_near_kickoff_stale_games(session, now=now)
    budget_before = before["credits_remaining_budget"]
    if budget_before <= 0:
        return {
            "status": "budget_exhausted",
            "before": before,
            "after": before,
            "resolved_pregame_games": 0,
            "credits_spent": 0,
            "fabricated": False,
        }

    # Prefer near/stale; ingest_odds_live already sorts by refresh priority.
    odds_summary = ingest_odds_live(session, now=now, horizon_hours=settings.odds_upcoming_hours)
    session.commit()
    after = list_near_kickoff_stale_games(session, now=datetime.now(UTC))

    before_ids = {g["game_id"] for g in before["pregame_games"]}
    after_ids = {g["game_id"] for g in after["pregame_games"]}
    resolved = sorted(before_ids - after_ids)
    still = sorted(before_ids & after_ids)

    samples = _verify_fresh_anytime_samples(session, resolved[:8])
    return {
        "status": odds_summary.get("status"),
        "odds_ingest": {
            "events_fetched": odds_summary.get("events_fetched"),
            "ticks_upserted": odds_summary.get("ticks_upserted"),
            "credits_spent": odds_summary.get("credits_spent"),
            "credits_remaining_budget": odds_summary.get("credits_remaining_budget"),
            "api_remaining": odds_summary.get("api_remaining"),
            "events_skipped": odds_summary.get("events_skipped"),
            "unmatched_players": odds_summary.get("unmatched_players"),
            "leagues": odds_summary.get("leagues"),
        },
        "before": {
            "near_kickoff_stale_game_count": before["near_kickoff_stale_game_count"],
            "pregame_stale": before["pregame_stale"],
            "already_started_stale": before["already_started_stale"],
            "games": before["games"][:30],
        },
        "after": {
            "near_kickoff_stale_game_count": after["near_kickoff_stale_game_count"],
            "pregame_stale": after["pregame_stale"],
            "already_started_stale": after["already_started_stale"],
        },
        "resolved_pregame_games": len(resolved),
        "resolved_game_ids": resolved,
        "still_stale_pregame": still,
        "still_stale_started_excluded": [g["game_id"] for g in before["started_games"]],
        "price_verification_samples": samples,
        "fabricated": False,
    }


def _verify_fresh_anytime_samples(session: Session, game_ids: list[str]) -> list[dict[str, Any]]:
    if not game_ids:
        return []
    rows = session.execute(
        text(
            """
            SELECT o.game_id, o.player_id, pl.name AS player_name, o.market_id, o.side,
                   t.bookmaker_id, t.price_american, t.captured_at, t.tick_id
            FROM outcomes o
            JOIN odds_ticks t ON t.outcome_id = o.outcome_id
            LEFT JOIN players pl ON pl.player_id = o.player_id
            WHERE o.game_id IN :gids AND o.market_id = 'anytime_td' AND o.side IN ('yes','over')
            """
        ).bindparams(bindparam("gids", expanding=True)),
        {"gids": game_ids},
    ).mappings().all()
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (str(r["game_id"]), str(r["player_id"]))
        prev = latest.get(key)
        if prev is None or as_utc(r["captured_at"]) >= as_utc(prev["captured_at"]):
            latest[key] = dict(r)
    out = []
    for (_gid, _pid), r in list(latest.items())[:12]:
        out.append(
            {
                "game_id": r["game_id"],
                "player_id": r["player_id"],
                "player": r["player_name"],
                "market_id": r["market_id"],
                "side": r["side"],
                "bookmaker_id": r["bookmaker_id"],
                "price_american": r["price_american"],
                "captured_at": str(r["captured_at"]),
                "tick_id": r["tick_id"],
            }
        )
    return out


def complete_pregame_freezes(session: Session, *, days: int = 4) -> dict[str, Any]:
    """Freeze missing M3/M5/M6 for not-started games; mark post-kickoff gaps as excluded."""
    now = datetime.now(UTC)
    games = weekend_games(session, now=now, days=days)
    before = freeze_coverage(session, games)

    # Capture append-only for still-pregame inventory.
    freeze_step = job_atd_candidates(session)
    session.commit()

    after_games = weekend_games(session, now=datetime.now(UTC), days=days)
    after = freeze_coverage(session, after_games)

    missing = after.get("missing_examples") or []
    # Recompute full missing list by family from by_league_family.
    by_lg = after.get("by_league_family") or {}
    core_missing_count = 0
    for lg, fams in by_lg.items():
        for fam in ("m3", "m5", "m6"):
            core_missing_count += int((fams.get(fam) or {}).get("missing") or 0)

    # Classify remaining core gaps: kickoff passed vs still pregame.
    classified = _classify_core_gaps(session, after_games, now=datetime.now(UTC))
    path = report_dir() / "missed_pregame_freezes.json"
    path.write_text(json.dumps(classified, indent=2, default=str), encoding="utf-8")

    return {
        "freeze_step": freeze_step,
        "before_core_missing": _core_missing(before),
        "after_core_missing": _core_missing(after),
        "resolved_core_freezes_approx": max(0, _core_missing(before) - _core_missing(after)),
        "by_league_family_after": after.get("by_league_family"),
        "field_issue_rows": after.get("field_issue_rows"),
        "classified_gaps": classified["summary"],
        "classified_path": str(path),
        "play_enabled": False,
        "backfilled_after_kickoff": False,
    }


def _core_missing(coverage: dict[str, Any]) -> int:
    by = coverage.get("missing_by_family") or {}
    if by:
        return int(by.get("m3") or 0) + int(by.get("m5") or 0) + int(by.get("m6") or 0)
    total = 0
    for fams in (coverage.get("by_league_family") or {}).values():
        for fam in ("m3", "m5", "m6"):
            total += int((fams.get(fam) or {}).get("missing") or 0)
    return total


def _classify_core_gaps(session: Session, games: list[dict[str, Any]], *, now: datetime) -> dict[str, Any]:
    if not games:
        return {"summary": {"pregame_still_missing": 0, "kickoff_passed_excluded": 0}, "rows": []}
    cov = freeze_coverage(session, games)
    # Build priced outcomes again for classification detail.
    gids = [g["game_id"] for g in games]
    kick_by = {g["game_id"]: as_utc(g["kickoff_utc"]) for g in games}
    # Sportsbook-priced only — exclude research: synthetic outcomes without odds ticks.
    priced = session.execute(
        text(
            """
            SELECT DISTINCT o.outcome_id, o.game_id, o.player_id, pl.name, g.league_id, g.kickoff_utc, g.status
            FROM outcomes o
            JOIN games g ON g.game_id = o.game_id
            JOIN odds_ticks t ON t.outcome_id = o.outcome_id
            LEFT JOIN players pl ON pl.player_id = o.player_id
            WHERE o.game_id IN :gids AND o.market_id = 'anytime_td' AND o.side IN ('yes','over')
              AND o.player_id IS NOT NULL
              AND o.outcome_id NOT LIKE 'research:%'
            """
        ).bindparams(bindparam("gids", expanding=True)),
        {"gids": gids},
    ).mappings().all()

    shadows = session.execute(
        text(
            """
            SELECT outcome_id, model_version_id, forecast_as_of
            FROM shadow_predictions
            WHERE market_id = 'anytime_td' AND game_id IN :gids
            """
        ).bindparams(bindparam("gids", expanding=True)),
        {"gids": gids},
    ).mappings().all()
    have: set[tuple[str, str]] = set()
    for s in shadows:
        fam = family_of(str(s["model_version_id"]))
        if fam in {"m3", "m5", "m6"}:
            kick = kick_by.get(str(s.get("outcome_id")))  # unused
            fas = as_utc(s["forecast_as_of"])
            # Only count pre-kickoff freezes; post-kickoff rows shouldn't exist.
            have.add((str(s["outcome_id"]), fam))

    rows = []
    for row in priced:
        oid = str(row["outcome_id"])
        kick = as_utc(row["kickoff_utc"])
        started = bool(kick and kick <= now) or str(row["status"]).lower() in {
            "in",
            "inprogress",
            "live",
            "halftime",
            "final",
            "post",
        }
        for fam in ("m3", "m5", "m6"):
            if (oid, fam) in have:
                # Still require freeze before kickoff when kick known.
                continue
            # Check if any shadow exists for fam on this outcome with pre-kickoff time
            present = False
            for s in shadows:
                if str(s["outcome_id"]) != oid:
                    continue
                if family_of(str(s["model_version_id"])) != fam:
                    continue
                fas = as_utc(s["forecast_as_of"])
                if kick and fas and fas >= kick:
                    continue
                present = True
                break
            if present:
                continue
            status = "excluded_kickoff_passed" if started else "still_missing_pregame"
            rows.append(
                {
                    "outcome_id": oid,
                    "player_id": row["player_id"],
                    "player": row["name"],
                    "game_id": row["game_id"],
                    "league_id": row["league_id"],
                    "family": fam,
                    "kickoff_utc": str(row["kickoff_utc"]),
                    "status": status,
                    "prospective_eval": status == "still_missing_pregame",
                    "note": (
                        "Do not backfill after kickoff"
                        if started
                        else "Eligible for pregame freeze if model can score player"
                    ),
                }
            )

    summary = {
        "pregame_still_missing": sum(1 for r in rows if r["status"] == "still_missing_pregame"),
        "kickoff_passed_excluded": sum(1 for r in rows if r["status"] == "excluded_kickoff_passed"),
        "by_family_pregame": dict(Counter(r["family"] for r in rows if r["status"] == "still_missing_pregame")),
        "by_family_excluded": dict(Counter(r["family"] for r in rows if r["status"] == "excluded_kickoff_passed")),
    }
    return {"summary": summary, "rows": rows, "coverage_snapshot": cov.get("by_league_family")}


def classify_sim_gaps(session: Session, *, days: int = 4) -> dict[str, Any]:
    """Explain missing atd_sim_v1 freezes without inventing predictions."""
    from spe.db.models import Game
    from spe.simulate.correlated_atd.examples import simulate_game_example
    from spe.simulate.correlated_atd.player_weights import build_opportunities
    from spe.simulate.correlated_atd.team_adapter import TeamTdV1Provider

    now = datetime.now(UTC)
    games = weekend_games(session, now=now, days=days)
    if not games:
        return {"n_games": 0}
    gids = [g["game_id"] for g in games]
    priced = session.execute(
        text(
            """
            SELECT DISTINCT o.outcome_id, o.game_id, o.player_id, pl.name, pl.position_group, g.league_id
            FROM outcomes o
            JOIN games g ON g.game_id = o.game_id
            LEFT JOIN players pl ON pl.player_id = o.player_id
            WHERE o.game_id IN :gids AND o.market_id = 'anytime_td' AND o.side IN ('yes','over')
              AND o.player_id IS NOT NULL
            """
        ).bindparams(bindparam("gids", expanding=True)),
        {"gids": gids},
    ).mappings().all()

    sim_frozen = {
        str(r[0])
        for r in session.execute(
            text(
                """
                SELECT DISTINCT outcome_id FROM shadow_predictions
                WHERE market_id = 'anytime_td' AND game_id IN :gids
                  AND model_version_id LIKE 'atd_sim_v1%'
                """
            ).bindparams(bindparam("gids", expanding=True)),
            {"gids": gids},
        ).all()
    }

    provider = TeamTdV1Provider(session)
    reasons: Counter[str] = Counter()
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_game_checked = 0

    # Group priced by game for batch sim.
    by_game: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in priced:
        by_game[str(row["game_id"])].append(dict(row))

    for gid, rows in by_game.items():
        game = session.get(Game, gid)
        if game is None:
            for row in rows:
                if str(row["outcome_id"]) in sim_frozen:
                    continue
                reasons["game_row_missing"] += 1
            continue
        kick = as_utc(game.kickoff_utc)
        started = bool(kick and kick <= now)
        ctx = provider.score_game(game)
        opp_ids: set[str] = set()
        if ctx:
            opp_ids = {p.player_id for p in build_opportunities(session, game, ctx)}
        example = simulate_game_example(session, game, provider, draw_count=64, seed=42)
        top24_ids = {p["player_id"] for p in (example.get("players") or [])}
        by_game_checked += 1

        for row in rows:
            oid = str(row["outcome_id"])
            if oid in sim_frozen:
                continue
            pid = str(row["player_id"])
            pos = (row.get("position_group") or "").upper()
            if started:
                reason = "kickoff_passed_no_backfill"
            elif example.get("error") == "team_td_model_unavailable":
                reason = "model_failure_team_td_unavailable"
            elif example.get("error") == "no_eligible_skill_players":
                reason = "missing_opportunity_data_no_eligible_skill"
            elif pos and pos not in {"RB", "WR", "TE", "QB", "FB"}:
                reason = "expected_exclusion_non_skill_position"
            elif pid not in opp_ids:
                reason = "not_in_eligible_opportunity_roster"
            elif pid not in top24_ids:
                reason = "expected_exclusion_top24_capture_truncation"
            else:
                reason = "missing_eligible_player_unexpected"
            reasons[reason] += 1
            if len(examples[reason]) < 8:
                examples[reason].append(
                    {
                        "outcome_id": oid,
                        "player_id": pid,
                        "player": row.get("name"),
                        "game_id": gid,
                        "league_id": row.get("league_id"),
                        "position_group": pos,
                    }
                )

    expected = sum(
        int(reasons[k])
        for k in reasons
        if str(k).startswith("expected_") or k == "kickoff_passed_no_backfill"
    )
    return {
        "priced_anytime_outcomes": len(priced),
        "sim_frozen_outcomes": len(sim_frozen),
        "missing_sim_outcomes": sum(reasons.values()),
        "expected_exclusions": expected,
        "actionable_gaps": sum(reasons.values()) - expected,
        "games_checked": by_game_checked,
        "reason_counts": dict(reasons),
        "examples": dict(examples),
        "notes": [
            "Freeze path currently receives only top-24 sim players from simulate_game_example.",
            "No predictions invented to fill gaps.",
            "top24 truncation and non-skill / non-eligible roster are expected exclusions.",
        ],
        "play_enabled": False,
    }


def audit_fbs_2026_stats(session: Session) -> dict[str, Any]:
    """Diagnose missing FBS 2026 player-game stats without running a full re-ingest."""
    from spe.adapters.cfbd import CfbdAdapter
    from spe.ingest.cfbd_players import DEFAULT_SEASONS

    db_counts = session.execute(
        text(
            """
            SELECT
              CAST(strftime('%Y', g.kickoff_utc) AS INTEGER) AS yr,
              COUNT(*) AS n
            FROM player_game_stats pgs
            JOIN games g ON g.game_id = pgs.game_id
            WHERE g.league_id = 'fbs'
              AND g.kickoff_utc >= '2024-01-01'
            GROUP BY yr
            ORDER BY yr
            """
        )
    ).mappings().all()

    fbs_2026_games = session.execute(
        text(
            """
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN lower(status) IN ('final','post') THEN 1 ELSE 0 END) AS finals
            FROM games
            WHERE league_id = 'fbs' AND kickoff_utc >= '2026-08-01'
            """
        )
    ).mappings().first()

    coverage = session.execute(
        text(
            """
            SELECT dataset, season_year, status, row_count, notes, last_ingested_at
            FROM data_coverage
            WHERE provider = 'cfbd' AND dataset LIKE '%player%'
            ORDER BY season_year DESC, last_ingested_at DESC
            LIMIT 20
            """
        )
    ).mappings().all()

    seasons_configured = list(DEFAULT_SEASONS)
    includes_2026 = 2026 in seasons_configured

    api_probe: dict[str, Any] = {"attempted": False}
    adapter = CfbdAdapter()
    if adapter.api_key:
        api_probe["attempted"] = True
        try:
            # Minimal probe: week 1 regular 2026 box — do not loop the season.
            result = adapter.fetch(
                "games_players",
                {"year": 2026, "week": 1, "seasonType": "regular", "classification": "fbs"},
            )
            n = len(result.records or [])
            api_probe.update(
                {
                    "endpoint": "games_players",
                    "params": {"year": 2026, "week": 1, "seasonType": "regular"},
                    "records_returned": n,
                    "calls_made": adapter.calls_made,
                    "status": "api_has_data" if n else "api_empty_or_unavailable",
                    "sample_keys": list((result.records[0] or {}).keys())[:12] if n else [],
                }
            )
        except Exception as exc:  # noqa: BLE001
            api_probe.update({"status": "api_error", "error": str(exc)[:400]})
    else:
        api_probe["status"] = "needs_cfbd_api_key"

    root_cause = []
    if not includes_2026:
        root_cause.append(
            {
                "code": "INGEST_DEFAULT_SEASONS_EXCLUDES_2026",
                "detail": f"cfbd_players.DEFAULT_SEASONS={seasons_configured[0]}..{seasons_configured[-1]} (range stops before 2026)",
                "class": "ingestion_issue",
            }
        )
    if int((fbs_2026_games or {}).get("n") or 0) > 0 and not any(int(r["yr"] or 0) == 2026 for r in db_counts):
        root_cause.append(
            {
                "code": "DB_HAS_2026_GAMES_ZERO_PGS",
                "detail": "FBS 2026 games exist in DB but player_game_stats has 0 rows for those kickoffs",
                "class": "ingestion_issue",
            }
        )
    if api_probe.get("status") == "api_has_data":
        root_cause.append(
            {
                "code": "CFBD_API_RETURNS_2026_BOX",
                "detail": f"Probe returned {api_probe.get('records_returned')} records for 2026 week 1",
                "class": "not_api_coverage_gap",
            }
        )
    elif api_probe.get("status") == "api_empty_or_unavailable":
        root_cause.append(
            {
                "code": "CFBD_API_EMPTY_FOR_2026_W1",
                "detail": "API returned 0 games_players rows for 2026 week 1 in this probe",
                "class": "api_coverage_issue",
            }
        )

    repair_plan = [
        {
            "step": 1,
            "action": "Extend DEFAULT_SEASONS / worker seasons to include 2026 (and current year)",
            "when": "After weekend live tracking — do not disrupt freeze/settle cadence",
            "risk": "low",
        },
        {
            "step": 2,
            "action": "Run targeted ingest_fbs_players(seasons=(2026,)) with CFBD budget, week-by-week",
            "when": "Off-peak after Sunday slate settles",
            "risk": "medium — CFBD call budget; resume-safe",
        },
        {
            "step": 3,
            "action": "Validate player_id mapping (fbs:{cfbd_id}) against existing roster/players",
            "when": "During ingest",
            "risk": "medium — mapping mismatches leave orphans",
        },
        {
            "step": 4,
            "action": "Recompute college model features that depend on current-season usage; do not retrain yet",
            "when": "After PGS land",
            "risk": "low if research-only",
        },
        {
            "step": 5,
            "action": "Re-evaluate FBS shadow accuracy only on games with complete boxes",
            "when": "After feature refresh",
            "risk": "low",
        },
    ]

    return {
        "pgs_counts_by_year_since_2024": [dict(r) for r in db_counts],
        "fbs_2026_games": dict(fbs_2026_games or {}),
        "default_seasons": seasons_configured,
        "default_seasons_includes_2026": includes_2026,
        "coverage_rows": [dict(r) for r in coverage],
        "api_probe": api_probe,
        "root_cause": root_cause,
        "repair_plan": repair_plan,
        "do_not_run_now": "Full 2026 ingest deferred to avoid disrupting live weekend tracking",
    }


def settle_finished_games(session: Session) -> dict[str, Any]:
    """Settle finals only. Missing player stats remain blocked, not zero."""
    before_pending = session.execute(
        text(
            """
            SELECT COUNT(*) FROM shadow_predictions s
            LEFT JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
            WHERE s.market_id = 'anytime_td' AND st.shadow_id IS NULL
            """
        )
    ).scalar()
    step = job_settle(session)
    session.commit()
    by_result = session.execute(
        text(
            """
            SELECT st.result, COUNT(*) AS n
            FROM shadow_settlements st
            JOIN shadow_predictions s ON s.shadow_id = st.shadow_id
            WHERE s.market_id = 'anytime_td'
              AND st.settled_at >= datetime('now', '-1 day')
            GROUP BY st.result
            """
        )
    ).mappings().all()
    blocked_reasons = session.execute(
        text(
            """
            SELECT st.void_reason, COUNT(*) AS n
            FROM shadow_settlements st
            JOIN shadow_predictions s ON s.shadow_id = st.shadow_id
            WHERE s.market_id = 'anytime_td'
              AND st.result = 'blocked'
              AND st.settled_at >= datetime('now', '-1 day')
            GROUP BY st.void_reason
            """
        )
    ).mappings().all()
    return {
        "settle_step": step,
        "pending_anytime_before": int(before_pending or 0),
        "recent_results": [dict(r) for r in by_result],
        "blocked_reasons": [dict(r) for r in blocked_reasons],
        "rule": "missing_box_score_not_assumed_zero",
        "fabricated": False,
        "play_enabled": False,
    }


def verify_automation(session: Session) -> dict[str, Any]:
    health = job_execution_health(session)
    sched = scheduler_status()
    by_job = health.get("by_job") or {}
    required = ["odds_refresh", "pregame_forecasts", "shadow_capture", "shadow_settle", "shadow_report"]
    # ATD should appear after wiring; may still be manual-only until next cycle.
    status = {
        "scheduler": sched,
        "job_execution": health,
        "failures": health.get("failures"),
        "recent_failures": health.get("recent_failures"),
        "required_jobs_last_status": {
            name: (by_job.get(name) or {}).get("last_status") for name in required
        },
        "atd_candidate_capture": by_job.get("atd_candidate_capture"),
        "minutes_since_last_cycle": health.get("minutes_since_last_cycle"),
        "automation_ok": int(health.get("failures") or 0) == 0 and not sched.get("blocker"),
    }
    return status


def run_urgent_readiness(session: Session, *, days: int = 4, skip_odds: bool = False) -> dict[str, Any]:
    now = datetime.now(UTC)
    report: dict[str, Any] = {
        "title": "Urgent Live Weekend Readiness",
        "generated_at": now.isoformat(),
        "play_enabled": False,
        "validated_play": False,
        "research_only": True,
        "develop_new_models": False,
        "fabricate_results": False,
    }

    print("[urgent] P1 refresh stale odds…", flush=True)
    report["priority_1_stale_odds"] = (
        {"skipped": True, "snapshot": list_near_kickoff_stale_games(session)}
        if skip_odds
        else refresh_stale_pregame_odds(session)
    )

    print("[urgent] P2 complete pregame freezes…", flush=True)
    report["priority_2_freezes"] = complete_pregame_freezes(session, days=days)

    print("[urgent] P3 verify automation…", flush=True)
    report["priority_3_automation"] = verify_automation(session)

    print("[urgent] P4 classify sim gaps…", flush=True)
    report["priority_4_sim_coverage"] = classify_sim_gaps(session, days=days)

    print("[urgent] P5 FBS 2026 audit (no full ingest)…", flush=True)
    report["priority_5_fbs_2026"] = audit_fbs_2026_stats(session)

    print("[urgent] P6 settle finished games…", flush=True)
    report["priority_6_settlements"] = settle_finished_games(session)

    report["operations_health"] = operations_health(session)
    report["odds_freshness_after"] = odds_freshness_health(session)
    report["dry_run_still_pass"] = run_settlement_dry_run().get("all_pass")
    report["after_weekend_questions"] = {
        "ready_command": "weekend-atd-results",
        "questions": [
            "How many TD predictions were made before kickoff?",
            "How many were successfully settled?",
            "Which models predicted outcomes most accurately?",
            "Which models had the best probability calibration?",
            "Did apparent value opportunities outperform sportsbook prices?",
            "Did strong model disagreements (e.g. Ja'Marr Chase) inform reliability?",
        ],
        "note": "Generate after finals settle; uses deduped pre-kickoff units only.",
    }

    p1 = report["priority_1_stale_odds"]
    p2 = report["priority_2_freezes"]
    report["executive_summary"] = {
        "stale_odds_resolved": p1.get("resolved_pregame_games"),
        "still_stale_pregame": len(p1.get("still_stale_pregame") or []),
        "missing_freezes_core_after": p2.get("after_core_missing"),
        "kickoff_passed_excluded": (p2.get("classified_gaps") or {}).get("kickoff_passed_excluded"),
        "pregame_still_missing": (p2.get("classified_gaps") or {}).get("pregame_still_missing"),
        "automation_ok": (report["priority_3_automation"] or {}).get("automation_ok"),
        "sim_gap_reasons": (report["priority_4_sim_coverage"] or {}).get("reason_counts"),
        "fbs_root_cause_codes": [r.get("code") for r in (report["priority_5_fbs_2026"] or {}).get("root_cause") or []],
        "settlements": (report["priority_6_settlements"] or {}).get("settle_step"),
        "near_kickoff_stale_rows_after": (report["odds_freshness_after"] or {}).get("near_kickoff_stale_rows"),
    }

    path = report_dir() / "urgent_live_weekend_readiness.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    return report
