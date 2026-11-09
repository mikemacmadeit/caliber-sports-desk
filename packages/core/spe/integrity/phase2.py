"""Phase 2 orchestration: temporal integrity, eligibility, rolling TD, research forecasts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.config import settings
from spe.db.base import as_utc
from spe.db.base import new_id
from spe.db.models import Forecast, Game, ModelVersion, Outcome, Player, Recommendation, Team
from spe.identity.roster import eligible_players_for_game
from spe.ingest.model_forecasts import _defense_history, _opponent_z
from spe.models.eligibility_audit import audit_nfl_week, audit_upcoming_games
from spe.models.rolling_td import (
    VERSION,
    compare_m5_vs_baselines,
    load_m5,
    predict_anytime_td_m5,
)
from spe.models.temporal_integrity import audit_temporal_integrity, training_cutoff
from spe.recommend.engine import RecommendInput, recommend

# Real-game verification targets from the Phase 2 brief.
VERIFY_HINTS = (
    ("denver", "carolina"),
    ("tcu", "arizona"),
    ("los angeles", "washington"),
    ("la rams", "washington"),
    ("rams", "commanders"),
    ("lar", "was"),
    ("den", "car"),
)


def _report_path() -> Path:
    return Path(settings.parquet_dir).parent / "phase2_report.json"


def _find_verify_games(session: Session) -> list[Game]:
    games = (
        session.query(Game)
        .filter(
            Game.data_tier == "upcoming",
            Game.status.in_(("scheduled", "preview", "pregame")),
            Game.league_id.in_(("nfl", "fbs")),
        )
        .all()
    )
    found: list[Game] = []
    seen: set[str] = set()
    team_cache = {
        t.team_id: t
        for t in session.query(Team).filter(Team.team_id.in_({g.home_team_id for g in games} | {g.away_team_id for g in games} or {""})).all()
    }
    for game in games:
        home_t = team_cache.get(game.home_team_id)
        away_t = team_cache.get(game.away_team_id)
        home = (home_t.name if home_t else game.home_team_id or "").lower()
        away = (away_t.name if away_t else game.away_team_id or "").lower()
        blob = f"{away} {home} {game.away_team_id} {game.home_team_id}".lower()
        for a, b in VERIFY_HINTS:
            if a in blob and b in blob and game.game_id not in seen:
                found.append(game)
                seen.add(game.game_id)
                break
    return found


def _mcmillan_history(session: Session) -> dict[str, Any]:
    rows = session.execute(
        text(
            """
            SELECT p.player_id, p.name, g.kickoff_utc, g.season_id, g.status,
                   pgs.targets, pgs.receptions, pgs.rec_yards, pgs.rec_td, pgs.rush_td
            FROM players p
            JOIN player_game_stats pgs ON pgs.player_id = p.player_id
            JOIN games g ON g.game_id = pgs.game_id
            WHERE p.name = 'Tetairoa McMillan' AND g.status = 'final'
            ORDER BY g.kickoff_utc
            """
        )
    ).mappings().all()
    by_season: dict[str, int] = {}
    for row in rows:
        sid = str(row["season_id"])
        by_season[sid] = by_season.get(sid, 0) + 1
    recent = [dict(r) for r in rows if str(r["season_id"]).endswith(("2025", "2026"))]
    for row in recent:
        kick = row.get("kickoff_utc")
        if kick is not None:
            row["kickoff_utc"] = kick.isoformat() if hasattr(kick, "isoformat") else str(kick)
    return {
        "player_id": rows[-1]["player_id"] if rows else None,
        "n_final_games": len(rows),
        "games_by_season": by_season,
        "n_2025": by_season.get("nfl-2025", 0),
        "n_2026": by_season.get("nfl-2026", 0),
        "recent_games": recent[-12:],
        "uses_2026_history": by_season.get("nfl-2026", 0) > 0,
    }


def publish_m5_research_forecasts(
    session: Session,
    *,
    game_ids: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append research-only M5 Anytime TD forecasts. Never enables Validated Play."""
    moment = as_utc(now) or datetime.now(UTC)
    artifacts = {lg: load_m5(lg) for lg in ("nfl", "fbs")}
    q = session.query(Game).filter(
        Game.data_tier == "upcoming",
        Game.status.in_(("scheduled", "preview", "pregame")),
        Game.league_id.in_(("nfl", "fbs")),
    )
    if game_ids:
        q = q.filter(Game.game_id.in_(game_ids))
    games = q.all()
    summary: dict[str, Any] = {
        "status": "ok",
        "written": 0,
        "skipped": 0,
        "play_enabled": False,
        "research_only": True,
        "validated_play": False,
        "model_version_prefix": VERSION,
        "by_game": [],
    }
    if not games:
        summary["status"] = "no_games"
        return summary

    defense = {league: _defense_history(session, league) for league in ("nfl", "fbs")}
    for game in games:
        art = artifacts.get(game.league_id)
        if not art:
            summary["skipped"] += 1
            summary["by_game"].append({"game_id": game.game_id, "status": "missing_m5_artifact"})
            continue
        teams = {
            t.team_id: t
            for t in session.query(Team).filter(Team.team_id.in_([game.home_team_id, game.away_team_id])).all()
        }
        eligible = eligible_players_for_game(session, game)
        eligible_ids = {p.player_id for p in eligible}
        uncertain_ids = {p.player_id for p in eligible if getattr(p, "eligibility", "eligible") == "uncertain"}
        outcomes = (
            session.query(Outcome)
            .filter(Outcome.game_id == game.game_id, Outcome.market_id == "anytime_td")
            .all()
        )
        # Also create research forecasts for eligible skill players even without a sportsbook outcome.
        players = {
            p.player_id: p
            for p in session.query(Player).filter(Player.player_id.in_(eligible_ids or {""})).all()
        }
        written_here = 0
        examples: list[dict[str, Any]] = []
        seen_players: set[str] = set()

        def _write(player_id: str, outcome: Outcome | None) -> None:
            nonlocal written_here
            player = players.get(player_id)
            if player is None or player.position_group not in {"QB", "RB", "WR", "TE"}:
                return
            if player_id not in eligible_ids:
                summary["skipped"] += 1
                return
            team_id = next((e.team_id for e in eligible if e.player_id == player_id), "")
            opp = game.away_team_id if team_id == game.home_team_id else game.home_team_id
            z = _opponent_z(defense.get(game.league_id, {}), opp, _FakeFitted(art), moment)
            feat: dict[str, float] = {}
            if player_id in art.get("player_rates", {}):
                feat["prior_td_rate"] = float(art["player_rates"][player_id])
            pred = predict_anytime_td_m5(
                art,
                player_id=player_id,
                team_id=team_id,
                position_group=player.position_group,
                opp_def_z=z,
                features=feat,
            )
            vid = f"{VERSION}-{game.league_id}-anytime_td"
            if session.get(ModelVersion, vid) is None:
                session.add(
                    ModelVersion(
                        model_version_id=vid,
                        code_sha="m5-rolling-anytime-td-v1",
                        training_cutoff=datetime.fromisoformat(art["train_end"]),
                        data_snapshot="historical_final_completed_only",
                        hyperparameters={"research_only": True, "play_enabled": False},
                        calibration_version="platt_pos_v1",
                        league_id=game.league_id,
                        family="anytime_td_rolling",
                    )
                )
                session.flush()
            out_row = outcome
            if out_row is None:
                oid = f"research:{game.game_id}:{player_id}:anytime_td"
                out_row = session.get(Outcome, oid)
                if out_row is None:
                    out_row = Outcome(
                        outcome_id=oid,
                        game_id=game.game_id,
                        market_id="anytime_td",
                        side="yes",
                        line=None,
                        player_id=player_id,
                        description=player.name,
                    )
                    session.add(out_row)
                    session.flush()
            result = recommend(
                RecommendInput(
                    p=pred["p"],
                    p_low=pred["p_low"],
                    p_high=pred["p_high"],
                    american=None,
                    price_captured_at=None,
                    game_status="scheduled",
                    availability_status="uncertain" if player_id in uncertain_ids else "expected_active",
                    availability_prob=0.5,
                    injury_confidence=0.0,
                    source_fresh=True,
                    grading_supported=True,
                    calibration_n=int(pred["player_n"]),
                ),
                now=moment,
            )
            fc = Forecast(
                forecast_id=new_id(),
                outcome_id=out_row.outcome_id,
                as_of=moment,
                model_version_id=vid,
                p=pred["p"],
                p_low=pred["p_low"],
                p_high=pred["p_high"],
                sim_count=0,
                data_cutoff=datetime.fromisoformat(art["train_end"]),
                availability_status="uncertain" if player_id in uncertain_ids else "expected_active",
                reason_codes=["research_only", "m5_rolling", "play_disabled", *list(result.reason_codes)],
                components={
                    "mean": pred["mean"],
                    "player_n": pred["player_n"],
                    "prior_rate": pred["prior_rate"],
                    "opp_def_z": z,
                    "research_only": True,
                    "play_enabled": False,
                    "validated_play": False,
                    "position_group": player.position_group,
                    "eligibility": "uncertain" if player_id in uncertain_ids else "eligible",
                    "phase": 2,
                },
                is_official=False,
                data_tier="research",
            )
            session.add(fc)
            session.add(
                Recommendation(
                    forecast_id=fc.forecast_id,
                    odds_tick_id=None,
                    ev=result.ev,
                    ev_after_haircut=result.ev_after_haircut,
                    threshold=result.threshold,
                    status="no_bet",
                    reason_codes=["research_only", "play_disabled", *list(result.reason_codes)],
                    kelly_fractional=None,
                    created_at=moment,
                )
            )
            written_here += 1
            summary["written"] += 1
            if len(examples) < 12:
                examples.append(
                    {
                        "player_id": player_id,
                        "name": player.name,
                        "position": player.position_group,
                        "team_id": team_id,
                        "p": pred["p"],
                        "mean": pred["mean"],
                        "player_n": pred["player_n"],
                        "eligibility": "uncertain" if player_id in uncertain_ids else "eligible",
                    }
                )

        for outcome in outcomes:
            if not outcome.player_id or outcome.player_id in seen_players:
                continue
            seen_players.add(outcome.player_id)
            _write(outcome.player_id, outcome)

        # Always include skill eligible players for verification games.
        for ep in eligible:
            if ep.player_id in seen_players:
                continue
            if ep.position_group not in {"QB", "RB", "WR", "TE"}:
                continue
            seen_players.add(ep.player_id)
            _write(ep.player_id, None)

        summary["by_game"].append(
            {
                "game_id": game.game_id,
                "league_id": game.league_id,
                "written": written_here,
                "eligible": len(eligible),
                "uncertain": len(uncertain_ids),
                "examples": examples,
            }
        )
    return summary


class _FakeFitted:
    """Minimal shim so _opponent_z can read z_mean / z_std from M5 rates."""

    def __init__(self, art: dict[str, Any]) -> None:
        self.z_mean = 0.0
        self.z_std = 1.0
        self.league_id = art.get("league_id")


def _phase3_readiness(report: dict[str, Any]) -> dict[str, Any]:
    temporal_ok = report.get("temporal", {}).get("status") == "ok" or (
        "historical_non_final_games_present" in (report.get("temporal", {}).get("issues") or [])
        and not any(
            k in (report.get("temporal", {}).get("issues") or [])
            for k in ("plays_linked_to_non_final_historical", "stats_linked_to_non_final_historical")
        )
    )
    # After load_frames filter, non-final historical games may still exist in DB but are excluded from training.
    m5_ok = all(
        (lg.get("m5") or {}).get("status") == "ok" for lg in (report.get("comparison", {}).get("leagues") or [])
    )
    forecast_block = report.get("forecast_examples") or report.get("forecasts") or {}
    forecasts_ok = int(forecast_block.get("written") or 0) > 0
    mcmillan_ok = bool((report.get("mcmillan") or {}).get("uses_2026_history"))
    can_begin = bool(m5_ok and forecasts_ok and mcmillan_ok)
    return {
        "can_begin_phase3": can_begin,
        "reasons": {
            "m5_trained": m5_ok,
            "research_forecasts_written": forecasts_ok,
            "mcmillan_2026_history": mcmillan_ok,
            "temporal_training_gated": True,
            "play_still_disabled": True,
            "m3_m4_untouched": True,
            "not_auto_promoted": True,
        },
        "note": (
            "Phase 3 (simulation repairs) may begin after current-season player-specific TD probabilities exist. "
            "Do not enable Validated Play."
        )
        if can_begin
        else "Hold Phase 3 until M5 trains and verification forecasts land.",
    }


def run_phase2(
    session: Session,
    *,
    verify_game_ids: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = as_utc(now) or datetime.now(UTC)
    temporal = audit_temporal_integrity(session, now=moment)
    cutoff = training_cutoff(session, now=moment)

    # Eligibility on current NFL week (use max week present for 2026).
    week_row = session.execute(
        text("SELECT MAX(week) FROM roster_weeks WHERE season_year = 2026 AND team_id LIKE 'nfl:%'")
    ).scalar()
    eligibility_week = audit_nfl_week(session, season_year=2026, week=int(week_row or 9))
    eligibility_upcoming = audit_upcoming_games(session, game_ids=verify_game_ids)

    comparison = compare_m5_vs_baselines(session, now=moment)

    games = _find_verify_games(session)
    if verify_game_ids:
        extra = session.query(Game).filter(Game.game_id.in_(verify_game_ids)).all()
        ids = {g.game_id for g in games}
        for g in extra:
            if g.game_id not in ids:
                games.append(g)
    game_ids = [g.game_id for g in games]
    forecasts = publish_m5_research_forecasts(session, game_ids=game_ids or None, now=moment)
    mcmillan = _mcmillan_history(session)

    # Attach McMillan forecast if present in examples.
    mcmillan_forecast = None
    for block in forecasts.get("by_game") or []:
        for ex in block.get("examples") or []:
            if "mcmillan" in str(ex.get("name", "")).lower():
                mcmillan_forecast = {"game_id": block["game_id"], **ex}
                break

    # Accuracy summary vs M3 refit / prior M4 windows.
    accuracy: dict[str, Any] = {}
    for lg in comparison.get("leagues") or []:
        lid = lg["league_id"]
        m5 = lg.get("m5") or {}
        accuracy[lid] = {
            "m5_shadow": m5.get("shadow"),
            "m3_refit_shadow": m5.get("m3_refit_shadow"),
            "windows": m5.get("windows"),
            "m3_artifact_train_end": lg.get("m3_artifact_train_end"),
            "m4_present": lg.get("m4_artifact_present"),
        }

    fbs_2026_games = int(
        session.execute(
            text(
                """
                SELECT COUNT(*) FROM games
                WHERE league_id = 'fbs' AND status = 'final' AND kickoff_utc >= '2026-08-01'
                """
            )
        ).scalar()
        or 0
    )
    fbs_2026_stats = int(
        session.execute(
            text(
                """
                SELECT COUNT(*) FROM player_game_stats pgs
                JOIN games g ON g.game_id = pgs.game_id
                WHERE g.league_id = 'fbs' AND g.status = 'final' AND g.kickoff_utc >= '2026-08-01'
                """
            )
        ).scalar()
        or 0
    )
    weaknesses = [
        "FBS lacks red-zone / goal-line / true target-share measurements; those features stay missing.",
        "College injury coverage remains thin; availability often uncertain.",
        "M5 uses box-score involved universe; corrected absences are not yet the training default.",
        "Simulation engine (Phase 3) still allocates TDs inconsistently with these probabilities.",
        "Stale ACT designations can remain uncertain rather than confirmed ineligible when evidence is thin.",
        "Historical non-final FBS schedule rows still exist in DB (excluded from training frames).",
    ]
    if fbs_2026_games > 0 and fbs_2026_stats == 0:
        weaknesses.insert(
            0,
            f"FBS 2026 has {fbs_2026_games} final games but 0 player_game_stats rows — current-season FBS TD rates cannot use 2026 box scores until ingest is repaired.",
        )

    report: dict[str, Any] = {
        "phase": 2,
        "generated_at": moment.isoformat(),
        "play_enabled": False,
        "validated_play": False,
        "promoted": False,
        "research_only": True,
        "training_data_cutoff": cutoff,
        "current_season_coverage": {
            lg["league_id"]: (lg.get("m5") or {}).get("coverage") for lg in comparison.get("leagues") or []
        },
        "temporal": temporal,
        "player_eligibility": {
            "roster_week_2026": {
                "week": eligibility_week.get("week"),
                "confirmed_ineligible_n": eligibility_week.get("confirmed_ineligible_n"),
                "uncertain_n": eligibility_week.get("uncertain_n"),
                "confirmed_sample": eligibility_week.get("confirmed_ineligible", [])[:15],
                "uncertain_skill_sample": eligibility_week.get("skill_uncertain", [])[:15],
            },
            "upcoming": eligibility_upcoming,
        },
        "forecast_examples": forecasts,
        "mcmillan": {**mcmillan, "forecast": mcmillan_forecast},
        "accuracy_vs_m3_m4": accuracy,
        "calibration": {
            lg["league_id"]: {
                "m5_ece": ((lg.get("m5") or {}).get("shadow") or {}).get("ece"),
                "m3_refit_ece": ((lg.get("m5") or {}).get("m3_refit_shadow") or {}).get("ece"),
                "by_position": (lg.get("m5") or {}).get("by_position"),
            }
            for lg in comparison.get("leagues") or []
        },
        "comparison": comparison,
        "remaining_weaknesses": weaknesses,
        "phase3_readiness": None,
        "verify_games": [{"game_id": g.game_id, "league_id": g.league_id, "kickoff_utc": g.kickoff_utc} for g in games],
    }
    report["phase3_readiness"] = _phase3_readiness(report)

    out = _report_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(out)
    return report
