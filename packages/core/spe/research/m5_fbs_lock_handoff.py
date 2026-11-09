"""Lock C_recent candidate, verify eligibility/loader, paired M5 freezes, Agent 1 handoff.

Does not promote. Does not overwrite protected fbs_m5.json.
Does not invent eligibility or post-kickoff freezes. Stops further tuning.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.db.models import Game
from spe.identity.roster import eligible_players_for_game, roster_summary, season_year_for_game
from spe.models.advanced import load_model_frame
from spe.models.rolling_td import artifact_path, load_m5, prepare_td_frame
from spe.research.atd_forecast import forecast_upcoming, predict_m5_strict
from spe.research.m5_fbs_refit import (
    PIPELINE_VERSION,
    RECENT_MODEL_ID,
    RESEARCH_MODEL_ID,
    out_dir,
)

SKILL = {"QB", "RB", "WR", "TE"}
CANDIDATE_ID = "C_recent"
CANDIDATE_FAMILY = "m5_c_recent"
LOCKED_VERSION = "fbs_m5_c_recent_locked_v1"
# Soft upper bound: season-filtered FBS skill roster is ~80–160; multi-year dumps were 1000+.
MAX_ELIGIBLE_WARN = 220
MAX_ELIGIBLE_FAIL = 500


def _sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _c_artifact_path() -> Path:
    return out_dir() / f"{RECENT_MODEL_ID}.json"


def _lock_dir() -> Path:
    path = out_dir() / "locked"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _handoff_dir() -> Path:
    path = out_dir() / "agent1_handoff"
    path.mkdir(parents=True, exist_ok=True)
    return path


def verify_shared_loader_regressions(session: Session) -> dict[str, Any]:
    """Confirm load_model_frame priors are computed on the involved universe only."""
    checks: dict[str, Any] = {}
    # FBS: priors after involved filter ⇒ live hist mean matches training prior_td_rate.
    frame = prepare_td_frame(session, "fbs", year_max=2026)
    if frame.empty:
        return {"status": "empty_fbs_frame"}
    ko = pd.to_datetime(frame["kickoff_utc"], utc=True)
    pool = frame[(ko.dt.year == 2026) & frame["prior_td_rate"].notna()]
    sample = pool.sample(n=min(30, len(pool)), random_state=11) if len(pool) else frame.head(0)
    max_delta = 0.0
    for rec in sample.itertuples(index=False):
        sub = frame[frame["player_id"] == rec.player_id].sort_values("kickoff_utc")
        expand = pd.to_numeric(sub["anytime_tds"], errors="coerce").shift(1).expanding(min_periods=1).mean()
        idx = sub.index[sub["game_id"] == rec.game_id]
        if not len(idx):
            continue
        recompute = float(expand.loc[idx[0]]) if pd.notna(expand.loc[idx[0]]) else None
        stored = float(rec.prior_td_rate) if pd.notna(rec.prior_td_rate) else None
        if recompute is not None and stored is not None:
            max_delta = max(max_delta, abs(recompute - stored))
    checks["fbs_prior_td_matches_involved_expanding"] = max_delta < 1e-9
    checks["fbs_prior_td_max_abs_delta"] = float(max_delta)
    checks["fbs_sample_n"] = int(len(sample))

    # NFL smoke: involved filter still returns rows; priors present.
    nfl = load_model_frame(session, "nfl", year_max=2026, universe="involved")
    checks["nfl_involved_rows"] = int(len(nfl))
    checks["nfl_has_prior_td"] = bool(len(nfl) and "prior_td_rate" in nfl.columns and nfl["prior_td_rate"].notna().any())
    checks["nfl_loader_ok"] = checks["nfl_involved_rows"] > 1000 and checks["nfl_has_prior_td"]

    # Involved-only: zero-touch rows must not remain.
    if not frame.empty:
        touch = (
            frame["targets"].fillna(0)
            + frame["receptions"].fillna(0)
            + frame["carries"].fillna(0)
            + frame["rec_yards"].fillna(0).abs()
            + frame["rush_yards"].fillna(0).abs()
        )
        checks["fbs_zero_touch_rows_remaining"] = int((touch <= 0).sum())
        checks["fbs_no_zero_touch"] = checks["fbs_zero_touch_rows_remaining"] == 0

    checks["status"] = "ok"
    checks["pass"] = bool(
        checks.get("fbs_prior_td_matches_involved_expanding")
        and checks.get("nfl_loader_ok")
        and checks.get("fbs_no_zero_touch")
    )
    return checks


def verify_player_eligibility(session: Session, *, horizon_hours: int = 72) -> dict[str, Any]:
    """Upcoming FBS games: season-filtered eligibility, not multi-year dumps."""
    moment = datetime.now(UTC)
    until = moment + timedelta(hours=horizon_hours)
    games = (
        session.query(Game)
        .filter(
            Game.league_id == "fbs",
            Game.kickoff_utc > moment,
            Game.kickoff_utc <= until,
            Game.status.in_(["scheduled", "preview", "status_scheduled", "pre"]),
        )
        .order_by(Game.kickoff_utc)
        .limit(40)
        .all()
    )
    # Broader status filter if empty
    if not games:
        games = (
            session.query(Game)
            .filter(Game.league_id == "fbs", Game.kickoff_utc > moment, Game.kickoff_utc <= until)
            .order_by(Game.kickoff_utc)
            .limit(40)
            .all()
        )

    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    warnings: list[str] = []
    for g in games:
        summary = roster_summary(session, g)
        eligible = eligible_players_for_game(session, g)
        skill = [e for e in eligible if e.position_group in SKILL]
        n = len(eligible)
        n_skill = len(skill)
        hist = int(summary.get("historical_roster_table_rows") or 0)
        year = season_year_for_game(session, g)
        issue = None
        if n >= MAX_ELIGIBLE_FAIL:
            issue = "eligible_looks_like_multi_year_dump"
            failures.append(f"{g.game_id}:{n}")
        elif n >= MAX_ELIGIBLE_WARN:
            issue = "eligible_count_high"
            warnings.append(f"{g.game_id}:{n}")
        if hist > 0 and n >= hist * 0.9 and hist > 400:
            issue = issue or "eligible_near_full_historical_table"
            failures.append(f"{g.game_id}:near_hist_{n}/{hist}")
        rows.append(
            {
                "game_id": g.game_id,
                "kickoff_utc": as_utc(g.kickoff_utc).isoformat() if g.kickoff_utc else None,
                "season_year": year,
                "eligible_count": n,
                "skill_eligible": n_skill,
                "historical_roster_table_rows": hist,
                "by_team": summary.get("by_team"),
                "issue": issue,
                "ratio_eligible_to_hist": (n / hist) if hist else None,
            }
        )

    return {
        "status": "ok",
        "n_games": len(rows),
        "n_fail": len(failures),
        "n_warn": len(warnings),
        "failures": failures[:20],
        "warnings": warnings[:20],
        "pass": len(failures) == 0 and len(rows) > 0,
        "note": "FBS eligibility must be season-year filtered; never full historical open-roster dump.",
        "games": rows,
    }


def _row_for_m5(pl: dict[str, Any]) -> dict[str, Any]:
    return {
        **pl,
        "prior_carries": pl.get("exp_carries"),
        "prior_targets": pl.get("exp_targets"),
        "prior_receptions": pl.get("prior_receptions") or pl.get("exp_receptions"),
        "prior_rz_targets": pl.get("exp_rz_targets"),
        "prior_gl_carries": pl.get("exp_gl_carries"),
        "prior_td_rate": pl.get("prior_td_rate"),
        "roll3_td_rate": pl.get("roll3_td_rate"),
        "workload_delta": pl.get("workload_delta"),
        "prior_carry_share": pl.get("prior_carry_share"),
        "own_off_epa": pl.get("own_off_epa"),
        "opp_adj_def": pl.get("opp_adj_def"),
        "team_rz_td": pl.get("team_rz_td"),
        "team_points_per_opp": pl.get("team_points_per_opp"),
        "usage_x_defense": pl.get("usage_x_defense"),
        "is_home": pl.get("is_home"),
        "opp_def_z": pl.get("opp_def_z"),
    }


def _score_c_recent(artifact: dict[str, Any], pl: dict[str, Any]) -> dict[str, Any]:
    """C_recent: substitute recent8 TD-count mean into prior_td_rate when observed.

    Live serving prefers ``recent8_td_rate_current_team`` so prior-organization
    games (transfers) cannot inflate the locked feature slot. Frozen pair rows
    that already store ``p_c_recent`` are untouched.
    """
    row = _row_for_m5(pl)
    recent = pl.get("recent8_td_rate_current_team")
    source = "recent8_td_rate_current_team"
    if recent is None or not np.isfinite(float(recent)):
        recent = pl.get("recent8_td_rate")
        source = "recent8_td_rate"
    if recent is not None and np.isfinite(float(recent)):
        row["prior_td_rate"] = float(recent)
        row["prior_td_rate_source"] = source
    else:
        row["prior_td_rate_source"] = "expanding_fallback"
    return predict_m5_strict(
        artifact,
        player_id=str(pl.get("player_id")),
        team_id=str(pl.get("team_id") or ""),
        position_group=str(pl.get("position_group") or "UNK"),
        row=row,
    )


def build_paired_pregame_freeze(
    session: Session,
    *,
    horizon_hours: int = 72,
    max_games: int = 25,
) -> dict[str, Any]:
    """Same games/players: production M5 vs locked C_recent. Pregame only."""
    moment = datetime.now(UTC)
    c_path = _c_artifact_path()
    if not c_path.exists():
        return {"status": "missing_c_recent_artifact", "path": str(c_path)}
    c_art = json.loads(c_path.read_text(encoding="utf-8"))
    m5 = load_m5("fbs")
    if m5 is None:
        return {"status": "missing_frozen_m5"}

    fc = forecast_upcoming(
        session,
        now=moment,
        horizon_hours=horizon_hours,
        max_games_per_league=max_games,
        include_nfl_ensemble=False,
    )
    fbs_games = ((fc.get("leagues") or {}).get("fbs") or {}).get("games") or []

    frozen: list[dict[str, Any]] = []
    n_paired = 0
    n_skipped_kick = 0
    for g in fbs_games:
        kick = g.get("kickoff_utc")
        if not kick:
            continue
        kick_dt = datetime.fromisoformat(str(kick).replace("Z", "+00:00"))
        if kick_dt.tzinfo is None:
            kick_dt = kick_dt.replace(tzinfo=UTC)
        if kick_dt <= moment:
            n_skipped_kick += 1
            continue  # never backfill after kickoff

        # Re-check eligibility at freeze time
        game = session.get(Game, g.get("game_id"))
        elig_ids: set[str] | None = None
        elig_n = None
        if game is not None:
            elig = eligible_players_for_game(session, game)
            elig_ids = {e.player_id for e in elig}
            elig_n = len(elig)
            if elig_n >= MAX_ELIGIBLE_FAIL:
                continue

        players_out: list[dict[str, Any]] = []
        for pl in g.get("players") or []:
            pid = pl.get("player_id")
            if elig_ids is not None and pid not in elig_ids:
                continue  # drop ineligible / stale catalog players
            # Production M5 already on pl as p_m5 from forecast; re-confirm via strict path
            m5_pred = predict_m5_strict(
                m5,
                player_id=str(pid),
                team_id=str(pl.get("team_id") or ""),
                position_group=str(pl.get("position_group") or "UNK"),
                row=_row_for_m5(pl),
            )
            c_pred = _score_c_recent(c_art, pl)
            entry = {
                "player_id": pid,
                "player_name": pl.get("player_name"),
                "team_id": pl.get("team_id"),
                "team_name": pl.get("team_name"),
                "position_group": pl.get("position_group"),
                "availability_status": pl.get("availability_status"),
                "availability_prob": pl.get("availability_prob"),
                "missing_information": pl.get("missing_information"),
                "prior_td_rate": pl.get("prior_td_rate"),
                "season_td_rate": pl.get("season_td_rate"),
                "recent8_td_rate": pl.get("recent8_td_rate"),
                "exp_carries": pl.get("exp_carries"),
                "exp_targets": pl.get("exp_targets"),
                "prior_receptions": pl.get("prior_receptions"),
                "fbs_receptions_usage_proxy": pl.get("fbs_receptions_usage_proxy"),
                "opp_def_z": pl.get("opp_def_z"),
                "opp_def_source": pl.get("opp_def_source"),
                "p_m5": m5_pred.get("p_atd") if m5_pred.get("status") == "ok" else None,
                "p_m5_status": m5_pred.get("status"),
                "p_c_recent": c_pred.get("p_atd") if c_pred.get("status") == "ok" else None,
                "p_c_recent_status": c_pred.get("status"),
                "p_c_recent_prior_source": (
                    "recent8_td_rate"
                    if pl.get("recent8_td_rate") is not None and np.isfinite(float(pl.get("recent8_td_rate")))
                    else "expanding_fallback"
                ),
                "paired": bool(
                    m5_pred.get("status") == "ok"
                    and c_pred.get("status") == "ok"
                    and m5_pred.get("p_atd") is not None
                    and c_pred.get("p_atd") is not None
                ),
            }
            if entry["paired"]:
                n_paired += 1
            players_out.append(entry)

        frozen.append(
            {
                "league_id": "fbs",
                "game_id": g.get("game_id"),
                "kickoff_utc": kick_dt.isoformat(),
                "frozen_at": moment.isoformat(),
                "holdout_tag": "prospective_unexamined_m5_c_recent_paired_v1",
                "matchup": f"{g.get('away')} @ {g.get('home')}",
                "eligible_count_at_freeze": elig_n,
                "n_players": len(players_out),
                "n_paired": sum(1 for p in players_out if p.get("paired")),
                "immutable": True,
                "players": players_out,
            }
        )

    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "status": "ok",
        "freeze_id": f"fbs_m5_vs_c_recent_paired_{stamp}",
        "frozen_at": moment.isoformat(),
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "validated_play": False,
        "tuning_stopped": True,
        "pair": {
            "A": {
                "family": "m5",
                "label": "production_frozen_m5",
                "artifact": str(artifact_path("fbs")),
                "sha256": _sha256_file(artifact_path("fbs")),
                "prob_field": "p_m5",
            },
            "C": {
                "family": CANDIDATE_FAMILY,
                "label": CANDIDATE_ID,
                "artifact": str(c_path),
                "sha256": _sha256_file(c_path),
                "prob_field": "p_c_recent",
                "feature_definition": "prior_td_rate:=recent8_mean_td_count_when_available",
                "pipeline_version": PIPELINE_VERSION + "+recent8_prior_td_rate",
                "locked_version": LOCKED_VERSION,
            },
        },
        "n_games": len(frozen),
        "n_paired_player_outcomes": n_paired,
        "n_skipped_already_kicked": n_skipped_kick,
        "baselines_to_compare": ["m5", CANDIDATE_FAMILY],
        "metrics_plan": ["brier", "log_loss", "ece", "paired_delta", "by_position"],
        "settlement": {
            "owner": "Agent 1",
            "system": "existing_shadow_predictions_append_only",
            "do_not_build_duplicate": True,
            "ingest_hint": "ingest_paired_freeze_into_shadow / verify-atd-research path",
        },
        "note": "Pregame only. Never backfill after kickoff. No promotion.",
        "games": frozen,
    }
    out = out_dir() / "prospective" / f"paired_freeze_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    (out_dir() / "prospective" / "latest_paired_freeze.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    payload["path"] = str(out)
    return payload


def lock_candidate(*, paired_freeze: dict[str, Any] | None = None) -> dict[str, Any]:
    """Immutable lock record — stop tuning C_recent while results collect."""
    moment = datetime.now(UTC)
    c_path = _c_artifact_path()
    b_path = out_dir() / f"{RESEARCH_MODEL_ID}.json"
    lock = {
        "status": "locked",
        "locked_at": moment.isoformat(),
        "candidate": CANDIDATE_ID,
        "family": CANDIDATE_FAMILY,
        "locked_version": LOCKED_VERSION,
        "pipeline_version": PIPELINE_VERSION + "+recent8_prior_td_rate",
        "play_enabled": False,
        "promoted": False,
        "validated_play": False,
        "research_only": True,
        "further_tuning": False,
        "tuning_stopped": True,
        "do_not_promote": True,
        "do_not_overwrite_production_m5": True,
        "protected_m5_sha256": _sha256_file(artifact_path("fbs")),
        "candidate_artifact": str(c_path),
        "candidate_sha256": _sha256_file(c_path),
        "refit_B_sha256": _sha256_file(b_path),
        "paired_freeze_id": (paired_freeze or {}).get("freeze_id"),
        "paired_freeze_path": (paired_freeze or {}).get("path"),
        "n_paired_games": (paired_freeze or {}).get("n_games"),
        "n_paired_player_outcomes": (paired_freeze or {}).get("n_paired_player_outcomes"),
        "collect_results": True,
        "note": "Candidate locked. Collect prospective settlements via Agent 1. No further feature/model tuning.",
    }
    path = _lock_dir() / "C_RECENT_LOCKED.json"
    if path.exists():
        prev = json.loads(path.read_text(encoding="utf-8"))
        lock["previous_lock"] = {
            "locked_at": prev.get("locked_at"),
            "candidate_sha256": prev.get("candidate_sha256"),
        }
        # Do not rewrite candidate weights; preserve first lock hash if identical
        if prev.get("candidate_sha256") == lock["candidate_sha256"]:
            lock["lock_unchanged"] = True
    path.write_text(json.dumps(lock, indent=2, default=str), encoding="utf-8")
    # Snapshot artifact bytes under locked/ so later research cannot silently mutate the graded file.
    if c_path.exists():
        snap = _lock_dir() / f"{RECENT_MODEL_ID}.locked.json"
        if not snap.exists():
            snap.write_text(c_path.read_text(encoding="utf-8"), encoding="utf-8")
            lock["artifact_snapshot"] = str(snap)
        else:
            lock["artifact_snapshot"] = str(snap)
            lock["artifact_snapshot_preserved"] = True
        path.write_text(json.dumps(lock, indent=2, default=str), encoding="utf-8")
    lock["path"] = str(path)
    return lock


def write_agent1_handoff(
    *,
    lock: dict[str, Any],
    paired_freeze: dict[str, Any],
    eligibility: dict[str, Any],
    loader: dict[str, Any],
) -> dict[str, Any]:
    """Package for Agent 1: capture + grade via existing shadow settlement only."""
    moment = datetime.now(UTC)
    handoff = {
        "to": "Agent 1",
        "from": "Agent 2",
        "handed_off_at": moment.isoformat(),
        "subject": "FBS M5 vs locked C_recent paired prospective cohort",
        "play_enabled": False,
        "promoted": False,
        "validated_play": False,
        "do_not_promote": True,
        "do_not_build_duplicate_settlement": True,
        "action_required": [
            "Ingest paired freeze into append-only shadow_predictions (m5 + m5_c_recent families)",
            "Settle after games final using existing shadow settlement",
            "Report paired Brier/log-loss/ECE for M5 vs C_recent on identical outcomes",
            "Never backfill freezes after kickoff",
            "Do not overwrite protected fbs_m5.json",
        ],
        "artifacts": {
            "lock": lock.get("path"),
            "paired_freeze": paired_freeze.get("path"),
            "candidate_snapshot": lock.get("artifact_snapshot"),
            "refit_report": str(out_dir() / "m5_fbs_refit_report.json"),
            "cohort_pointer": str(out_dir() / "prospective" / "latest_paired_freeze.json"),
        },
        "versioning": {
            "locked_version": LOCKED_VERSION,
            "pipeline_version": PIPELINE_VERSION + "+recent8_prior_td_rate",
            "candidate_sha256": lock.get("candidate_sha256"),
            "production_m5_sha256": lock.get("protected_m5_sha256"),
            "feature_definition_C": "prior_td_rate:=recent8_mean_td_count_when_available",
            "feature_definition_A": "expanding_prior_td_rate_involved_universe",
        },
        "gates": {
            "eligibility_pass": eligibility.get("pass"),
            "loader_pass": loader.get("pass"),
            "eligibility_n_games": eligibility.get("n_games"),
            "loader": {k: loader.get(k) for k in ("fbs_prior_td_max_abs_delta", "nfl_loader_ok", "fbs_no_zero_touch")},
        },
        "cohort_summary": {
            "freeze_id": paired_freeze.get("freeze_id"),
            "n_games": paired_freeze.get("n_games"),
            "n_paired_player_outcomes": paired_freeze.get("n_paired_player_outcomes"),
            "holdout_tag": "prospective_unexamined_m5_c_recent_paired_v1",
        },
        "model_version_ids": {
            "m5": "m5-fbs-anytime_td",
            "m5_c_recent": f"{CANDIDATE_FAMILY}-fbs-anytime_td",
        },
    }
    path = _handoff_dir() / "AGENT1_HANDOFF.json"
    path.write_text(json.dumps(handoff, indent=2, default=str), encoding="utf-8")
    # Stable pointer Agent 1 can poll without scanning history
    (_handoff_dir() / "LATEST.json").write_text(
        json.dumps({"handoff_path": str(path), "handed_off_at": moment.isoformat(), "status": "ready"}, indent=2),
        encoding="utf-8",
    )
    handoff["path"] = str(path)
    return handoff


def ingest_paired_freeze_into_shadow(session: Session, freeze: dict[str, Any] | None = None) -> dict[str, Any]:
    """Append-only shadow rows for both M5 and C_recent from the paired freeze.

    Reuses Agent 1's shadow_predictions table — no separate settlement system.
    """
    from spe.research.atd_verify import _priced_anytime_rows, _write_shadow_row
    from spe.db.models import ShadowPrediction

    moment = datetime.now(UTC)
    if freeze is None:
        latest = out_dir() / "prospective" / "latest_paired_freeze.json"
        if not latest.exists():
            return {"status": "missing_paired_freeze"}
        freeze = json.loads(latest.read_text(encoding="utf-8"))

    priced = _priced_anytime_rows(session)
    existing = {
        (r.outcome_id, r.model_version_id, r.odds_tick_id)
        for r in session.query(ShadowPrediction).filter(ShadowPrediction.market_id == "anytime_td").all()
    }
    summary: dict[str, Any] = {
        "status": "ok",
        "written": 0,
        "unchanged": 0,
        "skipped_no_price": 0,
        "skipped_no_prob": 0,
        "skipped_not_paired": 0,
        "by_model": {},
        "freeze_id": freeze.get("freeze_id"),
        "play_enabled": False,
        "research_only": True,
    }
    for g in freeze.get("games") or []:
        gid = g.get("game_id")
        for pl in g.get("players") or []:
            if not pl.get("paired"):
                summary["skipped_not_paired"] += 1
                continue
            pid = pl.get("player_id")
            price_row = None
            for row in priced.values():
                if row["game_id"] == gid and row["player_id"] == pid:
                    price_row = row
                    break
            if price_row is None:
                summary["skipped_no_price"] += 1
                continue
            for fam, field, mid in (
                ("m5", "p_m5", "m5-fbs-anytime_td"),
                (CANDIDATE_FAMILY, "p_c_recent", f"{CANDIDATE_FAMILY}-fbs-anytime_td"),
            ):
                p = pl.get(field)
                if p is None or not np.isfinite(float(p)):
                    summary["skipped_no_prob"] += 1
                    continue
                key = (price_row["outcome_id"], mid, price_row["tick_id"])
                if key in existing:
                    summary["unchanged"] += 1
                    continue
                _write_shadow_row(
                    session,
                    price_row=price_row,
                    model_version_id=mid,
                    p=float(p),
                    now=moment,
                    freeze_meta={
                        "freeze_frozen_at": freeze.get("frozen_at"),
                        "freeze_id": freeze.get("freeze_id"),
                        "holdout_tag": g.get("holdout_tag"),
                        "matchup": g.get("matchup"),
                        "player_name": pl.get("player_name"),
                        "position_group": pl.get("position_group"),
                        "availability_status": pl.get("availability_status"),
                        "family": fam,
                        "paired_vs": "m5" if fam == CANDIDATE_FAMILY else CANDIDATE_FAMILY,
                        "locked_version": LOCKED_VERSION if fam == CANDIDATE_FAMILY else None,
                        "prior_td_rate": pl.get("prior_td_rate"),
                        "recent8_td_rate": pl.get("recent8_td_rate"),
                        "p_c_recent_prior_source": pl.get("p_c_recent_prior_source"),
                        "fbs_receptions_usage_proxy": pl.get("fbs_receptions_usage_proxy"),
                        "missing_features": pl.get("missing_information") or [],
                    },
                )
                existing.add(key)
                summary["written"] += 1
                summary["by_model"][fam] = int(summary["by_model"].get(fam, 0)) + 1
    session.flush()
    path = _handoff_dir() / "shadow_ingest_summary.json"
    path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    summary["path"] = str(path)
    return summary


def run_lock_and_handoff(session: Session) -> dict[str, Any]:
    print("[lock] eligibility…", flush=True)
    eligibility = verify_player_eligibility(session)
    print("[lock] shared-loader regressions…", flush=True)
    loader = verify_shared_loader_regressions(session)
    gates_ok = bool(eligibility.get("pass") and loader.get("pass"))
    report: dict[str, Any] = {
        "status": "ok" if gates_ok else "blocked",
        "as_of": datetime.now(UTC).isoformat(),
        "play_enabled": False,
        "promoted": False,
        "further_tuning": False,
        "eligibility": eligibility,
        "loader": loader,
        "gates_pass": gates_ok,
    }
    if not gates_ok:
        path = out_dir() / "lock_handoff_report.json"
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        report["report_path"] = str(path)
        return report

    print("[lock] paired pregame freeze M5 vs C_recent…", flush=True)
    paired = build_paired_pregame_freeze(session)
    print("[lock] locking candidate…", flush=True)
    lock = lock_candidate(paired_freeze=paired)
    print("[lock] Agent 1 handoff package…", flush=True)
    handoff = write_agent1_handoff(lock=lock, paired_freeze=paired, eligibility=eligibility, loader=loader)
    print("[lock] ingest paired freeze into shadow (append-only)…", flush=True)
    ingest = ingest_paired_freeze_into_shadow(session, paired)
    report.update(
        {
            "paired_freeze": {
                "freeze_id": paired.get("freeze_id"),
                "path": paired.get("path"),
                "n_games": paired.get("n_games"),
                "n_paired_player_outcomes": paired.get("n_paired_player_outcomes"),
            },
            "lock": lock,
            "agent1_handoff": handoff,
            "shadow_ingest": ingest,
        }
    )
    path = out_dir() / "lock_handoff_report.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    print(f"[lock] wrote {path}", flush=True)
    return report
