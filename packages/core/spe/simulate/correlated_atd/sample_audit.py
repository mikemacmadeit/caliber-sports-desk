"""Audit Phase 5 'prospective' samples and label provenance accurately.

Never describe historical chronological replay as live prospective validation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.simulate.correlated_atd.phase5_isolation import phase5_dir
from spe.simulate.correlated_atd.proof_isolation import proof_dir

# Evidence taxonomy — use these labels only.
LABEL_HISTORICAL_REPLAY = "historical_chronological_holdout_replay"
LABEL_FROZEN_OOS = "previously_frozen_out_of_sample_forecast"
LABEL_GENUINE_LIVE = "genuine_pre_kickoff_frozen_then_settled"
LABEL_LIVE_OPEN = "genuine_pre_kickoff_frozen_awaiting_settlement"
LABEL_UNKNOWN = "insufficient_evidence"


def _load_phase5_role_games(league_id: str, role: str = "prospective") -> list[str]:
    path = phase5_dir() / f"{league_id}_expanded_validation.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Prefer explicit lists; fall back to roles.*.game_ids
    key = f"{role}_game_ids"
    if raw.get(key):
        return [str(g) for g in raw[key]]
    return [str(g) for g in ((raw.get("roles") or {}).get(role) or {}).get("game_ids") or []]


def audit_phase5_prospective(session: Session) -> dict[str, Any]:
    """Classify the 50-game Phase 5 prospective sets for NFL and FBS."""
    report: dict[str, Any] = {
        "definition": {
            LABEL_HISTORICAL_REPLAY: (
                "Completed games scored after the fact with current model artifacts on a "
                "chronological holdout split. Leakage-safe priors may be used, but forecasts "
                "were NOT frozen in the DB before kickoff. Must NOT be called live prospective."
            ),
            LABEL_FROZEN_OOS: (
                "Predictions written to shadow_predictions / forecasts before kickoff, later "
                "evaluated — but may have been produced in a research batch rather than the "
                "production shadow cycle."
            ),
            LABEL_GENUINE_LIVE: (
                "Shadow row with forecast_as_of < kickoff_utc, then settled after official final "
                "against the same player/game/outcome."
            ),
            LABEL_LIVE_OPEN: (
                "Shadow row frozen before kickoff; game not yet settled or still upcoming."
            ),
            "phase5_method_note": (
                "Phase 5 expanded_validation.split used trailing chronological games from "
                "build_player_td_frame (completed finals) and re-simulated with score_games. "
                "That is historical chronological holdout replay."
            ),
        },
        "leagues": {},
        "live_shadow_universe": {},
    }

    for league_id in ("nfl", "fbs"):
        gids = _load_phase5_role_games(league_id, "prospective")
        league_block = _classify_games(session, league_id, gids, sample_name="phase5_prospective")
        report["leagues"][league_id] = league_block

    report["live_shadow_universe"] = audit_live_shadow_anytime(session)
    report["verdict"] = {
        "phase5_prospective_is_live": False,
        "correct_label": LABEL_HISTORICAL_REPLAY,
        "do_not_say": "live prospective validation",
        "genuine_live_evidence_source": "shadow_predictions with forecast_as_of < kickoff (+ settlements)",
    }

    path = proof_dir() / "sample_provenance_audit.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["artifact_path"] = str(path)
    return report


def _classify_games(session: Session, league_id: str, game_ids: list[str], *, sample_name: str) -> dict[str, Any]:
    if not game_ids:
        return {"league_id": league_id, "sample": sample_name, "n_games": 0, "error": "no_game_ids"}

    from sqlalchemy import bindparam

    games = {
        r["game_id"]: dict(r)
        for r in session.execute(
            text(
                """
                SELECT game_id, league_id, status, data_tier, kickoff_utc, home_team_id, away_team_id
                FROM games WHERE game_id IN :gids
                """
            ).bindparams(bindparam("gids", expanding=True)),
            {"gids": game_ids},
        ).mappings()
    }

    shadows = session.execute(
        text(
            """
            SELECT s.shadow_id, s.game_id, s.player_id, s.model_version_id, s.forecast_as_of,
                   s.odds_tick_id, s.price_american, st.result, st.settled_at
            FROM shadow_predictions s
            LEFT JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
            WHERE s.market_id = 'anytime_td' AND s.game_id IN :gids
            """
        ).bindparams(bindparam("gids", expanding=True)),
        {"gids": game_ids},
    ).mappings().all()

    by_game_shadow: dict[str, list[dict[str, Any]]] = {}
    for row in shadows:
        by_game_shadow.setdefault(str(row["game_id"]), []).append(dict(row))

    counts = {
        LABEL_HISTORICAL_REPLAY: 0,
        LABEL_FROZEN_OOS: 0,
        LABEL_GENUINE_LIVE: 0,
        LABEL_LIVE_OPEN: 0,
        LABEL_UNKNOWN: 0,
    }
    examples: dict[str, list[dict[str, Any]]] = {k: [] for k in counts}
    per_game: list[dict[str, Any]] = []

    for gid in game_ids:
        g = games.get(gid)
        if g is None:
            label = LABEL_UNKNOWN
            detail = {"game_id": gid, "label": label, "reason": "game_row_missing"}
            counts[label] += 1
            per_game.append(detail)
            continue

        kick = as_utc(g["kickoff_utc"])
        status = str(g["status"] or "").lower()
        tier = str(g["data_tier"] or "").lower()
        rows = by_game_shadow.get(gid) or []

        pre_kick = []
        for s in rows:
            fas = as_utc(s["forecast_as_of"])
            if kick is not None and fas is not None and fas < kick:
                pre_kick.append(s)

        if pre_kick:
            settled = [s for s in pre_kick if s.get("result") in {"win", "loss"}]
            if settled:
                label = LABEL_GENUINE_LIVE
                reason = "shadow_frozen_before_kickoff_and_settled"
            elif status in {"final", "post"}:
                label = LABEL_FROZEN_OOS
                reason = "shadow_frozen_before_kickoff_but_settlement_missing_or_blocked"
            else:
                label = LABEL_LIVE_OPEN
                reason = "shadow_frozen_before_kickoff_awaiting_result"
        else:
            # Phase 5 scoring path: completed frame rows without pre-kickoff freeze.
            if tier == "historical" or status in {"final", "post"}:
                label = LABEL_HISTORICAL_REPLAY
                reason = "completed_game_resimulated_after_fact_no_prekickoff_shadow"
            else:
                label = LABEL_UNKNOWN
                reason = "no_prekickoff_shadow_and_not_clearly_historical"

        counts[label] += 1
        detail = {
            "game_id": gid,
            "label": label,
            "reason": reason,
            "status": status,
            "data_tier": tier,
            "kickoff_utc": str(kick) if kick else None,
            "n_anytime_shadow_rows": len(rows),
            "n_prekickoff_shadow_rows": len(pre_kick),
        }
        per_game.append(detail)
        if len(examples[label]) < 3:
            examples[label].append(detail)

    majority = max(counts, key=counts.get)  # type: ignore[arg-type]
    return {
        "league_id": league_id,
        "sample": sample_name,
        "n_games": len(game_ids),
        "label_counts": counts,
        "majority_label": majority,
        "is_live_prospective": majority in {LABEL_GENUINE_LIVE, LABEL_LIVE_OPEN} and counts[LABEL_HISTORICAL_REPLAY] == 0,
        "examples_by_label": examples,
        "games": per_game,
        "accurate_description": (
            f"Phase 5 '{sample_name}' for {league_id}: majority={majority}. "
            + (
                "These are historical chronological holdout replays, not live pre-kickoff validation."
                if majority == LABEL_HISTORICAL_REPLAY
                else "See per-game labels."
            )
        ),
    }


def audit_live_shadow_anytime(session: Session) -> dict[str, Any]:
    """Summarize genuine pre-kickoff anytime_td shadow rows by model family."""
    rows = session.execute(
        text(
            """
            SELECT s.shadow_id, s.league_id, s.model_version_id, s.forecast_as_of,
                   s.game_id, s.player_id, s.price_american, s.bookmaker_id,
                   s.recommendation_status, s.research_only, s.play_enabled,
                   g.kickoff_utc, g.status,
                   st.result
            FROM shadow_predictions s
            JOIN games g ON g.game_id = s.game_id
            LEFT JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
            WHERE s.market_id = 'anytime_td'
            """
        )
    ).mappings().all()

    families: dict[str, dict[str, int]] = {}
    for r in rows:
        fam = _family(str(r["model_version_id"]))
        fas = as_utc(r["forecast_as_of"])
        kick = as_utc(r["kickoff_utc"])
        pre = bool(kick and fas and fas < kick)
        bucket = families.setdefault(
            fam,
            {
                "n_rows": 0,
                "pre_kickoff": 0,
                "settled_win_loss": 0,
                "open_or_unsettled": 0,
                "post_kickoff_or_unknown": 0,
            },
        )
        bucket["n_rows"] += 1
        if pre:
            bucket["pre_kickoff"] += 1
            if r["result"] in {"win", "loss"}:
                bucket["settled_win_loss"] += 1
            else:
                bucket["open_or_unsettled"] += 1
        else:
            bucket["post_kickoff_or_unknown"] += 1

    return {
        "as_of": datetime.now(UTC).isoformat(),
        "by_family": families,
        "note": "Only pre_kickoff + settled_win_loss counts as genuine live prospective validation evidence.",
    }


def _family(model_version_id: str) -> str:
    mv = model_version_id or ""
    if mv.startswith("atd_sim_v1"):
        return "atd_sim_v1"
    if mv.startswith("m6-"):
        return "m6"
    if mv.startswith("m5-"):
        return "m5"
    if mv.startswith("m4-"):
        return "m4"
    if mv.startswith("m3-"):
        return "m3"
    return "other"
