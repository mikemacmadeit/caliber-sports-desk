"""Post-weekend Anytime TD evaluation report.

Uses only pre-kickoff freezes and official settlements. No fabricated results.
Validated Play stays off.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.shadow.live_weekend import REQUIRED_FAMILIES, family_of


def report_dir() -> Path:
    path = Path("data") / "atd_sim_dev" / "live_weekend"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _brier(p: float, y: int) -> float:
    return (p - y) ** 2


def _logloss(p: float, y: int) -> float:
    eps = 1e-6
    p = min(max(p, eps), 1 - eps)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def build_weekend_results_report(session: Session) -> dict[str, Any]:
    """Answer the post-weekend evaluation questions from settled shadow units."""
    rows = session.execute(
        text(
            """
            SELECT s.shadow_id, s.league_id, s.player_id, s.game_id, s.model_version_id,
                   s.forecast_probability, s.market_implied_raw, s.market_implied_novig,
                   s.expected_value, s.price_american, s.forecast_as_of, s.components,
                   g.kickoff_utc, g.status,
                   st.result, st.actual_value, st.flat_stake_return, st.void_reason
            FROM shadow_predictions s
            JOIN games g ON g.game_id = s.game_id
            LEFT JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
            WHERE s.market_id = 'anytime_td'
              AND (
                s.model_version_id LIKE 'm3-%'
                OR s.model_version_id LIKE 'm5-%'
                OR s.model_version_id LIKE 'm6-%'
                OR s.model_version_id LIKE 'atd_sim_v1%'
              )
            """
        )
    ).mappings().all()

    # Deduped unit = latest pre-kickoff freeze per (league, player, game, family)
    best: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for r in rows:
        fam = family_of(str(r["model_version_id"]))
        if fam not in REQUIRED_FAMILIES:
            continue
        fas = as_utc(r["forecast_as_of"])
        kick = as_utc(r["kickoff_utc"])
        if kick and fas and not (fas < kick):
            continue
        key = (str(r["league_id"]), str(r["player_id"]), str(r["game_id"]), fam)
        prev = best.get(key)
        if prev is None or as_utc(r["forecast_as_of"]) >= as_utc(prev["forecast_as_of"]):
            best[key] = dict(r)

    units = list(best.values())
    prekickoff_n = len(units)
    settled = [u for u in units if u.get("result") in {"win", "loss"}]
    blocked = [u for u in units if u.get("result") == "blocked"]
    voided = [u for u in units if u.get("result") == "void"]
    pending = [u for u in units if u.get("result") in {None, ""}]

    by_family: dict[str, dict[str, Any]] = {}
    by_league_family: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for u in settled:
        fam = family_of(str(u["model_version_id"]))
        league = str(u["league_id"])
        y = 1 if u["result"] == "win" else 0
        p = float(u["forecast_probability"] or 0.0)
        for bucket in (by_family.setdefault(fam, _empty_metrics()), by_league_family[league].setdefault(fam, _empty_metrics())):
            bucket["n"] += 1
            bucket["wins"] += y
            bucket["brier_sum"] += _brier(p, y)
            bucket["logloss_sum"] += _logloss(p, y)
            bucket["return_sum"] += float(u.get("flat_stake_return") or 0.0)
            mkt = u.get("market_implied_novig")
            if mkt is None:
                mkt = u.get("market_implied_raw")
            if mkt is not None:
                bucket["market_brier_sum"] += _brier(float(mkt), y)
                bucket["market_n"] += 1
            ev = u.get("expected_value")
            if ev is not None and float(ev) > 0:
                bucket["positive_ev_n"] += 1
                bucket["positive_ev_return_sum"] += float(u.get("flat_stake_return") or 0.0)

    for fam, m in by_family.items():
        _finalize_metrics(m)
    for league, fams in by_league_family.items():
        for fam, m in fams.items():
            _finalize_metrics(m)

    # Strong disagreements: same player/game, max-min p across families >= 0.15
    by_pg: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    by_pg_settle: dict[tuple[str, str], str] = {}
    for u in units:
        fam = family_of(str(u["model_version_id"]))
        key = (str(u["player_id"]), str(u["game_id"]))
        by_pg[key][fam] = float(u["forecast_probability"] or 0.0)
        if u.get("result") in {"win", "loss"}:
            by_pg_settle[key] = str(u["result"])

    disagreements = []
    for (pid, gid), fam_ps in by_pg.items():
        if len(fam_ps) < 2:
            continue
        vals = list(fam_ps.values())
        spread = max(vals) - min(vals)
        if spread < 0.15:
            continue
        comps_raw = next((u.get("components") for u in units if u["player_id"] == pid and u["game_id"] == gid), {})
        if isinstance(comps_raw, str):
            try:
                comps = json.loads(comps_raw)
            except json.JSONDecodeError:
                comps = {}
        elif isinstance(comps_raw, dict):
            comps = comps_raw
        else:
            comps = {}
        disagreements.append(
            {
                "player_id": pid,
                "player_name": comps.get("player_name"),
                "game_id": gid,
                "spread": spread,
                "by_family": fam_ps,
                "settled_result": by_pg_settle.get((pid, gid)),
            }
        )
    disagreements.sort(key=lambda r: r["spread"], reverse=True)

    report = {
        "title": "Weekend Anytime TD Results",
        "generated_at": datetime.now(UTC).isoformat(),
        "play_enabled": False,
        "validated_play": False,
        "rule": "latest pre-kickoff freeze per (league, player, game, family)",
        "q1_prekickoff_predictions": prekickoff_n,
        "q2_successfully_settled": {
            "win_loss": len(settled),
            "blocked": len(blocked),
            "void": len(voided),
            "pending": len(pending),
            "blocked_reasons": _count_field(blocked, "void_reason"),
        },
        "q3_accuracy_by_model": {
            fam: {"n": m["n"], "hit_rate": m["hit_rate"], "mean_return": m["mean_return"]}
            for fam, m in sorted(by_family.items())
        },
        "q4_calibration_by_model": {
            fam: {"n": m["n"], "brier": m["brier"], "logloss": m["logloss"]}
            for fam, m in sorted(by_family.items())
        },
        "q5_value_vs_book": {
            fam: {
                "positive_ev_n": m["positive_ev_n"],
                "positive_ev_mean_return": m["positive_ev_mean_return"],
                "model_brier": m["brier"],
                "market_brier": m["market_brier"],
            }
            for fam, m in sorted(by_family.items())
        },
        "q6_strong_disagreements": {
            "n": len(disagreements),
            "top": disagreements[:25],
            "note": "Large model spreads are diagnostic of reliability, not auto-bets.",
        },
        "by_league": {
            league: {fam: m for fam, m in fams.items()} for league, fams in by_league_family.items()
        },
        "insufficient_settlements": len(settled) == 0,
    }
    path = report_dir() / "weekend_atd_results.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    return report


def _empty_metrics() -> dict[str, Any]:
    return {
        "n": 0,
        "wins": 0,
        "brier_sum": 0.0,
        "logloss_sum": 0.0,
        "return_sum": 0.0,
        "market_brier_sum": 0.0,
        "market_n": 0,
        "positive_ev_n": 0,
        "positive_ev_return_sum": 0.0,
    }


def _finalize_metrics(m: dict[str, Any]) -> None:
    n = m["n"] or 0
    m["hit_rate"] = (m["wins"] / n) if n else None
    m["brier"] = (m["brier_sum"] / n) if n else None
    m["logloss"] = (m["logloss_sum"] / n) if n else None
    m["mean_return"] = (m["return_sum"] / n) if n else None
    m["market_brier"] = (m["market_brier_sum"] / m["market_n"]) if m["market_n"] else None
    m["positive_ev_mean_return"] = (
        (m["positive_ev_return_sum"] / m["positive_ev_n"]) if m["positive_ev_n"] else None
    )


def _count_field(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = str(r.get(field) or "unknown")
        out[k] = out.get(k, 0) + 1
    return out
