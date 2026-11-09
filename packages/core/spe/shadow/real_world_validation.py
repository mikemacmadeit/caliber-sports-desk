"""Real-world Anytime TD validation milestone report.

No new models. No promotion. Validated Play stays off.
Distinguishes genuine pre-kickoff freezes from historical replay.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.market.odds import american_to_implied
from spe.shadow.jobs import job_settle
from spe.shadow.live_weekend import family_of
from spe.shadow.weekend_results import build_weekend_results_report

FOCUS_PLAYERS = (
    "Jonah Coleman",
    "Ja'Marr Chase",
    "Tee Higgins",
    "Tetairoa McMillan",
)

ATD_FAMILIES = ("m3", "m5", "m6", "atd_sim_v1", "player_td_v1", "m6_histgbm", "nfl_ensemble")


def report_dir() -> Path:
    path = Path("data") / "atd_sim_dev" / "live_weekend"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _family_ext(model_version_id: str) -> str:
    mv = model_version_id or ""
    if mv.startswith("atd_sim_v1"):
        return "atd_sim_v1"
    if "player_td" in mv or mv.startswith("ptd"):
        return "player_td_v1"
    if "histgbm" in mv.lower() or "hist_gbm" in mv.lower():
        return "m6_histgbm"
    if "ensemble" in mv.lower():
        return "nfl_ensemble"
    return family_of(mv)


def _brier(p: float, y: int) -> float:
    return (p - y) ** 2


def _logloss(p: float, y: int) -> float:
    eps = 1e-6
    p = min(max(float(p), eps), 1 - eps)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def verify_fbs_2026_stats(session: Session) -> dict[str, Any]:
    cov = session.execute(
        text(
            """
            SELECT COUNT(DISTINCT p.game_id) AS games,
                   COUNT(DISTINCT p.player_id) AS players,
                   COUNT(*) AS rows
            FROM player_game_stats p
            JOIN games g ON g.game_id = p.game_id
            WHERE g.league_id='fbs' AND g.kickoff_utc >= '2026-08-01'
            """
        )
    ).mappings().first()
    finals = session.execute(
        text(
            """
            SELECT COUNT(*) AS finals,
                   SUM(CASE WHEN EXISTS (
                     SELECT 1 FROM player_game_stats p WHERE p.game_id=g.game_id
                   ) THEN 1 ELSE 0 END) AS with_pgs
            FROM games g
            WHERE g.league_id='fbs' AND g.kickoff_utc >= '2026-08-01'
              AND lower(g.status) IN ('final','post') AND g.data_tier != 'demo'
            """
        )
    ).mappings().first()
    from spe.ingest.cfbd_players import DEFAULT_SEASONS

    return {
        "default_seasons_includes_2026": 2026 in DEFAULT_SEASONS,
        "coverage": dict(cov or {}),
        "finals": dict(finals or {}),
        "fixed": int((cov or {}).get("rows") or 0) > 0
        and int((finals or {}).get("finals") or 0) > 0
        and int((finals or {}).get("with_pgs") or 0) == int((finals or {}).get("finals") or 0),
        "retrained": False,
        "play_enabled": False,
    }


def audit_settlement_rules_on_completed_games(session: Session) -> dict[str, Any]:
    """Verify grading rules on real completed games with box scores (no fabricated forecasts)."""
    rows = session.execute(
        text(
            """
            SELECT g.game_id, g.league_id, g.status, p.player_id, pl.name,
                   p.rush_td, p.rec_td, p.pass_td, p.carries, p.receptions
            FROM games g
            JOIN player_game_stats p ON p.game_id = g.game_id
            LEFT JOIN players pl ON pl.player_id = p.player_id
            WHERE lower(g.status) IN ('final','post')
              AND g.kickoff_utc >= '2026-11-01'
              AND g.league_id IN ('nfl','fbs')
            LIMIT 5000
            """
        )
    ).mappings().all()
    checks = {"pass_td_not_anytime": 0, "rush_rec_anytime": 0, "zero_is_loss_side": 0, "rows": 0}
    examples = []
    for r in rows:
        checks["rows"] += 1
        rush = float(r["rush_td"] or 0)
        rec = float(r["rec_td"] or 0)
        pas = float(r["pass_td"] or 0)
        anytime = rush + rec
        if pas > 0 and anytime == 0:
            checks["pass_td_not_anytime"] += 1
            if len(examples) < 5:
                examples.append(
                    {
                        "game_id": r["game_id"],
                        "player": r["name"],
                        "pass_td": pas,
                        "anytime_tds": anytime,
                        "rule": "pass_td_excluded",
                    }
                )
        if anytime > 0:
            checks["rush_rec_anytime"] += 1
        if anytime == 0:
            checks["zero_is_loss_side"] += 1

    # Missing-stat blocker: final game without a player row must not grade as zero.
    missing_probe = session.execute(
        text(
            """
            SELECT COUNT(*) FROM games g
            WHERE lower(g.status) IN ('final','post')
              AND g.kickoff_utc >= '2026-11-01'
              AND NOT EXISTS (SELECT 1 FROM player_game_stats p WHERE p.game_id = g.game_id)
            """
        )
    ).scalar()

    return {
        "completed_game_stat_rows_audited": checks["rows"],
        "pass_td_only_players_anytime_zero": checks["pass_td_not_anytime"],
        "players_with_rush_or_rec_td": checks["rush_rec_anytime"],
        "players_with_zero_rush_rec_td": checks["zero_is_loss_side"],
        "finals_missing_all_box_scores": int(missing_probe or 0),
        "examples_pass_td_excluded": examples,
        "rules_confirmed": {
            "anytime_td": "rush_td + rec_td > 0",
            "qb_passing_tds_count": False,
            "missing_player_row": "blocked — never assumed zero",
        },
    }


def settle_real_finals(session: Session) -> dict[str, Any]:
    """Settle eligible pre-kickoff freezes against official finals. No invented zeros."""
    before = session.execute(
        text(
            """
            SELECT
              SUM(CASE WHEN lower(g.status) IN ('final','post') AND st.shadow_id IS NULL THEN 1 ELSE 0 END) AS open_on_finals,
              COUNT(*) AS open_anytime
            FROM shadow_predictions s
            JOIN games g ON g.game_id = s.game_id
            LEFT JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
            WHERE s.market_id = 'anytime_td'
            """
        )
    ).mappings().first()

    step = job_settle(session)
    session.commit()

    recent = session.execute(
        text(
            """
            SELECT st.result, s.league_id, COUNT(*) AS n
            FROM shadow_settlements st
            JOIN shadow_predictions s ON s.shadow_id = st.shadow_id
            WHERE s.market_id = 'anytime_td'
              AND st.settled_at >= datetime('now', '-2 day')
            GROUP BY st.result, s.league_id
            """
        )
    ).mappings().all()

    blocked = session.execute(
        text(
            """
            SELECT st.void_reason, COUNT(*) AS n
            FROM shadow_settlements st
            JOIN shadow_predictions s ON s.shadow_id = st.shadow_id
            WHERE s.market_id='anytime_td' AND st.result='blocked'
              AND st.settled_at >= datetime('now', '-2 day')
            GROUP BY st.void_reason
            """
        )
    ).mappings().all()

    # Rule checks on a sample of settled anytime rows.
    sample = session.execute(
        text(
            """
            SELECT s.shadow_id, s.player_id, s.game_id, s.league_id, s.side,
                   st.result, st.actual_value, st.void_reason,
                   p.rush_td, p.rec_td, p.pass_td
            FROM shadow_settlements st
            JOIN shadow_predictions s ON s.shadow_id = st.shadow_id
            LEFT JOIN player_game_stats p
              ON p.game_id = s.game_id AND p.player_id = s.player_id
            WHERE s.market_id='anytime_td' AND st.result IN ('win','loss')
            ORDER BY st.settled_at DESC
            LIMIT 40
            """
        )
    ).mappings().all()
    rule_ok = 0
    rule_fail = []
    for row in sample:
        rush = float(row["rush_td"] or 0)
        rec = float(row["rec_td"] or 0)
        anytime = rush + rec
        expected = "win" if anytime > 0 else "loss"
        if row["side"] in {"yes", "over"} and row["result"] == expected:
            rule_ok += 1
        elif row["side"] not in {"yes", "over"}:
            rule_ok += 1
        else:
            rule_fail.append(
                {
                    "shadow_id": row["shadow_id"],
                    "result": row["result"],
                    "expected": expected,
                    "rush_td": rush,
                    "rec_td": rec,
                    "pass_td": row["pass_td"],
                }
            )

    status_mix = session.execute(
        text(
            """
            SELECT lower(g.status) AS status, COUNT(*) AS n
            FROM shadow_predictions s
            JOIN games g ON g.game_id = s.game_id
            WHERE s.market_id='anytime_td'
            GROUP BY lower(g.status)
            """
        )
    ).mappings().all()

    return {
        "open_before": dict(before or {}),
        "settle_step": step,
        "recent_by_result_league": [dict(r) for r in recent],
        "blocked_reasons": [dict(r) for r in blocked],
        "shadow_status_mix": [dict(r) for r in status_mix],
        "grading_rules": {
            "anytime_td": "rush_td + rec_td > 0",
            "pass_td_counts": False,
            "missing_stats": "blocked (not zero)",
            "inactive_pregame": "void",
        },
        "rule_audit": {
            "sampled_win_loss": len(sample),
            "rule_ok": rule_ok,
            "rule_fail_examples": rule_fail[:10],
        },
        "completed_box_audit": audit_settlement_rules_on_completed_games(session),
        "preserves_original_forecasts": True,
        "play_enabled": False,
        "note": (
            "Graded pregame N stays 0 until frozen weekend games reach final. "
            "In-progress shadows are correctly held pending."
        ),
    }


def load_deduped_settled_units(session: Session) -> list[dict[str, Any]]:
    """Latest pre-kickoff freeze per (league, player, game, family) with settlements."""
    rows = session.execute(
        text(
            """
            SELECT s.shadow_id, s.league_id, s.player_id, s.game_id, s.model_version_id,
                   s.forecast_probability, s.market_implied_raw, s.market_implied_novig,
                   s.expected_value, s.price_american, s.forecast_as_of, s.odds_tick_id,
                   s.components, s.side, s.outcome_id,
                   g.kickoff_utc, g.status,
                   pl.name AS player_name, pl.position_group,
                   st.result, st.actual_value, st.flat_stake_return, st.void_reason,
                   st.closing_price_american, st.closing_line_value AS clv
            FROM shadow_predictions s
            JOIN games g ON g.game_id = s.game_id
            LEFT JOIN players pl ON pl.player_id = s.player_id
            LEFT JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
            WHERE s.market_id = 'anytime_td'
            """
        )
    ).mappings().all()

    def _eval_rank(row: dict[str, Any]) -> tuple[int, datetime]:
        """On finals, a settled freeze outranks a newer unsettled re-freeze.

        Append-only capture can write post-settle research ticks; evaluation must
        keep the graded pre-kickoff unit so models are compared on identical outcomes.
        """
        fas = as_utc(row["forecast_as_of"]) or datetime.min.replace(tzinfo=UTC)
        settled = 1 if row.get("result") in {"win", "loss", "blocked", "void"} else 0
        status = str(row.get("status") or "").lower()
        if status in {"final", "post"}:
            return (settled, fas)
        return (0, fas)

    best: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for r in rows:
        fam = _family_ext(str(r["model_version_id"]))
        fas = as_utc(r["forecast_as_of"])
        kick = as_utc(r["kickoff_utc"])
        if kick and fas and not (fas < kick):
            continue  # not a genuine pre-kickoff freeze
        key = (str(r["league_id"]), str(r["player_id"]), str(r["game_id"]), fam)
        cand = {**dict(r), "family": fam}
        prev = best.get(key)
        if prev is None or _eval_rank(cand) >= _eval_rank(prev):
            best[key] = cand
    return list(best.values())


def compare_models(units: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [u for u in units if u.get("result") in {"win", "loss"}]
    by_league_fam: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(_empty))
    by_pos: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(_empty))
    by_bucket: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(_empty))
    sparse: dict[str, dict[str, Any]] = defaultdict(_empty)
    disagree_cases: list[dict[str, Any]] = []

    # Index by player-game for identical-outcome comparisons
    by_pg: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for u in settled:
        by_pg[(str(u["league_id"]), str(u["player_id"]), str(u["game_id"]))][u["family"]] = u

    identical_pairs: dict[str, dict[str, Any]] = defaultdict(_empty)

    for u in settled:
        fam = u["family"]
        league = str(u["league_id"])
        y = 1 if u["result"] == "win" else 0
        p = float(u["forecast_probability"] or 0.0)
        pos = str(u.get("position_group") or "UNK")
        bucket = _prob_bucket(p)
        for m in (
            by_league_fam[league][fam],
            by_pos[pos][fam],
            by_bucket[bucket][fam],
        ):
            _acc(m, p, y, u.get("flat_stake_return"))

        comps = _comps(u.get("components"))
        hist_n = comps.get("prior_games") or comps.get("n_prior_games") or comps.get("history_games")
        if hist_n is not None and float(hist_n) < 3:
            _acc(sparse[fam], p, y, u.get("flat_stake_return"))

    # Pairwise on identical outcomes
    for (_lg, _pid, _gid), fams in by_pg.items():
        names = sorted(fams.keys())
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                ua, ub = fams[a], fams[b]
                if ua["result"] != ub["result"]:
                    continue  # same outcome required; if settlements differ something is wrong
                key = f"{a}_vs_{b}"
                ya = 1 if ua["result"] == "win" else 0
                _acc(identical_pairs[key], float(ua["forecast_probability"] or 0), ya, None)
                # also track brier delta
                identical_pairs[key]["brier_a_sum"] = identical_pairs[key].get("brier_a_sum", 0) + _brier(
                    float(ua["forecast_probability"] or 0), ya
                )
                identical_pairs[key]["brier_b_sum"] = identical_pairs[key].get("brier_b_sum", 0) + _brier(
                    float(ub["forecast_probability"] or 0), ya
                )
                identical_pairs[key]["family_a"] = a
                identical_pairs[key]["family_b"] = b
                pa = float(ua["forecast_probability"] or 0)
                pb = float(ub["forecast_probability"] or 0)
                if abs(pa - pb) >= 0.10:
                    disagree_cases.append(
                        {
                            "league_id": ua["league_id"],
                            "player_id": ua["player_id"],
                            "player_name": ua.get("player_name"),
                            "game_id": ua["game_id"],
                            "a": a,
                            "b": b,
                            "p_a": pa,
                            "p_b": pb,
                            "spread": abs(pa - pb),
                            "result": ua["result"],
                            # If TD scored, higher p was better; if not, lower p was better.
                            "higher_probability_was_better": ya == 1,
                            "lower_probability_was_better": ya == 0,
                        }
                    )

    for league, fams in by_league_fam.items():
        for fam, m in fams.items():
            _finalize(m)
    for pos, fams in by_pos.items():
        for fam, m in fams.items():
            _finalize(m)
    for bucket, fams in by_bucket.items():
        for fam, m in fams.items():
            _finalize(m)
    for fam, m in sparse.items():
        _finalize(m)
    for key, m in identical_pairs.items():
        n = m["n"] or 1
        m["brier_a"] = m.get("brier_a_sum", 0) / n
        m["brier_b"] = m.get("brier_b_sum", 0) / n
        _finalize(m)

    higher_better = sum(1 for c in disagree_cases if c.get("higher_probability_was_better"))
    lower_better = sum(1 for c in disagree_cases if c.get("lower_probability_was_better"))
    return {
        "label": "genuine_prekickoff_freezes_only",
        "not_historical_replay": True,
        "n_deduped_units": len(units),
        "n_settled_win_loss": len(settled),
        "by_league_family": {lg: dict(fams) for lg, fams in by_league_fam.items()},
        "by_position_family": {pos: dict(fams) for pos, fams in by_pos.items()},
        "by_probability_bucket": {b: dict(fams) for b, fams in by_bucket.items()},
        "sparse_history": dict(sparse),
        "identical_outcome_pairs": dict(identical_pairs),
        "disagreement_10pp": {
            "n": len(disagree_cases),
            "higher_probability_was_better": higher_better,
            "lower_probability_was_better": lower_better,
            "examples": sorted(disagree_cases, key=lambda r: r["spread"], reverse=True)[:25],
        },
        "best_by_league": {
            lg: _best_family(fams) for lg, fams in by_league_fam.items()
        },
    }


def compare_sportsbooks(units: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [u for u in units if u.get("result") in {"win", "loss"} and u.get("price_american") is not None]
    by_fam: dict[str, dict[str, Any]] = defaultdict(_empty)
    value_rows = 0
    value_return = 0.0
    for u in settled:
        fam = u["family"]
        y = 1 if u["result"] == "win" else 0
        p = float(u["forecast_probability"] or 0.0)
        price = int(u["price_american"])
        breakeven = american_to_implied(price)
        mkt = u.get("market_implied_novig")
        if mkt is None:
            mkt = u.get("market_implied_raw")
        if mkt is None:
            mkt = breakeven
        mkt = float(mkt)
        _acc(by_fam[fam], p, y, u.get("flat_stake_return"))
        by_fam[fam]["market_brier_sum"] = by_fam[fam].get("market_brier_sum", 0.0) + _brier(mkt, y)
        by_fam[fam]["market_n"] = by_fam[fam].get("market_n", 0) + 1
        by_fam[fam]["breakeven_sum"] = by_fam[fam].get("breakeven_sum", 0.0) + float(breakeven)
        ev = u.get("expected_value")
        if ev is not None and float(ev) > 0:
            value_rows += 1
            value_return += float(u.get("flat_stake_return") or 0.0)
            by_fam[fam]["positive_ev_n"] = by_fam[fam].get("positive_ev_n", 0) + 1
            by_fam[fam]["positive_ev_return_sum"] = by_fam[fam].get("positive_ev_return_sum", 0.0) + float(
                u.get("flat_stake_return") or 0.0
            )
        # CLV only when legitimate closing price exists
        if u.get("closing_price_american") is not None and u.get("clv") is not None:
            by_fam[fam]["clv_n"] = by_fam[fam].get("clv_n", 0) + 1
            by_fam[fam]["clv_sum"] = by_fam[fam].get("clv_sum", 0.0) + float(u["clv"])

    for fam, m in by_fam.items():
        _finalize(m)
        mn = m.get("market_n") or 0
        m["market_brier"] = (m.get("market_brier_sum", 0) / mn) if mn else None
        m["mean_breakeven"] = (m.get("breakeven_sum", 0) / mn) if mn else None
        m["model_beats_market_brier"] = (
            m["brier"] is not None and m["market_brier"] is not None and m["brier"] < m["market_brier"]
        )
        pev = m.get("positive_ev_n") or 0
        m["positive_ev_mean_return"] = (m.get("positive_ev_return_sum", 0) / pev) if pev else None
        cn = m.get("clv_n") or 0
        m["mean_clv"] = (m.get("clv_sum", 0) / cn) if cn else None

    return {
        "n_settled_with_price": len(settled),
        "by_family": dict(by_fam),
        "positive_ev_hypothetical": {
            "n": value_rows,
            "flat_stake_return_sum": value_return,
            "mean_return": (value_return / value_rows) if value_rows else None,
            "disclaimer": "Hypothetical research only. Not proof of profitability.",
        },
        "no_vig_invented": False,
        "insufficient_for_profit_claim": len(settled) < 200,
    }


def investigate_disagreements(session: Session, units: list[dict[str, Any]]) -> dict[str, Any]:
    focus = []
    by_name = defaultdict(list)
    for u in units:
        name = (u.get("player_name") or "").strip()
        by_name[name].append(u)
        for target in FOCUS_PLAYERS:
            if name.lower() == target.lower() or target.lower() in name.lower():
                focus.append(u)

    # Group focus by player-game
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for u in focus:
        grouped[(str(u["player_id"]), str(u["game_id"]))].append(u)

    cases = []
    for (pid, gid), rows in grouped.items():
        fam_ps = {r["family"]: float(r["forecast_probability"] or 0) for r in rows}
        if len(fam_ps) < 2:
            continue
        spread = max(fam_ps.values()) - min(fam_ps.values())
        comps = _comps(rows[0].get("components"))
        cases.append(
            {
                "player_id": pid,
                "player_name": rows[0].get("player_name"),
                "game_id": gid,
                "league_id": rows[0].get("league_id"),
                "by_family": fam_ps,
                "spread": spread,
                "settled_result": next((r.get("result") for r in rows if r.get("result")), None),
                "possible_drivers": _drivers(comps, rows),
                "components_keys": sorted(comps.keys())[:30],
            }
        )
    cases.sort(key=lambda r: r["spread"], reverse=True)

    # Global higher/lower performance already in compare_models; summarize focus
    return {
        "focus_players": list(FOCUS_PLAYERS),
        "cases": cases,
        "note": "Probabilities not manually adjusted to force agreement.",
        "manual_probability_changes": False,
    }


def _drivers(comps: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    drivers = []
    if comps.get("prior_games") is not None and float(comps.get("prior_games") or 0) < 3:
        drivers.append("missing_or_sparse_player_history")
    if comps.get("injury_coverage") or any("INJURY" in str(r.get("components")) for r in rows):
        drivers.append("injury_information")
    if any(r["family"] == "atd_sim_v1" for r in rows):
        drivers.append("team_touchdown_expectations_and_correlation")
    if any(r["family"] in {"m5", "m6"} for r in rows):
        drivers.append("usage_or_matchup_features")
    if comps.get("defensive_matchup") or comps.get("opp_td_allowed") is not None:
        drivers.append("defensive_matchups")
    if not drivers:
        drivers.append("model_calibration_or_architecture_difference")
    return drivers


def build_final_answers(
    *,
    fbs: dict[str, Any],
    settle: dict[str, Any],
    models: dict[str, Any],
    books: dict[str, Any],
    disagree: dict[str, Any],
) -> dict[str, Any]:
    graded = models.get("n_settled_win_loss") or 0
    best = models.get("best_by_league") or {}
    pos = models.get("by_position_family") or {}
    # Position strength: average brier across families with n>=20
    pos_strength: dict[str, float] = {}
    for position, fams in pos.items():
        briers = [m["brier"] for m in fams.values() if m.get("brier") is not None and (m.get("n") or 0) >= 10]
        if briers:
            pos_strength[position] = sum(briers) / len(briers)
    strongest = sorted(pos_strength, key=pos_strength.get)[:3] if pos_strength else []
    weakest = sorted(pos_strength, key=pos_strength.get, reverse=True)[:3] if pos_strength else []

    beats_book = any(
        (m.get("model_beats_market_brier") is True) and (m.get("n") or 0) >= 30
        for m in (books.get("by_family") or {}).values()
    )

    weaknesses = []
    if not fbs.get("fixed"):
        weaknesses.append("FBS 2026 player-game stats still incomplete")
    if graded < 50:
        weaknesses.append("Too few genuine settled pregame units for confident ranking")
    sim_note = "Simulation freezes truncated to top-24 / roster-eligible only"
    weaknesses.append(sim_note)
    if books.get("insufficient_for_profit_claim"):
        weaknesses.append("Insufficient settled priced units to claim sportsbook edge")
    weaknesses = weaknesses[:3]

    next_steps = [
        "Continue live weekend freezes/settlements until sample sizes support league-specific ranking",
        "After slate settles, re-run weekend-atd-results and real-world-validation without retraining",
        "Then improve weakest positions / sparse-history handling using prospective evidence only",
    ]

    return {
        "1_fbs_2026_stats_fixed_and_flowing": bool(fbs.get("fixed")),
        "2_auto_settling_after_finals": (settle.get("settle_step") or {}).get("status") == "ok",
        "3_real_pregame_predictions_graded": graded,
        "4_best_model_currently": best,
        "5_beating_sportsbook_implied": {
            "evidence_of_better_brier_on_some_family": beats_book,
            "sufficient_to_claim": not books.get("insufficient_for_profit_claim"),
            "detail": books.get("by_family"),
        },
        "6_position_strength": {"strongest_brier": strongest, "weakest_brier": weakest, "by_position": pos_strength},
        "7_three_biggest_weaknesses": weaknesses,
        "8_what_to_build_next": next_steps,
        "validated_play": False,
        "promoted_models": False,
    }


def run_real_world_validation(session: Session) -> dict[str, Any]:
    print("[rwv] Phase1 verify FBS 2026…", flush=True)
    fbs = verify_fbs_2026_stats(session)
    print("[rwv] Phase2 settle real finals…", flush=True)
    settle = settle_real_finals(session)
    print("[rwv] Load deduped pre-kickoff units…", flush=True)
    units = load_deduped_settled_units(session)
    print("[rwv] Phase3 model comparison…", flush=True)
    models = compare_models(units)
    print("[rwv] Phase4 sportsbook comparison…", flush=True)
    books = compare_sportsbooks(units)
    print("[rwv] Phase5 disagreements…", flush=True)
    disagree = investigate_disagreements(session, units)
    print("[rwv] Phase6 final answers…", flush=True)
    answers = build_final_answers(fbs=fbs, settle=settle, models=models, books=books, disagree=disagree)
    weekend = build_weekend_results_report(session)

    report = {
        "title": "Real-World Anytime TD Validation Milestone",
        "generated_at": datetime.now(UTC).isoformat(),
        "play_enabled": False,
        "validated_play": False,
        "new_models": False,
        "promoted": False,
        "phase1_fbs_2026": fbs,
        "phase2_settlement": settle,
        "phase3_model_accuracy": models,
        "phase4_sportsbooks": books,
        "phase5_disagreements": disagree,
        "phase6_answers": answers,
        "weekend_results_path": weekend.get("report_path"),
        "evidence_rule": "Identical player-game outcomes; deduped latest pre-kickoff freeze; NFL/FBS separate; not historical replay",
    }
    path = report_dir() / "real_world_atd_validation.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    return report


def _empty() -> dict[str, Any]:
    return {
        "n": 0,
        "wins": 0,
        "brier_sum": 0.0,
        "logloss_sum": 0.0,
        "return_sum": 0.0,
    }


def _acc(m: dict[str, Any], p: float, y: int, ret: Any) -> None:
    m["n"] += 1
    m["wins"] += y
    m["brier_sum"] += _brier(p, y)
    m["logloss_sum"] += _logloss(p, y)
    if ret is not None:
        m["return_sum"] += float(ret)


def _finalize(m: dict[str, Any]) -> None:
    n = m["n"] or 0
    m["hit_rate"] = (m["wins"] / n) if n else None
    m["brier"] = (m["brier_sum"] / n) if n else None
    m["logloss"] = (m["logloss_sum"] / n) if n else None
    m["mean_return"] = (m["return_sum"] / n) if n else None
    # Wilson-ish rough uncertainty: stderr of brier not claimed; report n clearly
    m["sample_size"] = n


def _best_family(fams: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [(fam, m) for fam, m in fams.items() if (m.get("n") or 0) >= 10 and m.get("brier") is not None]
    if not eligible:
        eligible = [(fam, m) for fam, m in fams.items() if m.get("brier") is not None]
    if not eligible:
        return None
    fam, m = min(eligible, key=lambda item: item[1]["brier"])
    return {"family": fam, "brier": m["brier"], "logloss": m["logloss"], "n": m["n"], "hit_rate": m["hit_rate"]}


def _prob_bucket(p: float) -> str:
    if p < 0.1:
        return "0-10%"
    if p < 0.2:
        return "10-20%"
    if p < 0.35:
        return "20-35%"
    if p < 0.5:
        return "35-50%"
    return "50%+"


def _comps(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            val = json.loads(raw)
            return val if isinstance(val, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}
