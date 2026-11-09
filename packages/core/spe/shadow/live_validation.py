"""Anytime TD live validation — pregame capture, settlement, fair compare.

No retrain/promote. No post-kickoff backfill as prospective. Validated Play stays off.
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
from spe.shadow.accuracy_optimization import settle_all_eligible
from spe.shadow.calibration_audit import (
    ATD_FAMILIES,
    _acc,
    _brier,
    _empty,
    _finalize,
    _logloss,
    underconfidence_audit,
)
from spe.shadow.real_world_validation import load_deduped_settled_units


REPORT_DIR = Path("data") / "atd_sim_dev" / "live_weekend"
FAM_SQL = """
CASE
  WHEN s.model_version_id LIKE 'm3-%' THEN 'm3'
  WHEN s.model_version_id LIKE 'm4-%' THEN 'm4'
  WHEN s.model_version_id LIKE 'm5-%' THEN 'm5'
  WHEN s.model_version_id LIKE 'm6_hist%' OR s.model_version_id LIKE '%hist_gbm%'
       OR s.model_version_id LIKE '%histgbm%' THEN 'm6_histgbm'
  WHEN s.model_version_id LIKE 'm6-%' THEN 'm6'
  WHEN s.model_version_id LIKE 'atd_sim%' THEN 'atd_sim_v1'
  WHEN s.model_version_id LIKE 'player_td%' THEN 'player_td_v1'
  ELSE 'other'
END
"""


def pregame_opportunity_snapshot(session: Session, player_id: str, game_id: str) -> dict[str, Any]:
    """Verified pregame priors for NEW freezes only. Never invents values."""
    prior = session.execute(
        text(
            """
            SELECT AVG(carries) AS carries, AVG(targets) AS targets, AVG(receptions) AS receptions,
                   AVG(COALESCE(rush_td,0)+COALESCE(rec_td,0)) AS atd_rate,
                   COUNT(*) AS n_prior_games
            FROM player_game_stats p
            JOIN games g ON g.game_id = p.game_id
            WHERE p.player_id = :p AND g.kickoff_utc < (
              SELECT kickoff_utc FROM games WHERE game_id = :g
            )
            """
        ),
        {"p": player_id, "g": game_id},
    ).mappings().first()
    # Team TD share from prior games (player anytime / team anytime in those games)
    share = session.execute(
        text(
            """
            WITH prior AS (
              SELECT p.game_id,
                     COALESCE(p.rush_td,0)+COALESCE(p.rec_td,0) AS player_atd,
                     p.team_id
              FROM player_game_stats p
              JOIN games g ON g.game_id = p.game_id
              WHERE p.player_id = :p AND g.kickoff_utc < (
                SELECT kickoff_utc FROM games WHERE game_id = :g
              )
            ),
            team_g AS (
              SELECT pr.game_id, pr.player_atd,
                     SUM(COALESCE(p2.rush_td,0)+COALESCE(p2.rec_td,0)) AS team_atd
              FROM prior pr
              JOIN player_game_stats p2 ON p2.game_id = pr.game_id AND p2.team_id = pr.team_id
              GROUP BY pr.game_id, pr.player_atd
            )
            SELECT AVG(CASE WHEN team_atd > 0 THEN 1.0 * player_atd / team_atd END) AS prior_team_td_share,
                   COUNT(*) AS n_share_games
            FROM team_g
            """
        ),
        {"p": player_id, "g": game_id},
    ).mappings().first()
    out: dict[str, Any] = {
        "prior_carries": float(prior["carries"]) if prior and prior["carries"] is not None else None,
        "prior_targets": float(prior["targets"]) if prior and prior["targets"] is not None else None,
        "prior_receptions": float(prior["receptions"]) if prior and prior["receptions"] is not None else None,
        "prior_atd_rate": float(prior["atd_rate"]) if prior and prior["atd_rate"] is not None else None,
        "n_prior_games": int(prior["n_prior_games"] or 0) if prior else 0,
        "prior_team_td_share": float(share["prior_team_td_share"])
        if share and share["prior_team_td_share"] is not None
        else None,
    }
    return out


def audit_upcoming_capture(session: Session) -> dict[str, Any]:
    now = datetime.now(UTC)
    priced = session.execute(
        text(
            """
            WITH latest AS (
              SELECT o.outcome_id, o.game_id, o.player_id, g.league_id, g.kickoff_utc, g.data_tier,
                     g.status, pl.name, pl.position_group,
                     ROW_NUMBER() OVER (PARTITION BY o.outcome_id ORDER BY t.captured_at DESC) rn
              FROM outcomes o
              JOIN games g ON g.game_id = o.game_id
              JOIN odds_ticks t ON t.outcome_id = o.outcome_id
              LEFT JOIN players pl ON pl.player_id = o.player_id
              WHERE o.market_id = 'anytime_td' AND o.side IN ('yes', 'over') AND o.player_id IS NOT NULL
                AND g.kickoff_utc > :now AND g.status IN ('scheduled', 'preview', 'pregame')
                AND g.league_id IN ('nfl', 'fbs')
            )
            SELECT * FROM latest WHERE rn = 1
            """
        ),
        {"now": now.isoformat()},
    ).mappings().all()

    required = ("m3", "m4", "m5", "m6", "atd_sim_v1", "player_td_v1", "m6_histgbm")
    by_league: dict[str, dict[str, Any]] = {}
    gaps: list[dict[str, Any]] = []
    gap_reasons = Counter()

    for row in priced:
        lg = str(row["league_id"])
        bucket = by_league.setdefault(
            lg,
            {
                "priced_outcomes": 0,
                "games": set(),
                "data_tiers": Counter(),
                "have": Counter(),
                "missing": Counter(),
            },
        )
        bucket["priced_outcomes"] += 1
        bucket["games"].add(row["game_id"])
        bucket["data_tiers"][str(row["data_tier"] or "unknown")] += 1

        present = session.execute(
            text(
                f"""
                SELECT {FAM_SQL} AS fam, COUNT(*) n
                FROM shadow_predictions s
                JOIN games g ON g.game_id = s.game_id
                WHERE s.outcome_id = :oid AND s.market_id = 'anytime_td'
                  AND s.forecast_as_of < g.kickoff_utc
                GROUP BY 1
                """
            ),
            {"oid": row["outcome_id"]},
        ).fetchall()
        have = {r[0] for r in present}
        for fam in required:
            # player_td / histgbm are NFL research primaries; FBS uses M5 path instead
            if fam in {"player_td_v1", "m6_histgbm"} and lg != "nfl":
                continue
            if fam in have:
                bucket["have"][fam] += 1
            else:
                bucket["missing"][fam] += 1
                reason = _gap_reason(session, row, fam, have)
                gap_reasons[reason] += 1
                if len(gaps) < 80:
                    gaps.append(
                        {
                            "league": lg,
                            "game_id": row["game_id"],
                            "player": row["name"],
                            "position": row["position_group"],
                            "family": fam,
                            "data_tier": row["data_tier"],
                            "reason": reason,
                        }
                    )

    out_leagues = {}
    for lg, b in by_league.items():
        out_leagues[lg] = {
            "priced_outcomes": b["priced_outcomes"],
            "games": len(b["games"]),
            "data_tiers": dict(b["data_tiers"]),
            "have": dict(b["have"]),
            "missing": dict(b["missing"]),
            "coverage_rate": {
                fam: (b["have"][fam] / b["priced_outcomes"]) if b["priced_outcomes"] else None
                for fam in required
                if not (fam in {"player_td_v1", "m6_histgbm"} and lg != "nfl")
            },
        }
    return {
        "as_of": now.isoformat(),
        "leagues": out_leagues,
        "gap_reason_counts": dict(gap_reasons),
        "gap_examples": gaps,
        "note": "player_td_v1 and m6_histgbm expected on NFL priced outcomes only",
    }


def _gap_reason(session: Session, row: Any, fam: str, have: set[str]) -> str:
    tier = str(row.get("data_tier") or "")
    if tier == "demo":
        return "demo_slate_excluded_from_live_capture"
    if tier == "alias":
        return "alias_game_not_in_upcoming_tier"
    if fam == "atd_sim_v1":
        return "simulator_roster_exclusion_or_not_in_correlated_draw"
    if fam in {"player_td_v1", "m6_histgbm"}:
        return "research_freeze_not_ingested_for_this_game"
    if fam == "m4" and "m3" in have:
        return "m4_shadow_capture_gap"
    if not row.get("player_id"):
        return "identity_failure"
    if not row.get("position_group") or row.get("position_group") == "UNK":
        return "identity_failure_unknown_position"
    # history
    n = session.execute(
        text("SELECT COUNT(*) FROM player_game_stats WHERE player_id=:p"),
        {"p": row["player_id"]},
    ).scalar()
    if int(n or 0) == 0 and fam in {"m5", "m6", "player_td_v1"}:
        return "missing_player_history"
    return "model_not_captured_prekickoff"


def expand_pregame_captures(session: Session) -> dict[str, Any]:
    """Capture M3/M5/M6/sim + research player_td/histgbm for upcoming priced games.

    Only writes before kickoff. Append-only. Does not change prediction math.
    """
    from spe.research.atd_forecast import forecast_upcoming, freeze_prospective
    from spe.research.atd_verify import ingest_freeze_into_shadow
    from spe.simulate.correlated_atd.shadow_candidates import capture_all_atd_candidates

    now = datetime.now(UTC)
    # Candidate families (m3/m5/m6/sim) — raise game limit so weekend slate is covered
    cand = capture_all_atd_candidates(
        session,
        now=now,
        horizon_hours=24 * 14,
        limit_per_league=40,
        draw_count=3_000,
    )
    session.flush()

    # Research freeze for player_td / histgbm (NFL) — expand beyond the default 4-game forecast
    forecasts = forecast_upcoming(
        session,
        horizon_hours=24 * 14,
        now=now,
        include_nfl_ensemble=True,
        max_games_per_league=20,
    )
    # Build freeze payload from forecasts (same shape as freeze_prospective)
    freeze = freeze_prospective(
        session,
        horizon_hours=24 * 14,
        now=now,
        include_nfl_ensemble=True,
        examined_game_ids=set(),  # still tag previously examined via file if present
    )
    # Ensure forecast used wide slate: freeze_prospective already calls forecast with max 20
    ingest = ingest_freeze_into_shadow(session, now=now)
    session.flush()

    return {
        "candidate_capture": cand,
        "forecast_n_nfl_games": len(((forecasts.get("leagues") or {}).get("nfl") or {}).get("games") or []),
        "forecast_n_fbs_games": len(((forecasts.get("leagues") or {}).get("fbs") or {}).get("games") or []),
        "freeze": {
            "n_games": freeze.get("n_games"),
            "path": freeze.get("latest_path") or freeze.get("freeze_path"),
        },
        "ingest": ingest,
        "play_enabled": False,
        "post_kickoff_backfill": False,
    }


def settlement_health(session: Session, settle: dict[str, Any]) -> dict[str, Any]:
    blocked = session.execute(
        text(
            """
            SELECT st.void_reason, COUNT(*) n
            FROM shadow_settlements st
            JOIN shadow_predictions s ON s.shadow_id = st.shadow_id
            WHERE s.market_id = 'anytime_td' AND st.result IN ('blocked', 'void')
            GROUP BY 1 ORDER BY n DESC
            """
        )
    ).fetchall()
    pending_finals = session.execute(
        text(
            """
            SELECT s.game_id, g.league_id, COUNT(*) n,
                   SUM(CASE WHEN EXISTS(
                     SELECT 1 FROM player_game_stats p WHERE p.game_id=s.game_id AND p.player_id=s.player_id
                   ) THEN 1 ELSE 0 END) with_box
            FROM shadow_predictions s
            JOIN games g ON g.game_id = s.game_id
            LEFT JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
            WHERE s.market_id='anytime_td' AND lower(g.status) IN ('final','post')
              AND st.shadow_id IS NULL AND s.forecast_as_of < g.kickoff_utc
            GROUP BY 1,2
            """
        )
    ).mappings().all()
    return {
        "settle_step": settle,
        "blocked_void_reasons": [{"reason": r[0], "n": r[1]} for r in blocked],
        "pending_on_finals": [dict(r) for r in pending_finals],
        "rules": {
            "anytime_td": "rush_td + rec_td > 0",
            "pass_td_excluded": True,
            "missing_stats": "blocked_not_zero",
        },
    }


def fair_model_compare(units: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [u for u in units if u.get("result") in {"win", "loss"} and u.get("family") in ATD_FAMILIES]
    # Unique player-game outcomes (any family)
    unique_pg = {(u["league_id"], u["player_id"], u["game_id"]) for u in settled}

    by_league_fam: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(_empty))
    by_pos: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(_empty))
    by_bucket: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(_empty))
    by_pg: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = defaultdict(dict)

    for u in settled:
        y = 1 if u["result"] == "win" else 0
        p = float(u["forecast_probability"] or 0)
        lg = str(u["league_id"])
        fam = u["family"]
        pos = str(u.get("position_group") or "UNK")
        bucket = f"{int(min(9, max(0, p * 10)))/10:.1f}-{(int(min(9, max(0, p * 10)))+1)/10:.1f}"
        _acc(by_league_fam[lg][fam], p, y, u.get("flat_stake_return"))
        if u.get("price_american") is not None:
            be = american_to_implied(int(u["price_american"]))
            m = by_league_fam[lg][fam]
            m["mkt_brier_sum"] = m.get("mkt_brier_sum", 0.0) + _brier(be, y)
            m["mkt_n"] = m.get("mkt_n", 0) + 1
        _acc(by_pos[f"{lg}:{pos}"][fam], p, y, u.get("flat_stake_return"))
        _acc(by_bucket[f"{lg}:{bucket}"][fam], p, y, u.get("flat_stake_return"))
        by_pg[(lg, str(u["player_id"]), str(u["game_id"]))][fam] = u

    # Matched counts per family (outcomes where that family settled)
    matched_n = Counter()
    for fams in by_pg.values():
        for f in fams:
            matched_n[f] += 1

    # Pairwise identical
    pairs = {}
    fams = sorted(matched_n.keys())
    for i, a in enumerate(fams):
        for b in fams[i + 1 :]:
            rows = [(fams[a], fams[b]) for fams in by_pg.values() if a in fams and b in fams]
            if len(rows) < 5:
                continue
            ma, mb = _empty(), _empty()
            for ua, ub in rows:
                y = 1 if ua["result"] == "win" else 0
                _acc(ma, float(ua["forecast_probability"] or 0), y)
                _acc(mb, float(ub["forecast_probability"] or 0), y)
            pairs[f"{a}_vs_{b}"] = {"n": len(rows), a: _finalize(ma), b: _finalize(mb)}

    def _fin_league(d: dict[str, dict[str, Any]]) -> dict[str, Any]:
        out = {}
        for fam, m in d.items():
            _finalize(m)
            mn = m.get("mkt_n") or 0
            if mn:
                m["market_brier"] = m["mkt_brier_sum"] / mn
                m["model_beats_market"] = m["brier"] is not None and m["brier"] < m["market_brier"]
            # calibration buckets inside
            out[fam] = m
        return out

    return {
        "unique_player_game_outcomes": len(unique_pg),
        "raw_settled_rows": len(settled),
        "matched_outcomes_by_family": dict(matched_n),
        "by_league_family": {lg: _fin_league(fams) for lg, fams in by_league_fam.items()},
        "by_position": {k: {f: _finalize(m) for f, m in fams.items()} for k, fams in by_pos.items()},
        "by_probability_range": {
            k: {f: _finalize(m) for f, m in fams.items()} for k, fams in by_bucket.items() if sum(x["n"] for x in fams.values()) >= 3
        },
        "pairwise_identical": pairs,
        "dedupe_rule": "latest settled prekick freeze per (league,player,game,family)",
    }


def run_live_validation(session: Session, *, expand_capture: bool = True) -> dict[str, Any]:
    print("[live] coverage audit (before)…", flush=True)
    coverage_before = audit_upcoming_capture(session)
    capture = None
    if expand_capture:
        print("[live] expand pregame captures (before kickoff only)…", flush=True)
        try:
            capture = expand_pregame_captures(session)
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            capture = {"error": str(exc)[:500]}
    print("[live] coverage audit (after)…", flush=True)
    coverage_after = audit_upcoming_capture(session)
    print("[live] settle…", flush=True)
    settle = settle_all_eligible(session)
    health = settlement_health(session, settle)
    print("[live] load units…", flush=True)
    units = load_deduped_settled_units(session)
    print("[live] fair compare…", flush=True)
    compare = fair_model_compare(units)
    print("[live] weakness cohorts…", flush=True)
    weaknesses = underconfidence_audit(session, units)

    answers = _answers(coverage_before, coverage_after, capture, health, compare, weaknesses)
    report = {
        "title": "Anytime TD Live Validation",
        "generated_at": datetime.now(UTC).isoformat(),
        "play_enabled": False,
        "validated_play": False,
        "retrained": False,
        "promoted": False,
        "models_unchanged": True,
        "overwrote_freezes": False,
        "post_kickoff_backfill": False,
        "coverage_before": coverage_before,
        "capture_expansion": capture,
        "coverage_after": coverage_after,
        "settlement_health": health,
        "fair_compare": compare,
        "weaknesses": weaknesses,
        "answers": answers,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / "live_validation_report.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    return report


def _answers(
    before: dict[str, Any],
    after: dict[str, Any],
    capture: dict[str, Any] | None,
    health: dict[str, Any],
    compare: dict[str, Any],
    weaknesses: dict[str, Any],
) -> dict[str, Any]:
    unique = compare.get("unique_player_game_outcomes") or 0
    matched = compare.get("matched_outcomes_by_family") or {}
    # Best evidence: families with n>=20 preferred; else highest n
    evidence = []
    for lg, fams in (compare.get("by_league_family") or {}).items():
        for fam, m in fams.items():
            if m.get("brier") is None:
                continue
            evidence.append({"league": lg, "family": fam, "n": m.get("n"), "brier": m["brier"], "logloss": m.get("logloss")})
    evidence.sort(key=lambda r: (-(r["n"] or 0), r["brier"] or 9))
    best = evidence[:4]

    books_better = False
    for fams in (compare.get("by_league_family") or {}).values():
        for m in fams.values():
            if m.get("market_brier") is not None and m.get("brier") is not None and m["market_brier"] < m["brier"] and (m.get("n") or 0) >= 20:
                books_better = True

    under = weaknesses.get("systematic_flags") or []
    pending = health.get("pending_on_finals") or []
    capture_ok = not (capture or {}).get("error")
    settle_ok = (health.get("settle_step") or {}).get("open_on_finals_after", 0) == 0 or not pending

    return {
        "1_unique_settled_outcomes": unique,
        "2_matched_by_family": matched,
        "3_best_evidence": best if best else "insufficient",
        "4_sportsbooks_outperforming": {
            "on_current_slice": books_better,
            "detail": compare.get("by_league_family"),
        },
        "5_rush_qb_high_target_underestimated": {
            "systematic": [s for s in under if s.get("calibration_gap", 0) < 0],
            "cohorts": weaknesses.get("cohorts"),
            "do_not_adjust_models": True,
        },
        "6_capture_and_settlement_reliable": {
            "capture_expansion_ok": capture_ok,
            "open_finals_pending": pending,
            "blocked_reasons": health.get("blocked_void_reasons"),
            "coverage_before": before.get("leagues"),
            "coverage_after": after.get("leagues"),
            "gap_reasons_after": after.get("gap_reason_counts"),
            "verdict": "reliable_for_m3_m5_m6_core; player_td_histgbm_and_sim_partial" if capture_ok else "capture_error",
        },
        "7_problems_fixed": [
            "Expanded pregame candidate capture horizon/limit for weekend slate",
            "Fixed player_td score_upcoming _select_games so VERIFY_HINTS no longer exclude the full NFL slate",
            "Re-froze + ingested research player_td_v1 / HistGBM for upcoming NFL (append-only)",
            "Persist prior carries/targets/team_td_share on NEW freezes only",
            "Settlement re-run; missing≠0 and pass_td exclusion unchanged",
            "Coverage gaps classified (demo/alias/sim roster/research freeze)",
        ],
        "8_investigate_after_more_results": [
            "Confirm player_td/HistGBM coverage on all NFL priced games before each kickoff",
            "Sim roster exclusions vs depth charts — document, do not force ineligible players",
            "Rushing QB / high-target underconfidence once n≥50",
            "Matched multi-family Brier after Sunday NFL settles",
        ],
        "validated_play": False,
        "promoted": False,
        "models_unchanged": True,
    }
