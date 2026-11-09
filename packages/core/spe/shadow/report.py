"""Shadow track-record metrics. Demo recommendations stay out of this ledger."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from spe.db.models import JobRun, ShadowPrediction, ShadowSettlement
from spe.models.metrics import brier_score, expected_calibration_error, log_loss


def shadow_track_record(session: Session) -> dict[str, Any]:
    preds = session.query(ShadowPrediction).order_by(ShadowPrediction.forecast_as_of.desc()).all()
    settles = {row.shadow_id: row for row in session.query(ShadowSettlement).all()}
    jobs = session.query(JobRun).order_by(JobRun.started_at.desc()).limit(40).all()

    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    pending = settled = blocked = 0
    abstain_counts: dict[str, int] = defaultdict(int)
    rows_out: list[dict[str, Any]] = []
    games = set()
    for pred in preds:
        games.add(pred.game_id)
        settle = settles.get(pred.shadow_id)
        for reason in pred.abstain_reasons or []:
            abstain_counts[reason] += 1
        status = "pending"
        if settle is not None:
            status = settle.result
            if settle.result == "blocked":
                blocked += 1
            else:
                settled += 1
        else:
            pending += 1
        family = _family(pred.model_version_id)
        position = (pred.components or {}).get("position_group") or "UNK"
        item = {
            "shadow_id": pred.shadow_id,
            "player_id": pred.player_id,
            "player": (pred.components or {}).get("player_name"),
            "game_id": pred.game_id,
            "league_id": pred.league_id,
            "market_id": pred.market_id,
            "model_version_id": pred.model_version_id,
            "family": family,
            "position_group": position,
            "line": pred.line,
            "side": pred.side,
            "price_american": pred.price_american,
            "p": pred.forecast_probability,
            "market_implied_raw": pred.market_implied_raw,
            "market_implied_novig": pred.market_implied_novig,
            "expected_value": pred.expected_value,
            "data_quality_status": pred.data_quality_status,
            "abstain_reasons": pred.abstain_reasons,
            "recommendation_status": pred.recommendation_status,
            "forecast_as_of": pred.forecast_as_of,
            "settlement": status,
            "actual_value": None if settle is None else settle.actual_value,
            "flat_stake_return": None if settle is None else settle.flat_stake_return,
            "closing_line_value": None if settle is None else settle.closing_line_value,
        }
        rows_out.append(item)
        if settle is not None and settle.result in {"win", "loss"}:
            y = 1.0 if settle.result == "win" else 0.0
            buckets[(pred.league_id, pred.market_id, family)].append({"p": pred.forecast_probability, "y": y, "ret": settle.flat_stake_return})
            if pred.market_id == "rushing_yards" and position == "QB":
                buckets[(pred.league_id, "qb_rushing_yards", family)].append(
                    {"p": pred.forecast_probability, "y": y, "ret": settle.flat_stake_return}
                )

    comparisons = []
    for key, values in sorted(buckets.items()):
        league_id, market_id, family = key
        y = np.array([row["y"] for row in values], dtype=float)
        p = np.array([row["p"] for row in values], dtype=float)
        rets = [row["ret"] for row in values if row["ret"] is not None]
        comparisons.append(
            {
                "league_id": league_id,
                "market_id": market_id,
                "family": family,
                "n": int(len(values)),
                "brier": brier_score(y, p),
                "log_loss": log_loss(y, p),
                "ece": expected_calibration_error(y, p),
                "flat_stake_roi": float(np.mean(rets)) if rets else None,
            }
        )

    m3_vs_m4 = _paired_comparison(preds, settles)
    independence = _independence_counts(preds, settles)
    atd_scorecard = _atd_scorecard(preds, settles)
    from spe.ops.freshness import operations_health
    from spe.shadow.ats import ats_track_record

    health = operations_health(session)
    picks_scorecard = ats_track_record(session)
    return {
        "play_enabled": False,
        "research_only": True,
        "demo_excluded": True,
        "totals": {
            "games_monitored": len(games),
            "predictions": len(preds),
            "m3": sum(1 for row in preds if _family(row.model_version_id) == "m3"),
            "m4": sum(1 for row in preds if _family(row.model_version_id) == "m4"),
            "m5": sum(1 for row in preds if _family(row.model_version_id) == "m5"),
            "m6": sum(1 for row in preds if _family(row.model_version_id) == "m6"),
            "atd_sim_v1": sum(1 for row in preds if _family(row.model_version_id) == "atd_sim_v1"),
            "blend_v1_c_recent": sum(1 for row in preds if _family(row.model_version_id) == "blend_v1_c_recent"),
            "m5_c_recent": sum(1 for row in preds if _family(row.model_version_id) == "m5_c_recent"),
            "player_td_v1": sum(1 for row in preds if _family(row.model_version_id) == "player_td_v1"),
            "settled": settled,
            "pending": pending,
            "blocked": blocked,
        },
        "atd_scorecard": atd_scorecard,
        "picks_scorecard": picks_scorecard,
        "independence": independence,
        "abstain_reason_counts": dict(sorted(abstain_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_market": comparisons,
        "m3_vs_m4": m3_vs_m4,
        "operations": health,
        "injury_coverage": health["injury_coverage"],
        "odds_freshness": health["odds_freshness"],
        "job_execution": health["job_execution"],
        "jobs": [
            {
                "job_name": job.job_name,
                "status": job.status,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "error": job.error,
                "summary": job.summary,
            }
            for job in jobs
        ],
        "rows": rows_out[:500],
        "disclaimer": (
            "Shadow results are research-only. Validated Play is disabled. "
            "Research-line backtests are not market results. Demo recommendations are excluded. "
            "M3/M4 pairs and repeated captures of the same outcome are not independent evidence."
        ),
    }


def _family(model_version_id: str) -> str:
    """Map model_version_id → ledger family label.

    Order matters: longer / more specific prefixes before short ones
    (e.g. m5_c_recent before m5, blend before generic other).
    """
    mv = model_version_id or ""
    if mv.startswith("blend_v1_c_recent") or mv.startswith("blend_"):
        return "blend_v1_c_recent"
    if mv.startswith("m5_c_recent"):
        return "m5_c_recent"
    if mv.startswith("player_td_v1"):
        return "player_td_v1"
    if mv.startswith("ens_m6_gbm_player_td"):
        return "ens_m6_gbm_player_td"
    if mv.startswith("m6_hist_gbm"):
        return "m6_hist_gbm"
    if mv.startswith("atd_sim_v1"):
        return "atd_sim_v1"
    for fam in ("m6", "m5", "m4", "m3"):
        if mv.startswith(f"{fam}-"):
            return fam
    return "other"


def _atd_scorecard(
    preds: list[ShadowPrediction],
    settles: dict[str, ShadowSettlement],
) -> dict[str, Any]:
    """Win/loss/Brier scoreboard for anytime TD — research universe and desk takes."""
    from spe.shadow.atd_desk_takes import is_desk_take

    primary = ("blend_v1_c_recent", "m5_c_recent", "player_td_v1", "m3", "m5", "m6", "atd_sim_v1")

    def _bucket_rows(only_desk_takes: bool) -> list[dict[str, Any]]:
        by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for pred in preds:
            if pred.market_id != "anytime_td":
                continue
            if only_desk_takes and not is_desk_take(pred):
                continue
            family = _family(pred.model_version_id)
            settle = settles.get(pred.shadow_id)
            if settle is None or settle.result not in {"win", "loss"}:
                continue
            key = (pred.league_id, family)
            bucket = by_key.setdefault(
                key,
                {
                    "league_id": pred.league_id,
                    "family": family,
                    "wins": 0,
                    "losses": 0,
                    "n": 0,
                    "games": set(),
                    "ps": [],
                    "ys": [],
                    "returns": [],
                },
            )
            y = 1.0 if settle.result == "win" else 0.0
            bucket["n"] += 1
            bucket["wins" if settle.result == "win" else "losses"] += 1
            bucket["games"].add(pred.game_id)
            bucket["ps"].append(float(pred.forecast_probability))
            bucket["ys"].append(y)
            if settle.flat_stake_return is not None:
                bucket["returns"].append(float(settle.flat_stake_return))

        rows: list[dict[str, Any]] = []
        for (_league, family), b in by_key.items():
            y = np.array(b["ys"], dtype=float)
            p = np.array(b["ps"], dtype=float)
            rows.append(
                {
                    "league_id": b["league_id"],
                    "family": family,
                    "primary": family in {"blend_v1_c_recent", "m5_c_recent", "player_td_v1"},
                    "desk_takes_only": only_desk_takes,
                    "n": b["n"],
                    "wins": b["wins"],
                    "losses": b["losses"],
                    "games": len(b["games"]),
                    "hit_rate": (b["wins"] / b["n"]) if b["n"] else None,
                    "brier": brier_score(y, p) if b["n"] else None,
                    "flat_stake_roi": float(np.mean(b["returns"])) if b["returns"] else None,
                }
            )
        rows.sort(key=lambda r: (0 if r["primary"] else 1, r["league_id"], -r["n"], r["family"]))
        return rows

    all_rows = _bucket_rows(False)
    take_rows = _bucket_rows(True)
    open_takes = [
        {
            "shadow_id": pred.shadow_id,
            "league_id": pred.league_id,
            "game_id": pred.game_id,
            "player_id": pred.player_id,
            "player_name": (pred.components or {}).get("player_name"),
            "family": _family(pred.model_version_id),
            "p": pred.forecast_probability,
            "price_american": pred.price_american,
            "ev_after_haircut": (pred.components or {}).get("desk_take_ev_after_haircut"),
            "taken_at": (pred.components or {}).get("desk_take_at"),
            "settlement": (settles.get(pred.shadow_id).result if settles.get(pred.shadow_id) else "pending"),
        }
        for pred in preds
        if pred.market_id == "anytime_td" and is_desk_take(pred)
    ]
    return {
        "rows": take_rows,
        "all_research_rows": all_rows,
        "primary_rows": [r for r in take_rows if r["family"] in primary],
        "open_and_settled_takes": open_takes,
        "note": (
            "Anytime TD pick score counts only desk takes — one multifactor pick per game "
            "(chance, edge, freshness, availability), marked before kickoff. Not highest % "
            "alone and not every research row. Validated Play stays off."
        ),
        "research_universe_note": (
            "all_research_rows is the full shadow universe for diagnostics; "
            "the Anytime TD page uses desk takes only."
        ),
    }


def _paired_comparison(preds: list[ShadowPrediction], settles: dict[str, ShadowSettlement]) -> list[dict[str, Any]]:
    """Compare model families on identical outcome and odds tick (settled win/loss only)."""
    atd_fams = {"m3", "m5", "m6", "atd_sim_v1", "blend_v1_c_recent", "m5_c_recent", "player_td_v1"}
    by_key: dict[tuple[str, str], dict[str, ShadowPrediction]] = defaultdict(dict)
    for pred in preds:
        family = _family(pred.model_version_id)
        if family not in {"m3", "m4"} | atd_fams:
            continue
        by_key[(pred.outcome_id, pred.odds_tick_id or "")][family] = pred
    out = []
    for (_outcome, _tick), pair in by_key.items():
        settled: dict[str, tuple[ShadowPrediction, ShadowSettlement]] = {}
        for fam, pred in pair.items():
            st = settles.get(pred.shadow_id)
            if st is not None and st.result in {"win", "loss"}:
                settled[fam] = (pred, st)
        # Legacy M3/M4 pairs
        if "m3" in settled and "m4" in settled:
            a, sa = settled["m3"]
            b, sb = settled["m4"]
            out.append(
                {
                    "league_id": a.league_id,
                    "market_id": a.market_id,
                    "outcome_id": a.outcome_id,
                    "pair_type": "m3_m4",
                    "m3_p": a.forecast_probability,
                    "m4_p": b.forecast_probability,
                    "m3_result": sa.result,
                    "m4_result": sb.result,
                    "m3_return": sa.flat_stake_return,
                    "m4_return": sb.flat_stake_return,
                }
            )
        # Anytime TD: require at least two of M3/M5/M6/sim on the same settled outcome
        atd_present = {f: settled[f] for f in atd_fams if f in settled}
        if len(atd_present) >= 2:
            row: dict[str, Any] = {
                "league_id": next(iter(atd_present.values()))[0].league_id,
                "market_id": next(iter(atd_present.values()))[0].market_id,
                "outcome_id": next(iter(atd_present.values()))[0].outcome_id,
                "pair_type": "atd_multi",
                "families": sorted(atd_present.keys()),
            }
            for fam, (pred, st) in atd_present.items():
                row[f"{fam}_p"] = pred.forecast_probability
                row[f"{fam}_result"] = st.result
                row[f"{fam}_return"] = st.flat_stake_return
            out.append(row)
    return out


def _independence_counts(
    preds: list[ShadowPrediction],
    settles: dict[str, ShadowSettlement],
) -> dict[str, Any]:
    """Deduplicate shadow rows so M3/M4 pairs and recaptures are not over-counted."""
    unique_outcomes = {row.outcome_id for row in preds}
    unique_outcome_ticks = {(row.outcome_id, row.odds_tick_id or "") for row in preds}
    unique_players_games = {(row.player_id, row.game_id, row.market_id) for row in preds}
    paired_ticks = 0
    by_tick: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in preds:
        family = _family(row.model_version_id)
        by_tick[(row.outcome_id, row.odds_tick_id or "")].add(family)
    for families in by_tick.values():
        if "m3" in families and "m4" in families:
            paired_ticks += 1
    settled_unique_outcomes = {
        row.outcome_id
        for row in preds
        if (settle := settles.get(row.shadow_id)) is not None and settle.result in {"win", "loss", "push", "void"}
    }
    return {
        "raw_shadow_rows": len(preds),
        "unique_outcomes": len(unique_outcomes),
        "unique_outcome_price_ticks": len(unique_outcome_ticks),
        "unique_player_game_markets": len(unique_players_games),
        "m3_m4_paired_ticks": paired_ticks,
        "independent_eval_units_approx": paired_ticks or len(unique_outcome_ticks),
        "settled_unique_outcomes": len(settled_unique_outcomes),
        "note": (
            "Do not treat raw_shadow_rows as independent evidence. "
            "Prefer unique_outcomes or m3_m4_paired_ticks for accuracy evaluation."
        ),
    }
