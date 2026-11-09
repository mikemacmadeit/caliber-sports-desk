"""Freeze M3/M5/M6/atd_sim_v1 Anytime TD candidates before kickoff.

Append-only. Never rewrite after kickoff. Research-only / no_bet.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from spe.db.base import as_utc, new_id
from spe.db.models import Game, ShadowPrediction
from spe.market.odds import american_to_implied, ev_per_dollar, no_vig_two_way
from spe.models.rolling_td import load_m5, predict_anytime_td_m5
from spe.shadow.settle import settle_shadow_predictions
from spe.simulate.correlated_atd.examples import simulate_game_example
from spe.simulate.correlated_atd.player_weights import load_m6, _m6_predict_simple
from spe.simulate.correlated_atd.scoring import load_model_artifact, shrink_rate
from spe.simulate.correlated_atd.team_adapter import TeamTdV1Provider, upcoming_games


CANDIDATE_FAMILIES = ("m3", "m5", "m6", "atd_sim_v1")


def capture_and_settle_candidates(
    session: Session,
    *,
    now: datetime | None = None,
    horizon_hours: int = 24 * 10,
    limit_per_league: int | None = 8,
    draw_count: int = 4_000,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    capture = capture_all_atd_candidates(
        session,
        now=now,
        horizon_hours=horizon_hours,
        limit_per_league=limit_per_league,
        draw_count=draw_count,
    )
    settle = settle_shadow_predictions(session, now=now)
    session.flush()
    verification = verify_open_candidate_rows(session)
    return {
        "capture": capture,
        "settle": settle,
        "verification": verification,
        "play_enabled": False,
        "research_only": True,
        "validated_play": False,
        "rewrite_after_kickoff": False,
    }


def capture_all_atd_candidates(
    session: Session,
    *,
    now: datetime | None = None,
    horizon_hours: int = 24 * 10,
    limit_per_league: int | None = 8,
    draw_count: int = 4_000,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    summary: dict[str, Any] = {
        "written": 0,
        "unchanged": 0,
        "skipped": 0,
        "by_family": {f: 0 for f in CANDIDATE_FAMILIES},
        "games": 0,
        "play_enabled": False,
        "research_only": True,
        "recommendation_status": "no_bet",
    }
    priced = _priced_anytime_rows(session)
    if not priced:
        summary["status"] = "no_priced_anytime_td"
        return summary

    existing = {
        (r.outcome_id, r.model_version_id, r.odds_tick_id)
        for r in session.query(ShadowPrediction)
        .filter(ShadowPrediction.market_id == "anytime_td")
        .filter(
            or_(
                ShadowPrediction.model_version_id.like("m3-%anytime_td"),
                ShadowPrediction.model_version_id.like("m5-%"),
                ShadowPrediction.model_version_id.like("m6-%"),
                ShadowPrediction.model_version_id.like("atd_sim_v1-%"),
            )
        )
        .all()
    }

    # Also treat existing m3-{lg}-anytime_td rows as present.
    existing |= {
        (r.outcome_id, r.model_version_id, r.odds_tick_id)
        for r in session.query(ShadowPrediction)
        .filter(ShadowPrediction.market_id == "anytime_td")
        .filter(ShadowPrediction.model_version_id.like("m3-%"))
        .all()
    }

    provider = TeamTdV1Provider(session)
    games = upcoming_games(session, horizon_hours=horizon_hours, limit_per_league=limit_per_league)
    priced_gids = {r["game_id"] for r in priced.values()}
    have = {g.game_id for g in games}
    missing = priced_gids - have
    if missing:
        games.extend(session.query(Game).filter(Game.game_id.in_(list(missing))).all())

    by_game: dict[str, list[dict[str, Any]]] = {}
    for row in priced.values():
        by_game.setdefault(row["game_id"], []).append(row)

    arts = {
        lg: {
            "m3": load_model_artifact(lg, "m3"),
            "m5": load_m5(lg),
            "m6": load_m6(lg),
        }
        for lg in ("nfl", "fbs")
    }

    for game in games:
        rows = by_game.get(game.game_id) or []
        if not rows:
            continue
        kickoff = as_utc(game.kickoff_utc)
        if kickoff is not None and kickoff <= now:
            summary["skipped"] += len(rows)
            continue
        summary["games"] += 1
        example = simulate_game_example(session, game, provider, draw_count=draw_count, seed=42)
        sim_by_pid = {p["player_id"]: p for p in example.get("players") or []}
        injury_status = _injury_coverage(session, game.game_id)

        for row in rows:
            pid = row["player_id"]
            pos = row.get("position_group") or "WR"
            tid = _player_team(session, game, pid) or ""
            tick_id = row["tick_id"]
            league_arts = arts.get(game.league_id) or {}

            candidates: list[tuple[str, str, float, dict[str, Any]]] = []

            # M3
            p3 = shrink_rate(league_arts.get("m3"), pid, tid, pos)
            if p3 is not None:
                candidates.append(
                    (
                        "m3",
                        f"m3-{game.league_id}-anytime_td",
                        float(p3),
                        {"source": "artifact_shrink_rate"},
                    )
                )

            # M5
            m5 = league_arts.get("m5")
            if m5:
                pred = predict_anytime_td_m5(m5, player_id=pid, team_id=tid, position_group=pos)
                candidates.append(
                    (
                        "m5",
                        f"m5-{game.league_id}-anytime_td",
                        float(pred["p"]),
                        {"mean": pred.get("mean"), "source": "predict_anytime_td_m5"},
                    )
                )

            # M6
            m6 = league_arts.get("m6")
            p6 = mean6 = None
            if m6:
                p6, mean6 = _m6_predict_simple(m6, pid, pos)
            if p6 is None and pid in sim_by_pid:
                p6 = sim_by_pid[pid].get("m6_p")
            if p6 is not None:
                candidates.append(
                    (
                        "m6",
                        f"m6-{game.league_id}-anytime_td",
                        float(p6),
                        {"mean": mean6, "source": "m6_artifact"},
                    )
                )

            # Simulator — do not force ineligible / non-rostered players into draws
            sim_row = sim_by_pid.get(pid)
            if sim_row:
                candidates.append(
                    (
                        "atd_sim_v1",
                        f"atd_sim_v1-{game.league_id}-anytime_td",
                        float(sim_row["p_anytime_td"]),
                        {
                            "mean_tds": sim_row.get("mean_tds"),
                            "p_rush_td": sim_row.get("p_rush_td"),
                            "p_rec_td": sim_row.get("p_rec_td"),
                            "m6_p": sim_row.get("m6_p"),
                            "source": "simulate_correlated_atd",
                            "sim_seed": 42,
                            "draw_count": draw_count,
                        },
                    )
                )
            else:
                summary.setdefault("skip_reasons", {})
                summary["skip_reasons"]["simulator_roster_exclusion"] = (
                    int(summary["skip_reasons"].get("simulator_roster_exclusion", 0)) + 1
                )

            if not candidates:
                summary["skipped"] += 1
                summary.setdefault("skip_reasons", {})
                summary["skip_reasons"]["no_model_probability"] = (
                    int(summary["skip_reasons"].get("no_model_probability", 0)) + 1
                )
                continue

            for family, version, p, extra in candidates:
                key = (row["outcome_id"], version, tick_id)
                if key in existing:
                    summary["unchanged"] += 1
                    continue
                _append(
                    session,
                    row=row,
                    model_version_id=version,
                    p=p,
                    family=family,
                    now=now,
                    team_id=tid,
                    injury=injury_status.get(pid),
                    components_extra=extra,
                    game=game,
                )
                summary["written"] += 1
                summary["by_family"][family] = int(summary["by_family"].get(family, 0)) + 1
                existing.add(key)

    summary["status"] = "ok"
    return summary


def verify_open_candidate_rows(session: Session) -> dict[str, Any]:
    """Spot-check that open candidate rows have required freeze fields."""
    rows = session.execute(
        text(
            """
            SELECT s.shadow_id, s.player_id, s.game_id, s.league_id, s.model_version_id,
                   s.forecast_as_of, s.price_american, s.bookmaker_id, s.odds_tick_id,
                   s.recommendation_status, s.research_only, s.play_enabled, s.components,
                   g.kickoff_utc, g.home_team_id, g.away_team_id, g.status,
                   pl.name
            FROM shadow_predictions s
            JOIN games g ON g.game_id = s.game_id
            LEFT JOIN players pl ON pl.player_id = s.player_id
            LEFT JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
            WHERE s.market_id = 'anytime_td'
              AND st.shadow_id IS NULL
              AND (
                s.model_version_id LIKE 'm3-%anytime_td'
                OR s.model_version_id LIKE 'm5-%'
                OR s.model_version_id LIKE 'm6-%'
                OR s.model_version_id LIKE 'atd_sim_v1-%'
              )
            ORDER BY s.forecast_as_of DESC
            LIMIT 200
            """
        )
    ).mappings().all()

    checks = {
        "n_checked": 0,
        "ok": 0,
        "issues": [],
        "required_fields": [
            "player_id",
            "game_id",
            "model_version_id",
            "forecast_as_of_before_kickoff",
            "price_american",
            "odds_tick_id",
            "research_only",
            "play_disabled",
            "recommendation_no_bet",
        ],
    }
    for r in rows:
        checks["n_checked"] += 1
        fas = as_utc(r["forecast_as_of"])
        kick = as_utc(r["kickoff_utc"])
        problems = []
        if not r["player_id"]:
            problems.append("missing_player")
        if not r["game_id"]:
            problems.append("missing_game")
        if kick and fas and not (fas < kick):
            problems.append("forecast_not_before_kickoff")
        if r["price_american"] is None:
            problems.append("missing_price")
        if not r["odds_tick_id"]:
            problems.append("missing_odds_tick")
        if not r["research_only"]:
            problems.append("research_only_false")
        if r["play_enabled"]:
            problems.append("play_enabled_true")
        if r["recommendation_status"] != "no_bet":
            problems.append("recommendation_not_no_bet")
        comps = r["components"] if isinstance(r["components"], dict) else {}
        warnings = []
        if "injury_coverage" not in comps and "injury" not in comps:
            # Legacy Phase-5 rows may omit the field; missing ≠ assumed healthy.
            warnings.append("injury_coverage_missing_in_components_legacy")
        if problems:
            if len(checks["issues"]) < 25:
                checks["issues"].append({"shadow_id": r["shadow_id"], "problems": problems, "warnings": warnings})
        else:
            checks["ok"] += 1
            if warnings and len(checks.setdefault("warnings", [])) < 25:
                checks["warnings"].append({"shadow_id": r["shadow_id"], "warnings": warnings})
    checks["pass_rate"] = float(checks["ok"] / checks["n_checked"]) if checks["n_checked"] else None
    return checks


def _append(
    session: Session,
    *,
    row: dict[str, Any],
    model_version_id: str,
    p: float,
    family: str,
    now: datetime,
    team_id: str,
    injury: dict[str, Any] | None,
    components_extra: dict[str, Any],
    game: Game,
) -> None:
    price = int(row["price_american"])
    implied_raw = american_to_implied(price)
    opposing = _opposing_price(session, row["outcome_id"], row["bookmaker_id"], row["side"])
    novig = None
    if opposing is not None:
        try:
            a, _b = no_vig_two_way(implied_raw, american_to_implied(opposing))
            novig = float(a)
        except ValueError:
            novig = None
    ev = ev_per_dollar(p, price)
    reasons = ["RESEARCH_ONLY", "PLAY_DISABLED", "SHADOW_TRACK", "PROOF_ACCURACY", "VALIDATED_PLAY_DISABLED"]
    if ev <= 0:
        reasons.append("NEGATIVE_EXPECTED_VALUE")
    if novig is None:
        reasons.append("YES_ONLY_NOVIG_UNIDENTIFIED")
    injury_cov = injury or {"status": "unknown", "note": "missing_report_not_assumed_healthy"}
    # Persist verified pregame opportunity / TD-share priors on NEW freezes only.
    try:
        from spe.shadow.live_validation import pregame_opportunity_snapshot

        opp_snap = pregame_opportunity_snapshot(session, str(row["player_id"]), str(row["game_id"]))
    except Exception:  # noqa: BLE001 — never block capture on research metadata
        opp_snap = {}
    try:
        from spe.shadow.freeze_versioning import build_freeze_version_block

        version_block = build_freeze_version_block(
            family=family,
            league_id=str(row["league_id"]),
            forecast_as_of=now,
            source_data_cutoff=now,
            price_captured_at=_as_dt(row.get("captured_at")),
            bookmaker_id=str(row.get("bookmaker_id") or ""),
            opportunity=opp_snap,
            missing_features=list(components_extra.get("missing_features") or [])
            if isinstance(components_extra.get("missing_features"), list)
            else [],
        )
    except Exception:  # noqa: BLE001
        version_block = {}
    session.add(
        ShadowPrediction(
            shadow_id=new_id(),
            player_id=row["player_id"],
            game_id=row["game_id"],
            league_id=row["league_id"],
            market_id="anytime_td",
            outcome_id=row["outcome_id"],
            bookmaker_id=row["bookmaker_id"],
            line=None if row["line"] is None else float(row["line"]),
            side=row["side"],
            price_american=price,
            odds_tick_id=row["tick_id"],
            forecast_probability=float(p),
            market_implied_raw=float(implied_raw),
            market_implied_novig=novig,
            expected_value=float(ev),
            model_version_id=model_version_id,
            forecast_as_of=now,
            data_quality_status="research",
            abstain_reasons=reasons,
            recommendation_status="no_bet",
            components={
                "family": family,
                "position_group": row.get("position_group"),
                "player_name": row.get("name"),
                "team_id": team_id,
                "home_team_id": game.home_team_id,
                "away_team_id": game.away_team_id,
                "market_implied_raw": implied_raw,
                "market_implied_novig": novig,
                "novig_identified": novig is not None,
                "play_enabled": False,
                "line_source": "sportsbook",
                "injury_coverage": injury_cov,
                "prior_carries": opp_snap.get("prior_carries"),
                "prior_targets": opp_snap.get("prior_targets"),
                "prior_receptions": opp_snap.get("prior_receptions"),
                "prior_atd_rate": opp_snap.get("prior_atd_rate"),
                "n_prior_games": opp_snap.get("n_prior_games"),
                "prior_team_td_share": opp_snap.get("prior_team_td_share"),
                "team_td_share": opp_snap.get("prior_team_td_share"),
                **version_block,
                **components_extra,
            },
            research_only=True,
            play_enabled=False,
            created_at=now,
        )
    )


def _priced_anytime_rows(session: Session) -> dict[str, dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT o.outcome_id, o.game_id, o.player_id, o.market_id, o.side, o.line,
                   g.league_id, g.status, g.kickoff_utc,
                   t.tick_id, t.bookmaker_id, t.price_american, t.captured_at,
                   pl.position_group, pl.name
            FROM outcomes o
            JOIN games g ON g.game_id = o.game_id
            JOIN odds_ticks t ON t.outcome_id = o.outcome_id
            LEFT JOIN players pl ON pl.player_id = o.player_id
            WHERE g.data_tier = 'upcoming'
              AND g.status IN ('scheduled', 'preview', 'pregame')
              AND g.league_id IN ('nfl', 'fbs')
              AND o.market_id = 'anytime_td'
              AND o.side IN ('yes', 'over')
              AND o.player_id IS NOT NULL
            """
        )
    ).mappings().all()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        oid = row["outcome_id"]
        prev = latest.get(oid)
        if prev is None or _as_dt(row["captured_at"]) >= _as_dt(prev["captured_at"]):
            latest[oid] = dict(row)
    return latest


def _injury_coverage(session: Session, game_id: str) -> dict[str, dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT i.player_id, i.designation, i.reported_at
            FROM injury_events i
            JOIN games g ON g.game_id = i.game_id
            WHERE i.game_id = :gid AND i.reported_at < g.kickoff_utc
            ORDER BY i.reported_at DESC
            """
        ),
        {"gid": game_id},
    ).mappings().all()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        pid = str(r["player_id"])
        if pid in out:
            continue
        out[pid] = {
            "status": str(r["designation"] or "unknown"),
            "reported_at": str(r["reported_at"]),
            "complete_for_player": True,
        }
    return out


def _player_team(session: Session, game: Game, player_id: str) -> str | None:
    # Prefer roster week / open roster via eligible list would be heavy; use latest player_game or team from outcomes join.
    row = session.execute(
        text(
            """
            SELECT team_id FROM player_game_stats
            WHERE player_id = :pid AND (team_id = :h OR team_id = :a)
            ORDER BY id DESC LIMIT 1
            """
        ),
        {"pid": player_id, "h": game.home_team_id, "a": game.away_team_id},
    ).first()
    if row and row[0]:
        return str(row[0])
    return None


def _opposing_price(session: Session, outcome_id: str, bookmaker_id: str, side: str) -> int | None:
    want = {"over": "under", "under": "over", "yes": "no", "no": "yes"}.get(side)
    if not want:
        return None
    row = session.execute(
        text(
            """
            SELECT t.price_american
            FROM outcomes o
            JOIN odds_ticks t ON t.outcome_id = o.outcome_id
            WHERE o.game_id = (SELECT game_id FROM outcomes WHERE outcome_id = :oid)
              AND o.player_id = (SELECT player_id FROM outcomes WHERE outcome_id = :oid)
              AND o.market_id = (SELECT market_id FROM outcomes WHERE outcome_id = :oid)
              AND o.side = :side
              AND t.bookmaker_id = :book
            ORDER BY t.captured_at DESC
            LIMIT 1
            """
        ),
        {"oid": outcome_id, "side": want, "book": bookmaker_id},
    ).first()
    return int(row[0]) if row else None


def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return as_utc(value) or value
    if isinstance(value, str):
        return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00"))) or datetime.now(UTC)
    return datetime.now(UTC)
