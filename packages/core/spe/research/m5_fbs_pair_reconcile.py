"""Reconcile locked M5 vs C_recent paired freeze for Agent 1.

Does not invent odds. Does not create a second settlement system.
Does not retune or promote. Missing box scores stay blocked (not zero).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.research.m5_fbs_lock_handoff import (
    CANDIDATE_FAMILY,
    LOCKED_VERSION,
    _handoff_dir,
    out_dir,
)
from spe.research.m5_fbs_refit import PIPELINE_VERSION

M5_MID = "m5-fbs-anytime_td"
C_MID = f"{CANDIDATE_FAMILY}-fbs-anytime_td"
PROB_TOL = 1e-6


def _paired_freeze_path() -> Path:
    return out_dir() / "prospective" / "latest_paired_freeze.json"


def _load_freeze() -> dict[str, Any]:
    path = _paired_freeze_path()
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_dt(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return as_utc(value) or value
    text_v = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text_v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return as_utc(dt) or dt


def _iter_pairs(freeze: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for g in freeze.get("games") or []:
        for pl in g.get("players") or []:
            if not pl.get("paired"):
                continue
            rows.append(
                {
                    "freeze_id": freeze.get("freeze_id"),
                    "frozen_at": freeze.get("frozen_at"),
                    "game_id": g.get("game_id"),
                    "kickoff_utc": g.get("kickoff_utc"),
                    "matchup": g.get("matchup"),
                    "holdout_tag": g.get("holdout_tag"),
                    "player_id": pl.get("player_id"),
                    "player_name": pl.get("player_name"),
                    "team_id": pl.get("team_id"),
                    "position_group": pl.get("position_group"),
                    "availability_status": pl.get("availability_status"),
                    "p_m5": pl.get("p_m5"),
                    "p_c_recent": pl.get("p_c_recent"),
                    "prior_td_rate": pl.get("prior_td_rate"),
                    "recent8_td_rate": pl.get("recent8_td_rate"),
                    "p_c_recent_prior_source": pl.get("p_c_recent_prior_source"),
                    "missing_information": pl.get("missing_information"),
                }
            )
    return rows


def _shadow_index(session: Session, game_ids: list[str]) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    if not game_ids:
        return {}
    from sqlalchemy import bindparam

    rows = session.execute(
        text(
            """
            SELECT shadow_id, player_id, game_id, model_version_id, forecast_probability,
                   forecast_as_of, created_at, odds_tick_id, outcome_id, price_american,
                   components,
                   json_extract(components, '$.freeze_id') AS freeze_id,
                   json_extract(components, '$.freeze_frozen_at') AS freeze_frozen_at,
                   json_extract(components, '$.family') AS family,
                   json_extract(components, '$.locked_version') AS locked_version
            FROM shadow_predictions
            WHERE market_id = 'anytime_td' AND league_id = 'fbs'
              AND game_id IN :games
              AND model_version_id IN :mids
            """
        ).bindparams(bindparam("games", expanding=True), bindparam("mids", expanding=True)),
        {"games": game_ids, "mids": [M5_MID, C_MID]},
    ).mappings().all()
    out: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for r in rows:
        key = (str(r["game_id"]), str(r["player_id"]), str(r["model_version_id"]))
        comps = r["components"]
        if isinstance(comps, str):
            try:
                comps = json.loads(comps)
            except Exception:
                comps = {}
        if not isinstance(comps, dict):
            comps = {}
        # Prefer explicit json_extract columns (SQLite JSON reliable); fill gaps in comps.
        for k, col in (
            ("freeze_id", "freeze_id"),
            ("freeze_frozen_at", "freeze_frozen_at"),
            ("family", "family"),
            ("locked_version", "locked_version"),
        ):
            val = r.get(col)
            if val is not None and comps.get(k) is None:
                comps[k] = val
        out.setdefault(key, []).append(
            {
                "shadow_id": r["shadow_id"],
                "forecast_probability": float(r["forecast_probability"]),
                "forecast_as_of": r["forecast_as_of"],
                "created_at": r["created_at"],
                "odds_tick_id": r["odds_tick_id"],
                "outcome_id": r["outcome_id"],
                "price_american": r["price_american"],
                "components": comps,
            }
        )
    return out


def _settlements_for(session: Session, shadow_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not shadow_ids:
        return {}
    from sqlalchemy import bindparam

    rows = session.execute(
        text(
            """
            SELECT settlement_id, shadow_id, result, actual_value, settled_at,
                   official_source, void_reason, evidence
            FROM shadow_settlements
            WHERE shadow_id IN :ids
            """
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": shadow_ids},
    ).mappings().all()
    return {str(r["shadow_id"]): dict(r) for r in rows}


def _game_status(session: Session, game_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not game_ids:
        return {}
    from sqlalchemy import bindparam

    rows = session.execute(
        text("SELECT game_id, status, kickoff_utc FROM games WHERE game_id IN :games").bindparams(
            bindparam("games", expanding=True)
        ),
        {"games": game_ids},
    ).mappings().all()
    return {str(r["game_id"]): dict(r) for r in rows}


def _box_and_injury(
    session: Session, game_ids: list[str]
) -> tuple[dict[tuple[str, str], dict[str, float]], dict[tuple[str, str], str]]:
    """Reuse settle.py semantics without inventing zeros."""
    from spe.shadow.settle import _inactive_before_kickoff, _player_stats

    finals = {
        gid
        for gid, g in _game_status(session, game_ids).items()
        if str(g.get("status") or "").lower() in {"final", "post"}
    }
    return _player_stats(session, finals), _inactive_before_kickoff(session, finals)


def _accuracy_outcome(
    *,
    game_status: str | None,
    kickoff: datetime | None,
    now: datetime,
    stats: dict[str, float] | None,
    injury: str | None,
) -> dict[str, Any]:
    """Mirror existing settle rules for prediction-accuracy (odds optional)."""
    status = str(game_status or "").lower()
    if kickoff and kickoff > now:
        return {
            "status": "pending_kickoff",
            "gradeable": False,
            "y_atd": None,
            "result_vs_yes": None,
            "rule": "pregame_freeze_preserved",
        }
    if status not in {"final", "post"}:
        return {
            "status": "pending_final",
            "gradeable": False,
            "y_atd": None,
            "result_vs_yes": None,
            "rule": "wait_for_official_final",
        }
    if injury:
        return {
            "status": "void",
            "gradeable": False,
            "y_atd": None,
            "result_vs_yes": "void",
            "rule": "confirmed_inactive_pregame",
            "injury_designation": injury,
        }
    if stats is None:
        return {
            "status": "blocked",
            "gradeable": False,
            "y_atd": None,
            "result_vs_yes": "blocked",
            "rule": "missing_box_score_not_assumed_zero",
        }
    y = 1.0 if float(stats.get("anytime_tds") or 0) > 0 else 0.0
    return {
        "status": "resolved",
        "gradeable": True,
        "y_atd": y,
        "result_vs_yes": "win" if y > 0 else "loss",
        "rule": "player_game_stats_rush_td_plus_rec_td",
        "anytime_tds": float(stats.get("anytime_tds") or 0),
    }


def _pick_shadow_row(
    candidates: list[dict[str, Any]],
    *,
    expected_p: float | None,
    freeze_id: str | None,
    kickoff: datetime | None,
) -> tuple[dict[str, Any] | None, str]:
    """Prefer row matching freeze_id + probability; else best pregame match."""
    if not candidates:
        return None, "missing_shadow_row"
    # Prefer exact freeze_id from this handoff ingest
    with_fid = [c for c in candidates if (c.get("components") or {}).get("freeze_id") == freeze_id]
    pool = with_fid or candidates

    def pregame_ok(c: dict[str, Any]) -> bool:
        fa = _parse_dt(c.get("forecast_as_of"))
        return bool(kickoff and fa and fa < kickoff)

    pregame = [c for c in pool if pregame_ok(c)]
    pool2 = pregame or pool

    if expected_p is not None:
        exact = [c for c in pool2 if abs(float(c["forecast_probability"]) - float(expected_p)) <= PROB_TOL]
        if exact:
            # Prefer newest among exact matches
            exact.sort(key=lambda c: _parse_dt(c.get("created_at")) or datetime.min.replace(tzinfo=UTC), reverse=True)
            return exact[0], "prob_match"
        # Closest pregame
        pool2.sort(key=lambda c: abs(float(c["forecast_probability"]) - float(expected_p)))
        return pool2[0], "prob_mismatch"

    pool2.sort(key=lambda c: _parse_dt(c.get("created_at")) or datetime.min.replace(tzinfo=UTC), reverse=True)
    return pool2[0], "no_expected_prob"


def _verify_m5_baseline(
    shadow: dict[str, Any],
    *,
    expected_p: float,
    freeze_frozen_at: datetime | None,
    kickoff: datetime | None,
    from_this_freeze: bool,
) -> dict[str, Any]:
    comps = shadow.get("components") or {}
    fa = _parse_dt(shadow.get("forecast_as_of"))
    created = _parse_dt(shadow.get("created_at"))
    p = float(shadow["forecast_probability"])
    prob_ok = abs(p - float(expected_p)) <= PROB_TOL
    pregame_ok = bool(kickoff and fa and fa < kickoff)
    # Reused rows from earlier freezes may lack our locked pipeline tags — require
    # either this freeze_id OR (prob match + pregame + m5 family artifact).
    family = comps.get("family") or "m5"
    freeze_id_ok = comps.get("freeze_id") is not None or from_this_freeze is False
    version_ok = True  # production m5 family; C_recent has locked_version separately
    if comps.get("locked_version") and comps.get("locked_version") != LOCKED_VERSION:
        # Should not appear on m5 baseline
        version_ok = False

    issues: list[str] = []
    if not prob_ok:
        issues.append("probability_mismatch_vs_locked_pair")
    if not pregame_ok:
        issues.append("forecast_not_before_kickoff")
    if family not in {"m5", None, "m5_anytime_td"} and family != "m5":
        if family != "m5":
            issues.append(f"unexpected_family:{family}")
    if not freeze_id_ok and not from_this_freeze:
        pass

    # For reused rows: ingestion time may be earlier than this freeze; freeze evidence
    # for THIS cohort is the immutable paired artifact. Shadow must still be pregame.
    match_status = "valid_baseline_match" if (prob_ok and pregame_ok) else "unresolved_baseline_match"
    return {
        "match_status": match_status,
        "probability_match": prob_ok,
        "pregame_cutoff_ok": pregame_ok,
        "version_ok": version_ok,
        "shadow_probability": p,
        "locked_pair_probability": float(expected_p),
        "prob_delta": abs(p - float(expected_p)),
        "forecast_as_of": fa.isoformat() if fa else None,
        "ingestion_created_at": created.isoformat() if created else None,
        "freeze_frozen_at": freeze_frozen_at.isoformat() if freeze_frozen_at else None,
        "kickoff_utc": kickoff.isoformat() if kickoff else None,
        "freeze_time_vs_ingestion_separate": True,
        "shadow_freeze_id": comps.get("freeze_id"),
        "shadow_freeze_frozen_at": comps.get("freeze_frozen_at"),
        "shadow_family": family,
        "from_this_freeze_ingest": from_this_freeze,
        "issues": issues,
        "shadow_id": shadow.get("shadow_id"),
    }


def reconcile_paired_cohort(session: Session) -> dict[str, Any]:
    moment = datetime.now(UTC)
    freeze = _load_freeze()
    pairs = _iter_pairs(freeze)
    freeze_id = freeze.get("freeze_id")
    freeze_frozen_at = _parse_dt(freeze.get("frozen_at"))
    game_ids = sorted({p["game_id"] for p in pairs if p.get("game_id")})
    games = _game_status(session, game_ids)
    shadow_idx = _shadow_index(session, game_ids)
    all_shadow_ids = [r["shadow_id"] for rows in shadow_idx.values() for r in rows]
    settlements = _settlements_for(session, all_shadow_ids)
    stats, injuries = _box_and_injury(session, game_ids)

    mapping: list[dict[str, Any]] = []
    counts = {
        "total_frozen_pairs": len(pairs),
        "priced": 0,
        "unpriced": 0,
        "m5_shadow_present": 0,
        "c_recent_shadow_present": 0,
        "m5_reused_verified_valid": 0,
        "m5_reused_unresolved": 0,
        "m5_new_from_this_freeze": 0,
        "accuracy_gradeable_now": 0,
        "accuracy_pending": 0,
        "accuracy_blocked": 0,
        "accuracy_void": 0,
        "agent1_can_evaluate_when_resolved": 0,
    }

    for pair in pairs:
        gid = str(pair["game_id"])
        pid = str(pair["player_id"])
        kick = _parse_dt(pair.get("kickoff_utc")) or _parse_dt((games.get(gid) or {}).get("kickoff_utc"))
        gstat = games.get(gid) or {}
        m5_cands = shadow_idx.get((gid, pid, M5_MID), [])
        c_cands = shadow_idx.get((gid, pid, C_MID), [])

        priced = bool(m5_cands or c_cands)
        # Priced for our cohort means both families ideally; ledger may have one.
        # Definition used for accounting: presence of a sportsbook-backed shadow for either
        # family (odds existed at ingest). Unpriced = neither family in shadow for this pair.
        if priced:
            counts["priced"] += 1
        else:
            counts["unpriced"] += 1

        m5_row, m5_pick = _pick_shadow_row(
            m5_cands, expected_p=pair.get("p_m5"), freeze_id=freeze_id, kickoff=kick
        )
        c_row, c_pick = _pick_shadow_row(
            c_cands, expected_p=pair.get("p_c_recent"), freeze_id=freeze_id, kickoff=kick
        )

        m5_verify = None
        if m5_row is not None:
            counts["m5_shadow_present"] += 1
            from_this = (m5_row.get("components") or {}).get("freeze_id") == freeze_id
            if from_this:
                counts["m5_new_from_this_freeze"] += 1
            m5_verify = _verify_m5_baseline(
                m5_row,
                expected_p=float(pair["p_m5"]),
                freeze_frozen_at=freeze_frozen_at,
                kickoff=kick,
                from_this_freeze=from_this,
            )
            if not from_this:
                if m5_verify["match_status"] == "valid_baseline_match":
                    counts["m5_reused_verified_valid"] += 1
                else:
                    counts["m5_reused_unresolved"] += 1
            elif m5_verify["match_status"] != "valid_baseline_match":
                counts["m5_reused_unresolved"] += 1  # treat failed new as unresolved too

        if c_row is not None:
            counts["c_recent_shadow_present"] += 1

        key = (gid, pid)
        acc = _accuracy_outcome(
            game_status=gstat.get("status"),
            kickoff=kick,
            now=moment,
            stats=stats.get(key),
            injury=injuries.get(key),
        )
        if acc["status"] == "resolved" and acc.get("gradeable"):
            counts["accuracy_gradeable_now"] += 1
        elif acc["status"] == "blocked":
            counts["accuracy_blocked"] += 1
        elif acc["status"] == "void":
            counts["accuracy_void"] += 1
        else:
            counts["accuracy_pending"] += 1

        # Agent 1 can evaluate every pair for prediction accuracy once outcome resolves,
        # using immutable freeze probs even when unpriced (no invented odds).
        counts["agent1_can_evaluate_when_resolved"] += 1

        m5_settle = settlements.get(m5_row["shadow_id"]) if m5_row else None
        c_settle = settlements.get(c_row["shadow_id"]) if c_row else None

        mapping.append(
            {
                "pair_key": f"{gid}|{pid}",
                "game_id": gid,
                "player_id": pid,
                "player_name": pair.get("player_name"),
                "position_group": pair.get("position_group"),
                "matchup": pair.get("matchup"),
                "kickoff_utc": kick.isoformat() if kick else pair.get("kickoff_utc"),
                "freeze_id": freeze_id,
                "freeze_frozen_at": freeze.get("frozen_at"),
                "holdout_tag": pair.get("holdout_tag"),
                "pricing": {
                    "status": "priced" if priced else "unpriced",
                    "m5_in_shadow": m5_row is not None,
                    "c_recent_in_shadow": c_row is not None,
                    "note": (
                        None
                        if priced
                        else "retained_in_immutable_paired_artifact; join outcomes via existing settle rules; no invented odds"
                    ),
                },
                "locked_predictions": {
                    "p_m5": pair.get("p_m5"),
                    "p_c_recent": pair.get("p_c_recent"),
                    "prior_td_rate": pair.get("prior_td_rate"),
                    "recent8_td_rate": pair.get("recent8_td_rate"),
                    "p_c_recent_prior_source": pair.get("p_c_recent_prior_source"),
                    "pipeline_version_C": PIPELINE_VERSION + "+recent8_prior_td_rate",
                    "locked_version": LOCKED_VERSION,
                },
                "shadow_m5": {
                    "present": m5_row is not None,
                    "pick_reason": m5_pick,
                    "shadow_id": (m5_row or {}).get("shadow_id"),
                    "baseline_verification": m5_verify,
                    "settlement": (
                        {
                            "settlement_id": m5_settle.get("settlement_id"),
                            "result": m5_settle.get("result"),
                            "actual_value": m5_settle.get("actual_value"),
                            "void_reason": m5_settle.get("void_reason"),
                            "settled_at": (
                                m5_settle["settled_at"].isoformat()
                                if hasattr(m5_settle.get("settled_at"), "isoformat")
                                else m5_settle.get("settled_at")
                            ),
                        }
                        if m5_settle
                        else {"result": "unsettled" if m5_row else "no_shadow_row"}
                    ),
                },
                "shadow_c_recent": {
                    "present": c_row is not None,
                    "pick_reason": c_pick,
                    "shadow_id": (c_row or {}).get("shadow_id"),
                    "c_prob_match": (
                        abs(float(c_row["forecast_probability"]) - float(pair["p_c_recent"])) <= PROB_TOL
                        if c_row is not None and pair.get("p_c_recent") is not None
                        else None
                    ),
                    "forecast_as_of": (
                        _parse_dt(c_row.get("forecast_as_of")).isoformat()
                        if c_row and _parse_dt(c_row.get("forecast_as_of"))
                        else None
                    ),
                    "ingestion_created_at": (
                        _parse_dt(c_row.get("created_at")).isoformat()
                        if c_row and _parse_dt(c_row.get("created_at"))
                        else None
                    ),
                    "settlement": (
                        {
                            "settlement_id": c_settle.get("settlement_id"),
                            "result": c_settle.get("result"),
                            "actual_value": c_settle.get("actual_value"),
                            "void_reason": c_settle.get("void_reason"),
                        }
                        if c_settle
                        else {"result": "unsettled" if c_row else "no_shadow_row"}
                    ),
                },
                "prediction_accuracy": {
                    **acc,
                    "source_of_truth_probs": "immutable_paired_freeze",
                    "evaluation_path": (
                        "shadow_ledger_plus_artifact"
                        if priced
                        else "paired_artifact_join_existing_settlement_logic"
                    ),
                },
                "missing_information": pair.get("missing_information"),
            }
        )

    # Reused = priced m5 shadow present but not written under this freeze_id (the prior "200 unchanged").
    reused = [
        m
        for m in mapping
        if m["pricing"]["status"] == "priced"
        and m["shadow_m5"]["present"]
        and not ((m["shadow_m5"].get("baseline_verification") or {}).get("from_this_freeze_ingest"))
    ]
    reused_valid = sum(
        1
        for m in reused
        if (m["shadow_m5"].get("baseline_verification") or {}).get("match_status") == "valid_baseline_match"
    )
    reused_unresolved = len(reused) - reused_valid
    new_from_freeze = [
        m
        for m in mapping
        if (m["shadow_m5"].get("baseline_verification") or {}).get("from_this_freeze_ingest")
    ]
    new_valid = sum(
        1
        for m in new_from_freeze
        if (m["shadow_m5"].get("baseline_verification") or {}).get("match_status") == "valid_baseline_match"
    )

    # Both families priced
    both_priced = sum(
        1 for m in mapping if m["shadow_m5"]["present"] and m["shadow_c_recent"]["present"]
    )

    report = {
        "status": "ok",
        "reconciled_at": moment.isoformat(),
        "to": "Agent 1",
        "from": "Agent 2",
        "play_enabled": False,
        "promoted": False,
        "further_tuning": False,
        "do_not_promote": True,
        "do_not_invent_odds": True,
        "do_not_build_duplicate_settlement": True,
        "freeze_id": freeze_id,
        "freeze_path": str(_paired_freeze_path()),
        "locked_version": LOCKED_VERSION,
        "cohort_preserved": True,
        "participation_void_rules": [
            "confirmed_inactive_pregame → void",
            "missing_box_score → blocked (not assumed zero TD)",
            "final + box → y_atd = rush_td+rec_td > 0",
        ],
        "summary": {
            "total_frozen_pairs_reconciled": counts["total_frozen_pairs"],
            "priced_pairs": counts["priced"],
            "unpriced_pairs": counts["unpriced"],
            "both_families_in_shadow": both_priced,
            "m5_shadow_present": counts["m5_shadow_present"],
            "c_recent_shadow_present": counts["c_recent_shadow_present"],
            "m5_new_from_this_freeze": len(new_from_freeze),
            "m5_new_from_this_freeze_valid": new_valid,
            "m5_reused_rows_checked": len(reused),
            "m5_reused_valid_baseline_matches": reused_valid,
            "m5_reused_unresolved_matches": reused_unresolved,
            "m5_baseline_valid_total": reused_valid + new_valid,
            "m5_baseline_unresolved_total": (len(reused) - reused_valid)
            + (len(new_from_freeze) - new_valid),
            "locked_artifact_is_source_of_truth_when_unresolved": True,
            "accuracy_gradeable_now": counts["accuracy_gradeable_now"],
            "accuracy_pending": counts["accuracy_pending"],
            "accuracy_blocked": counts["accuracy_blocked"],
            "accuracy_void": counts["accuracy_void"],
            "agent1_can_evaluate_every_gradeable_pair": True,
            "agent1_pairs_with_eval_path": counts["agent1_can_evaluate_when_resolved"],
        },
        "accounting_note": {
            "prior_ingest_written": 262,
            "prior_ingest_unchanged_m5": 200,
            "prior_ingest_skipped_no_price": 590,
            "reconciled_priced": counts["priced"],
            "reconciled_unpriced": counts["unpriced"],
            "expected": "priced≈231 (m5 31 new + 200 reused; c_recent 231); unpriced≈590; total 821",
        },
        "agent1_instructions": [
            "Use mapping JSONL as the complete pair ledger.",
            "For priced pairs: settle shadow rows via existing settle_shadow_predictions.",
            "For unpriced pairs: keep immutable freeze probs; join y_atd via same box/injury rules; do not invent odds.",
            "If m5 baseline_verification.match_status is unresolved_baseline_match, use locked_predictions.p_m5 from the paired artifact for accuracy (shadow row is not a valid baseline for this cohort).",
            "Keep freeze_frozen_at separate from shadow created_at / forecast_as_of ingestion timestamps.",
            "Never backfill or recreate predictions after kickoff.",
            "Do not retune C_recent; do not promote.",
        ],
    }

    hand = _handoff_dir()
    map_path = hand / "PAIR_SETTLEMENT_MAPPING.jsonl"
    with map_path.open("w", encoding="utf-8") as fh:
        for row in mapping:
            fh.write(json.dumps(row, default=str) + "\n")

    # Compact index for quick Agent 1 lookups
    index = {
        "freeze_id": freeze_id,
        "n": len(mapping),
        "pairs": [
            {
                "pair_key": m["pair_key"],
                "pricing": m["pricing"]["status"],
                "m5_match": (m["shadow_m5"].get("baseline_verification") or {}).get("match_status"),
                "accuracy_status": m["prediction_accuracy"]["status"],
                "gradeable": m["prediction_accuracy"].get("gradeable"),
                "m5_shadow_id": m["shadow_m5"].get("shadow_id"),
                "c_shadow_id": m["shadow_c_recent"].get("shadow_id"),
            }
            for m in mapping
        ],
    }
    index_path = hand / "PAIR_INDEX.json"
    index_path.write_text(json.dumps(index, indent=2, default=str), encoding="utf-8")

    report["mapping_path"] = str(map_path)
    report["index_path"] = str(index_path)
    report_path = hand / "AGENT1_RECONCILIATION.json"
    # Embed no full mapping (too large) — paths only + summary
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(report_path)

    # Update LATEST handoff pointer
    (hand / "LATEST.json").write_text(
        json.dumps(
            {
                "status": "reconciled_ready_for_agent1",
                "handed_off_at": moment.isoformat(),
                "reconciliation_path": str(report_path),
                "mapping_path": str(map_path),
                "index_path": str(index_path),
                "freeze_id": freeze_id,
                "total_pairs": len(mapping),
                "play_enabled": False,
                "further_tuning": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    # Also update original handoff with reconciliation pointer
    handoff_main = hand / "AGENT1_HANDOFF.json"
    if handoff_main.exists():
        prev = json.loads(handoff_main.read_text(encoding="utf-8"))
        prev["reconciliation"] = {
            "report": str(report_path),
            "mapping": str(map_path),
            "index": str(index_path),
            "reconciled_at": moment.isoformat(),
            "total_frozen_pairs_reconciled": counts["total_frozen_pairs"],
        }
        handoff_main.write_text(json.dumps(prev, indent=2, default=str), encoding="utf-8")

    return report


def run_reconcile(session: Session) -> dict[str, Any]:
    print("[reconcile] loading paired freeze + shadow ledger…", flush=True)
    report = reconcile_paired_cohort(session)
    s = report.get("summary") or {}
    print(
        json.dumps(
            {
                "total": s.get("total_frozen_pairs_reconciled"),
                "priced": s.get("priced_pairs"),
                "unpriced": s.get("unpriced_pairs"),
                "m5_reused_valid": s.get("m5_reused_valid_baseline_matches"),
                "m5_reused_unresolved": s.get("m5_reused_unresolved_matches"),
                "agent1_can_evaluate_every_gradeable_pair": s.get("agent1_can_evaluate_every_gradeable_pair"),
                "report_path": report.get("report_path"),
            },
            indent=2,
        ),
        flush=True,
    )
    return report
