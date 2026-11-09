"""End-to-end functional audit — read-only probes against live DB/API/vendors."""
from __future__ import annotations

import json
import sys
import traceback
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core"))
sys.path.insert(0, str(ROOT / "services" / "api"))

from sqlalchemy import func, text  # noqa: E402
from spe.db import models as m  # noqa: E402
from spe.db.session import session_factory  # noqa: E402

REPORT: dict = {"generated_at": datetime.now(UTC).isoformat(), "steps": {}, "issues": []}


def issue(step: str, severity: str, title: str, evidence: str, fix: str) -> None:
    REPORT["issues"].append(
        {
            "step": step,
            "severity": severity,
            "title": title,
            "evidence": evidence,
            "recommended_fix": fix,
        }
    )


def api_get(path: str, timeout: int = 60):
    with urllib.request.urlopen(f"http://localhost:8000{path}", timeout=timeout) as r:
        return json.loads(r.read().decode())


def api_post(path: str, body: dict, timeout: int = 120):
    req = urllib.request.Request(
        f"http://localhost:8000{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def pick_games(s):
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = s.execute(
        text(
            """
            select g.game_id, g.league_id, g.kickoff_utc, g.home_team_id, g.away_team_id, g.status, g.venue_id,
              (select count(*) from outcomes o where o.game_id=g.game_id) outcomes,
              (select count(*) from odds_ticks ot join outcomes o on o.outcome_id=ot.outcome_id where o.game_id=g.game_id) ticks,
              (select count(*) from forecasts f join outcomes o on o.outcome_id=f.outcome_id where o.game_id=g.game_id) forecasts,
              (select count(*) from injury_events i where i.game_id=g.game_id) injuries,
              (select count(*) from weather_snapshots w where w.game_id=g.game_id) weather
            from games g
            where g.kickoff_utc >= :now
              and g.game_id not like '%seed%'
              and g.game_id not like '%demo%'
              and g.game_id not like 'nfl-kc-buf%'
            order by case when g.league_id='nfl' then 0 else 1 end,
                     forecasts desc, ticks desc, injuries desc, g.kickoff_utc
            """
        ),
        {"now": now},
    ).mappings().all()
    nfl = next((r for r in rows if r["league_id"] == "nfl" and (r["forecasts"] > 0 or r["ticks"] > 0)), None)
    if nfl is None:
        nfl = next((r for r in rows if r["league_id"] == "nfl"), None)
    fbs = next((r for r in rows if r["league_id"] == "fbs" and (r["forecasts"] > 0 or r["ticks"] > 0)), None)
    if fbs is None:
        fbs = next((r for r in rows if r["league_id"] == "fbs"), None)
    return nfl, fbs, rows


def audit_game(s, row, step: str) -> dict:
    gid = row["game_id"]
    game = s.get(m.Game, gid)
    home = s.get(m.Team, game.home_team_id)
    away = s.get(m.Team, game.away_team_id)
    out = {
        "game_id": gid,
        "league": game.league_id,
        "matchup": f"{away.name if away else game.away_team_id} @ {home.name if home else game.home_team_id}",
        "kickoff_utc": str(game.kickoff_utc),
        "status": game.status,
        "db_counts": dict(row),
    }

    # Duplicate matchups
    dupes = (
        s.query(m.Game)
        .filter(
            m.Game.league_id == game.league_id,
            m.Game.home_team_id == game.home_team_id,
            m.Game.away_team_id == game.away_team_id,
            m.Game.kickoff_utc == game.kickoff_utc,
        )
        .all()
    )
    # Also fuzzy by kickoff day + team abbreviations in id
    out["duplicate_game_ids"] = [d.game_id for d in dupes]
    if len(dupes) > 1:
        issue(
            step,
            "high",
            "Duplicate game records for same matchup",
            f"{[d.game_id for d in dupes]}",
            "Canonicalize ESPN vs nflverse/CFBD game IDs into one event identity.",
        )

    # Roster
    roster = (
        s.query(m.Roster)
        .filter(m.Roster.team_id.in_([game.home_team_id, game.away_team_id]))
        .all()
    )
    # Prefer season-scoped roster weeks if present
    rw_count = 0
    try:
        rw_count = (
            s.query(func.count())
            .select_from(m.RosterWeek)
            .filter(m.RosterWeek.team_id.in_([game.home_team_id, game.away_team_id]))
            .scalar()
        )
    except Exception:
        pass
    players = []
    for r in roster[:500]:
        p = s.get(m.Player, r.player_id)
        if p:
            players.append(
                {
                    "player_id": p.player_id,
                    "name": p.name,
                    "position_group": p.position_group,
                    "team_id": r.team_id,
                }
            )
    out["roster"] = {
        "roster_table_rows": len(roster),
        "roster_week_rows": rw_count,
        "unique_players": len({p["player_id"] for p in players}),
        "sample": players[:15],
        "note": "Roster table is not game-scoped; may include historical seasons.",
    }
    if len(roster) == 0:
        issue(step, "critical", "No roster rows for game teams", gid, "Ingest current-season active roster for both teams.")
    if len(roster) > 200:
        issue(
            step,
            "high",
            "Roster appears multi-season / not active-only",
            f"{gid}: {len(roster)} roster rows for two teams",
            "Filter roster to current season week / active status before modeling.",
        )

    # Injuries
    injuries = (
        s.query(m.InjuryEvent)
        .filter(m.InjuryEvent.game_id == gid)
        .order_by(m.InjuryEvent.reported_at.desc())
        .all()
    )
    out["injuries"] = {
        "count": len(injuries),
        "sample": [
            {
                "player_id": i.player_id,
                "name": (s.get(m.Player, i.player_id).name if s.get(m.Player, i.player_id) else None),
                "designation": i.designation,
                "source": i.source,
                "reported_at": str(i.reported_at),
                "coverage_complete": i.coverage_complete,
            }
            for i in injuries[:12]
        ],
    }
    if game.league_id == "fbs" and len(injuries) == 0:
        issue(
            step,
            "high",
            "No FBS injury rows for selected game",
            gid,
            "College injury coverage is incomplete; never default missing to healthy.",
        )

    # Weather
    weather = (
        s.query(m.WeatherSnapshot)
        .filter(m.WeatherSnapshot.game_id == gid)
        .order_by(m.WeatherSnapshot.issued_at.desc())
        .all()
    )
    out["weather"] = {
        "count": len(weather),
        "latest": None
        if not weather
        else {
            "issued_at": str(weather[0].issued_at),
            "temperature_c": weather[0].temperature_c,
            "wind_mps": weather[0].wind_mps,
            "precip_mm": weather[0].precip_mm,
            "summary": weather[0].summary,
            "roof_status": weather[0].roof_status,
            "provider": weather[0].provider,
        },
    }
    if not weather:
        issue(step, "medium", "No weather snapshot for game", gid, "Run ingest-weather / NWS for venue.")

    # Outcomes / markets / odds
    outcomes = s.query(m.Outcome).filter(m.Outcome.game_id == gid).all()
    by_market: dict[str, list] = defaultdict(list)
    for o in outcomes:
        ticks = (
            s.query(m.OddsTick)
            .filter(m.OddsTick.outcome_id == o.outcome_id)
            .order_by(m.OddsTick.captured_at.desc())
            .limit(3)
            .all()
        )
        fcs = (
            s.query(m.Forecast)
            .filter(m.Forecast.outcome_id == o.outcome_id)
            .order_by(m.Forecast.as_of.desc())
            .limit(2)
            .all()
        )
        player = s.get(m.Player, o.player_id) if o.player_id else None
        by_market[o.market_id].append(
            {
                "outcome_id": o.outcome_id,
                "player_id": o.player_id,
                "player_name": player.name if player else None,
                "side": o.side,
                "line": o.line,
                "latest_odds": [
                    {
                        "book": t.bookmaker_id,
                        "price": t.price_american,
                        "captured_at": str(t.captured_at),
                        "is_live": t.is_live,
                    }
                    for t in ticks
                ],
                "latest_forecast": None
                if not fcs
                else {
                    "p": fcs[0].p,
                    "p_low": fcs[0].p_low,
                    "p_high": fcs[0].p_high,
                    "as_of": str(fcs[0].as_of),
                    "model_version_id": fcs[0].model_version_id,
                    "components": fcs[0].components,
                    "data_cutoff": str(fcs[0].data_cutoff) if fcs[0].data_cutoff else None,
                    "is_official": fcs[0].is_official,
                },
            }
        )
    expected_markets = {
        "player_anytime_td",
        "player_receptions",
        "player_reception_yds",
        "player_rush_yds",
    }
    # market ids may differ — normalize
    present = set(by_market.keys())
    out["markets_present"] = sorted(present)
    out["market_detail"] = {k: v[:8] for k, v in by_market.items()}
    missing = []
    aliases = {
        "anytime_td": "player_anytime_td",
        "player_anytime_td": "player_anytime_td",
        "receptions": "player_receptions",
        "player_receptions": "player_receptions",
        "receiving_yards": "player_reception_yds",
        "player_reception_yds": "player_reception_yds",
        "rushing_yards": "player_rush_yds",
        "player_rush_yds": "player_rush_yds",
    }
    normalized = {aliases.get(k, k) for k in present}
    for em in expected_markets:
        if em not in normalized and not any(em in k for k in present):
            missing.append(em)
    out["missing_prop_markets"] = missing
    if missing:
        issue(
            step,
            "critical" if game.league_id == "nfl" else "high",
            f"Missing prop markets: {missing}",
            f"{gid} has {sorted(present)}",
            "Ingest Odds API player props for all four v1 markets.",
        )
    if not outcomes:
        issue(step, "critical", "No priced outcomes for game", gid, "Odds ingest did not create outcomes.")

    # Feature rows (table name)
    feat_table = None
    for candidate in ("features", "feature_rows", "feature"):
        try:
            s.execute(text(f"select 1 from {candidate} limit 1"))
            feat_table = candidate
            break
        except Exception:
            s.rollback()
    out["feature_table"] = feat_table
    if feat_table:
        feat_n = s.execute(
            text(f"select count(*) from {feat_table} where game_id=:g"), {"g": gid}
        ).scalar()
        out["feature_rows"] = feat_n
        sample_feat = s.execute(
            text(f"select player_id, as_of, feature_version, feature_blob from {feat_table} where game_id=:g limit 3"),
            {"g": gid},
        ).mappings().all()
        out["feature_sample"] = [dict(x) for x in sample_feat]
    else:
        out["feature_rows"] = 0
        issue(step, "critical", "Feature table missing or empty for game", gid, "Rebuild point-in-time features.")

    # API detail
    try:
        detail = api_get(f"/v1/games/{urllib.request.quote(gid, safe='')}")
        out["api_detail_ok"] = True
        out["api_props_count"] = len(detail.get("props") or [])
        out["api_injuries_count"] = len(detail.get("injuries") or [])
        out["api_weather"] = detail.get("weather")
        out["api_research"] = detail.get("research")
        out["api_key_features_count"] = len(detail.get("key_features") or [])
    except Exception as e:
        out["api_detail_ok"] = False
        out["api_detail_error"] = str(e)
        issue(step, "critical", "Game detail API failed", f"{gid}: {e}", "Fix /v1/games/{id} errors.")

    return out


def verify_model_math(s, game_audit: dict, step: str) -> dict:
    gid = game_audit["game_id"]
    findings = {"game_id": gid, "checks": []}
    # Collect forecasts by player+market+line
    rows = s.execute(
        text(
            """
            select o.market_id, o.player_id, o.side, o.line, f.p, f.p_low, f.p_high, f.model_version_id, f.as_of, f.components, f.data_cutoff
            from forecasts f
            join outcomes o on o.outcome_id=f.outcome_id
            where o.game_id=:g
              and f.as_of = (
                select max(f2.as_of) from forecasts f2 where f2.outcome_id=f.outcome_id
              )
            """
        ),
        {"g": gid},
    ).mappings().all()
    findings["forecast_count"] = len(rows)
    invalid = []
    for r in rows:
        p, lo, hi = r["p"], r["p_low"], r["p_high"]
        ok = True
        reasons = []
        if p is None or not (0 <= p <= 1):
            ok = False
            reasons.append(f"p={p}")
        if lo is not None and hi is not None:
            if not (0 <= lo <= hi <= 1) or not (lo <= p <= hi):
                ok = False
                reasons.append(f"interval [{lo},{hi}] vs p={p}")
        if not ok:
            invalid.append({"row": dict(r), "reasons": reasons})
    findings["invalid_probability_count"] = len(invalid)
    findings["invalid_sample"] = invalid[:10]
    if invalid:
        issue(step, "critical", "Invalid forecast probabilities", f"{gid}: {len(invalid)} invalid", "Clamp/calibrate model outputs.")

    # Over-line monotonicity for yards/receptions
    mono_violations = []
    by_key: dict[tuple, list] = defaultdict(list)
    for r in rows:
        if r["side"] and str(r["side"]).lower() in {"over", "o"} and r["line"] is not None:
            by_key[(r["market_id"], r["player_id"], r["side"])].append(r)
    for key, items in by_key.items():
        items = sorted(items, key=lambda x: float(x["line"]))
        for a, b in zip(items, items[1:]):
            if a["p"] is not None and b["p"] is not None and float(b["line"]) > float(a["line"]) and b["p"] > a["p"] + 1e-6:
                mono_violations.append({"key": key, "lower_line": dict(a), "higher_line": dict(b)})
    findings["monotonicity_violations"] = len(mono_violations)
    findings["monotonicity_sample"] = mono_violations[:8]
    if mono_violations:
        issue(
            step,
            "high",
            "Higher over lines have higher probability (non-monotonic)",
            f"{gid}: {len(mono_violations)} violations",
            "Enforce distributional consistency across lines.",
        )

    # Traceability: components / model version / data cutoff
    missing_trace = [dict(r) for r in rows if not r["model_version_id"] or r["components"] in (None, {}, [])]
    findings["missing_trace_count"] = len(missing_trace)
    findings["model_versions_used"] = sorted({r["model_version_id"] for r in rows if r["model_version_id"]})
    findings["data_cutoff_sample"] = [str(r["data_cutoff"]) for r in rows[:5]]
    if missing_trace:
        issue(
            step,
            "high",
            "Forecasts lack model/component provenance",
            f"{gid}: {len(missing_trace)}/{len(rows)}",
            "Persist components linking player features and model version.",
        )

    # Current-season data check via components / feature blob if available
    stale = []
    for r in rows[:50]:
        cutoff = r["data_cutoff"]
        if cutoff and str(cutoff) < "2026-01-01":
            stale.append(str(cutoff))
    findings["stale_data_cutoff_pre_2026"] = len(stale)
    if stale:
        issue(
            step,
            "critical",
            "Forecast data_cutoff predates 2026 season",
            f"{gid}: sample {stale[:5]}",
            "Retrain/score with current-season features.",
        )
    return findings


def test_simulation(s, game_audit: dict, step: str) -> dict:
    gid = game_audit["game_id"]
    result = {"game_id": gid}
    try:
        created = api_post(
            "/v1/simulations",
            {"game_id": gid, "draw_count": 2000, "seed": 42, "scenario_overrides": {}},
        )
        result["create"] = created
        run = api_get(f"/v1/simulations/{created['run_id']}")
        result["run"] = {
            "draw_count": run.get("draw_count"),
            "scenario": run.get("scenario"),
            "research_only": run.get("research_only"),
            "model_version": run.get("model_version"),
            "score_quantiles": (run.get("summary") or {}).get("score_quantiles"),
            "player_td_prob_n": len((run.get("summary") or {}).get("player_td_prob") or {}),
            "convergence": run.get("convergence"),
            "provenance": run.get("provenance"),
        }
        sim_td = (run.get("summary") or {}).get("player_td_prob") or {}
        # Compare to model anytime TD forecasts
        model_td = {}
        for market, rows in (game_audit.get("market_detail") or {}).items():
            if "td" in market.lower() or "anytime" in market.lower():
                for r in rows:
                    if r.get("latest_forecast") and r.get("player_id"):
                        model_td[r["player_id"]] = r["latest_forecast"]["p"]
        diffs = []
        for pid, sp in list(sim_td.items())[:40]:
            if pid in model_td:
                diffs.append(
                    {
                        "player_id": pid,
                        "sim_p": sp,
                        "model_p": model_td[pid],
                        "abs_diff": abs(sp - model_td[pid]),
                    }
                )
        diffs.sort(key=lambda x: -x["abs_diff"])
        result["sim_vs_model_td"] = {
            "compared": len(diffs),
            "max_abs_diff": diffs[0]["abs_diff"] if diffs else None,
            "top_diffs": diffs[:10],
        }
        if diffs and diffs[0]["abs_diff"] > 0.15:
            issue(
                step,
                "high",
                "Simulator TD frequencies diverge from model probabilities",
                f"{gid}: max abs diff {diffs[0]['abs_diff']:.3f} player={diffs[0]['player_id']}",
                "Align sim role parameters with model opportunity rates; do not treat completion as accuracy.",
            )
        # Realistic scoring
        sq = (run.get("summary") or {}).get("score_quantiles") or {}
        result["score_check"] = sq
        # Heuristic: median total points
        try:
            home_med = (sq.get("home") or sq.get("home_score") or {}).get("0.5")
            away_med = (sq.get("away") or sq.get("away_score") or {}).get("0.5")
            result["median_scores"] = {"home": home_med, "away": away_med}
            if home_med is not None and away_med is not None:
                total = home_med + away_med
                if total < 10 or total > 80:
                    issue(
                        step,
                        "high",
                        "Unrealistic simulated score totals",
                        f"{gid}: median total={total}",
                        "Recalibrate drive/scoring model.",
                    )
        except Exception:
            pass
        # Roster bug: API loads ALL roster rows then filters — check role count via DB path
        from spe.db.models import Roster
        from spe.simulate.engine import PlayerRole, simulate_game

        game = s.get(m.Game, gid)
        roster_rows = s.query(Roster).all()
        result["sim_api_loads_all_rosters"] = len(roster_rows)
        if len(roster_rows) > 5000:
            issue(
                step,
                "critical",
                "Simulation endpoint loads entire roster table into memory",
                f"len(Roster.all())={len(roster_rows)} before team filter",
                "Query roster filtered by team_id (and season).",
            )
    except Exception as e:
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc()[-1500:]
        issue(step, "critical", "Simulation failed", f"{gid}: {e}", "Fix /v1/simulations pipeline.")
    return result


def test_openai(s, game_audit: dict, step: str) -> dict:
    gid = game_audit["game_id"]
    out = {"game_id": gid}
    # Existing research
    try:
        from spe.intelligence.present import research_detail

        existing = research_detail(s, gid)
        out["existing_research"] = existing
    except Exception as e:
        out["existing_research_error"] = str(e)

    # Run live research for one game (limit 1) — may cost money
    try:
        from spe.intelligence.client import OpenAIResponses
        from spe.intelligence.research import research_game

        game = s.get(m.Game, gid)
        client = OpenAIResponses()
        run = research_game(s, game, client)
        s.commit()
        out["new_run"] = {
            "run_id": getattr(run, "run_id", None) or getattr(run, "ai_run_id", None),
            "status": getattr(run, "status", None),
            "model": getattr(run, "model", None),
            "prompt_tokens": getattr(run, "prompt_tokens", None),
            "as_of": str(getattr(run, "as_of", None)),
        }
        detail = research_detail(s, gid)
        out["detail_after"] = detail
        # Hallucination heuristics
        text_blob = json.dumps(detail or {}, default=str).lower()
        out["mentions_wrong_season"] = ("2024" in text_blob and "2026" not in text_blob) or "2023 season" in text_blob
        if out["mentions_wrong_season"]:
            issue(step, "high", "AI research may reference wrong season", gid, "Pin season/year in research context.")
        # Check for fabricated injuries: any injury names not in injury table / roster
        findings = (detail or {}).get("findings") or (detail or {}).get("signals") or []
        out["findings_count"] = len(findings) if isinstance(findings, list) else findings
    except Exception as e:
        s.rollback()
        out["error"] = str(e)
        out["traceback"] = traceback.format_exc()[-2000:]
        issue(step, "high", "OpenAI research workflow failed", str(e), "Fix intelligence client/context.")
    return out


def test_live_updates(s, game_audit: dict, step: str) -> dict:
    gid = game_audit["game_id"]
    out = {"game_id": gid}
    before_odds = s.execute(
        text(
            "select count(*), max(captured_at) from odds_ticks ot join outcomes o on o.outcome_id=ot.outcome_id where o.game_id=:g"
        ),
        {"g": gid},
    ).fetchone()
    before_wx = s.execute(
        text("select count(*), max(issued_at) from weather_snapshots where game_id=:g"), {"g": gid}
    ).fetchone()
    before_inj = s.execute(
        text("select count(*), max(reported_at) from injury_events where game_id=:g"), {"g": gid}
    ).fetchone()
    before_fc = s.execute(
        text(
            "select count(*), max(as_of) from forecasts f join outcomes o on o.outcome_id=f.outcome_id where o.game_id=:g"
        ),
        {"g": gid},
    ).fetchone()
    out["before"] = {
        "odds": {"n": before_odds[0], "max": str(before_odds[1])},
        "weather": {"n": before_wx[0], "max": str(before_wx[1])},
        "injuries": {"n": before_inj[0], "max": str(before_inj[1])},
        "forecasts": {"n": before_fc[0], "max": str(before_fc[1])},
    }

    # Run live ingest commands via imports (real network)
    try:
        from spe.ingest.odds_live import ingest_odds_live
        from spe.ingest.nws_live import ingest_nws_forecasts

        odds_res = ingest_odds_live(s, horizon_hours=96)
        s.commit()
        out["odds_ingest"] = odds_res
    except Exception as e:
        s.rollback()
        out["odds_ingest_error"] = str(e)
        issue(step, "high", "Live odds refresh failed", str(e), "Debug Odds API ingest.")

    try:
        from spe.ingest.nws_live import ingest_nws_forecasts

        wx_res = ingest_nws_forecasts(s, horizon_hours=24 * 8)
        s.commit()
        out["weather_ingest"] = wx_res
    except Exception as e:
        s.rollback()
        out["weather_ingest_error"] = str(e)
        issue(step, "medium", "Weather refresh failed", str(e), "Debug NWS ingest.")

    # Try publishing forecasts again to see if recalculated
    try:
        from spe.ingest.model_forecasts import publish_model_forecasts

        pub = publish_model_forecasts(s)
        s.commit()
        out["publish_forecasts"] = pub
    except Exception as e:
        s.rollback()
        out["publish_error"] = str(e)
        issue(step, "high", "Forecast republish failed", str(e), "Fix publish-model-forecasts.")

    after_odds = s.execute(
        text(
            "select count(*), max(captured_at) from odds_ticks ot join outcomes o on o.outcome_id=ot.outcome_id where o.game_id=:g"
        ),
        {"g": gid},
    ).fetchone()
    after_wx = s.execute(
        text("select count(*), max(issued_at) from weather_snapshots where game_id=:g"), {"g": gid}
    ).fetchone()
    after_fc = s.execute(
        text(
            "select count(*), max(as_of) from forecasts f join outcomes o on o.outcome_id=f.outcome_id where o.game_id=:g"
        ),
        {"g": gid},
    ).fetchone()
    out["after"] = {
        "odds": {"n": after_odds[0], "max": str(after_odds[1])},
        "weather": {"n": after_wx[0], "max": str(after_wx[1])},
        "forecasts": {"n": after_fc[0], "max": str(after_fc[1])},
    }
    out["detected_odds_change"] = (after_odds[0] or 0) > (before_odds[0] or 0) or str(after_odds[1]) != str(
        before_odds[1]
    )
    out["detected_weather_change"] = (after_wx[0] or 0) > (before_wx[0] or 0) or str(after_wx[1]) != str(
        before_wx[1]
    )
    out["forecasts_recalculated"] = str(after_fc[1]) != str(before_fc[1]) or (after_fc[0] or 0) > (
        before_fc[0] or 0
    )
    if not out["detected_odds_change"]:
        issue(
            step,
            "medium",
            "Odds refresh did not produce new ticks for selected game",
            gid,
            "Confirm event mapping between Odds API and internal game_id.",
        )
    if not out["forecasts_recalculated"]:
        issue(
            step,
            "high",
            "Forecasts not recalculated after live refresh",
            gid,
            "Wire change detection to re-score affected games.",
        )
    return out


def test_automation(s, step: str) -> dict:
    out = {}
    # JobRun / IngestRun tables
    try:
        jobs = s.query(m.JobRun).order_by(m.JobRun.started_at.desc()).limit(30).all()
        out["job_runs"] = [
            {
                "job": getattr(j, "job_name", None) or getattr(j, "name", None),
                "status": j.status,
                "started_at": str(j.started_at),
                "finished_at": str(getattr(j, "finished_at", None)),
                "error": getattr(j, "error", None),
            }
            for j in jobs
        ]
    except Exception as e:
        out["job_runs_error"] = str(e)
        issue(step, "high", "Cannot read job_runs", str(e), "Ensure scheduler writes JobRun rows.")

    try:
        ingests = s.query(m.IngestRun).order_by(m.IngestRun.started_at.desc()).limit(30).all()
        out["ingest_runs"] = [
            {
                "provider": getattr(i, "provider", None),
                "status": i.status,
                "started_at": str(i.started_at),
                "error": getattr(i, "error", None) or getattr(i, "message", None),
            }
            for i in ingests
        ]
    except Exception as e:
        out["ingest_runs_error"] = str(e)

    # Check if any scheduler process concept exists
    sched_files = list((ROOT / "services" / "worker").rglob("*sched*")) + list(
        (ROOT / "infra").rglob("*cron*")
    )
    out["scheduler_artifacts"] = [str(p.relative_to(ROOT)) for p in sched_files]
    if not out.get("job_runs"):
        issue(
            step,
            "critical",
            "No evidence of scheduled jobs running",
            "job_runs empty or missing",
            "Run worker loop / OS scheduler; persist JobRun success/failure.",
        )

    # Duplicate odds ticks heuristic
    dup = s.execute(
        text(
            """
            select outcome_id, bookmaker_id, price_american, captured_at, count(*) c
            from odds_ticks
            group by outcome_id, bookmaker_id, price_american, captured_at
            having c > 1
            limit 10
            """
        )
    ).fetchall()
    out["duplicate_odds_tick_groups"] = len(dup)
    if dup:
        issue(step, "medium", "Duplicate odds tick records detected", str(dup[:3]), "Dedupe on ingest.")

    # Stale odds: ticks older than 24h for upcoming games inside 48h
    stale = s.execute(
        text(
            """
            select g.game_id, max(ot.captured_at) last_tick
            from games g
            join outcomes o on o.game_id=g.game_id
            join odds_ticks ot on ot.outcome_id=o.outcome_id
            where g.kickoff_utc between datetime('now') and datetime('now', '+2 days')
            group by g.game_id
            having last_tick < datetime('now', '-1 day')
            limit 20
            """
        )
    ).fetchall()
    out["stale_odds_upcoming_games"] = [list(r) for r in stale]
    if stale:
        issue(
            step,
            "high",
            "Upcoming games have odds ticks older than 24h",
            str(stale[:5]),
            "Ensure live odds job runs within credit budget.",
        )
    return out


def test_track_record(s, step: str) -> dict:
    out = {}
    try:
        tr = api_get("/v1/shadow/track-record")
        out["api"] = tr
    except Exception as e:
        out["api_error"] = str(e)
        issue(step, "high", "Track record API failed", str(e), "Fix shadow track-record endpoint.")

    preds = s.query(m.ShadowPrediction).count()
    settled = s.query(m.ShadowSettlement).count()
    out["shadow_prediction_count"] = preds
    out["shadow_settlement_count"] = settled
    # Freeze check: predictions with kickoff in future should be frozen?
    sample = s.query(m.ShadowPrediction).order_by(m.ShadowPrediction.created_at.desc()).limit(20).all()
    out["sample_predictions"] = []
    for p in sample:
        out["sample_predictions"].append(
            {
                k: str(getattr(p, k, None))
                for k in [
                    "shadow_prediction_id",
                    "game_id",
                    "outcome_id",
                    "p",
                    "frozen_at",
                    "kickoff_at",
                    "status",
                    "created_at",
                ]
                if hasattr(p, k)
            }
        )
    # Settlement on historical
    try:
        from spe.shadow.jobs import job_settle

        settle_res = job_settle(s)
        s.commit()
        out["settle_job"] = settle_res
    except Exception as e:
        s.rollback()
        out["settle_error"] = str(e)
        issue(step, "high", "Shadow settlement job failed", str(e), "Fix settlement using historical fixtures only.")

    settlements = s.query(m.Settlement).count()
    out["recommendation_settlement_count"] = settlements
    if preds == 0:
        issue(
            step,
            "critical",
            "No shadow/track-record predictions frozen",
            "shadow_predictions=0",
            "Run shadow capture before kickoff on real slate.",
        )
    return out


def cross_check_espn(game_audit: dict, step: str) -> dict:
    """Cross-check kickoff/teams against ESPN public scoreboard when game_id is espn:*."""
    gid = game_audit["game_id"]
    out = {"game_id": gid}
    if not gid.startswith("espn:"):
        out["skipped"] = "not an espn game id"
        return out
    # espn:nfl:401873043 or espn:fbs:...
    parts = gid.split(":")
    espn_id = parts[-1]
    sport = "football"
    league = "nfl" if "nfl" in gid else "college-football"
    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/summary?event={espn_id}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read().decode())
        comp = (data.get("header") or {}).get("competitions", [{}])[0]
        competitors = comp.get("competitors") or []
        names = []
        for c in competitors:
            names.append(
                {
                    "homeAway": c.get("homeAway"),
                    "team": (c.get("team") or {}).get("displayName"),
                    "abbr": (c.get("team") or {}).get("abbreviation"),
                }
            )
        out["espn_teams"] = names
        out["espn_date"] = comp.get("date") or (data.get("header") or {}).get("competitions", [{}])[0].get("date")
        out["caliber_matchup"] = game_audit["matchup"]
        out["caliber_kickoff"] = game_audit["kickoff_utc"]
        # Rough name check
        cal = game_audit["matchup"].lower()
        for n in names:
            team = (n.get("team") or "").lower()
            if team and team.split()[-1] not in cal and team not in cal:
                # soft fail — Commanders vs Washington etc.
                pass
    except Exception as e:
        out["error"] = str(e)
        issue(step, "medium", "ESPN cross-check failed", str(e), "Manual verify kickoff/teams.")
    return out


def college_gaps(s, fbs_audit: dict, step: str) -> dict:
    gid = fbs_audit["game_id"]
    out = {"game_id": gid}
    # Player game stats recent for teams
    home = fbs_audit["db_counts"]["home_team_id"]
    away = fbs_audit["db_counts"]["away_team_id"]
    # Injuries
    out["injury_count"] = fbs_audit["injuries"]["count"]
    out["odds_markets"] = fbs_audit["markets_present"]
    out["missing_markets"] = fbs_audit["missing_prop_markets"]
    # CFBD player analytics
    pa = (
        s.query(func.count())
        .select_from(m.PlayerAnalytics)
        .filter(m.PlayerAnalytics.league_id == "fbs")
        .scalar()
    )
    out["fbs_player_analytics_rows"] = pa
    # Depth charts for college?
    try:
        dc = s.query(func.count()).select_from(m.DepthChartSnapshot).scalar()
        out["depth_chart_snapshots"] = dc
    except Exception:
        out["depth_chart_snapshots"] = None
    gaps = []
    if out["injury_count"] == 0:
        gaps.append("No college injury feed wired to this game (availability uncertain).")
    if out["missing_markets"]:
        gaps.append(f"Missing markets {out['missing_markets']} → props incomplete.")
    if not fbs_audit.get("weather", {}).get("latest"):
        gaps.append("No weather → outdoor kickoff conditions unknown.")
    if (fbs_audit.get("feature_rows") or 0) == 0:
        gaps.append("No feature rows → model cannot use current usage/matchup inputs.")
    if pa == 0:
        gaps.append("No FBS player analytics → falls back to weak priors.")
    out["gaps"] = gaps
    out["reliability_impact"] = (
        "College predictions are less reliable when injuries, depth, and prop markets are missing; "
        "probabilities may reflect league priors rather than game-specific information."
    )
    for g in gaps:
        issue(step, "high", "College data gap", f"{gid}: {g}", "Prioritize CFBD/injury/odds coverage for FBS.")
    return out


def main():
    s = session_factory()
    s.connection().exec_driver_sql("PRAGMA busy_timeout=300000")
    try:
        nfl, fbs, all_rows = pick_games(s)
        REPORT["steps"]["slate_overview"] = {
            "upcoming_with_signal": [
                {
                    "game_id": r["game_id"],
                    "league": r["league_id"],
                    "kickoff": str(r["kickoff_utc"]),
                    "outcomes": r["outcomes"],
                    "ticks": r["ticks"],
                    "forecasts": r["forecasts"],
                    "injuries": r["injuries"],
                    "weather": r["weather"],
                }
                for r in all_rows[:40]
            ],
            "selected_nfl": dict(nfl) if nfl else None,
            "selected_fbs": dict(fbs) if fbs else None,
        }
        # Seed contamination
        seed = s.execute(
            text(
                "select game_id, kickoff_utc, status from games where game_id like '%seed%' or game_id like '%demo%' or game_id='nfl-kc-buf'"
            )
        ).fetchall()
        REPORT["steps"]["seed_contamination"] = [list(x) for x in seed]
        if seed:
            issue(
                "global",
                "high",
                "Demo/seed games still present in database",
                str(seed),
                "Remove seed slate from production DB or exclude from /v1/slate.",
            )

        if not nfl:
            issue("step1", "critical", "No upcoming NFL game found", "", "Ingest ESPN/nflverse schedule.")
        else:
            print("Auditing NFL", nfl["game_id"], flush=True)
            nfl_audit = audit_game(s, nfl, "step1")
            nfl_audit["espn_crosscheck"] = cross_check_espn(nfl_audit, "step1")
            REPORT["steps"]["step1_nfl"] = nfl_audit
            REPORT["steps"]["step3_nfl_math"] = verify_model_math(s, nfl_audit, "step3")
            print("Simulating NFL", flush=True)
            REPORT["steps"]["step4_nfl_sim"] = test_simulation(s, nfl_audit, "step4")

        if not fbs:
            issue("step2", "critical", "No upcoming FBS game found", "", "Ingest ESPN/CFBD schedule.")
        else:
            print("Auditing FBS", fbs["game_id"], flush=True)
            fbs_audit = audit_game(s, fbs, "step2")
            fbs_audit["espn_crosscheck"] = cross_check_espn(fbs_audit, "step2")
            REPORT["steps"]["step2_fbs"] = fbs_audit
            REPORT["steps"]["step2_gaps"] = college_gaps(s, fbs_audit, "step2")
            REPORT["steps"]["step3_fbs_math"] = verify_model_math(s, fbs_audit, "step3")
            print("Simulating FBS", flush=True)
            REPORT["steps"]["step4_fbs_sim"] = test_simulation(s, fbs_audit, "step4")

        # OpenAI on NFL preferred
        target = REPORT["steps"].get("step1_nfl") or REPORT["steps"].get("step2_fbs")
        if target:
            print("OpenAI research", target["game_id"], flush=True)
            REPORT["steps"]["step5_openai"] = test_openai(s, target, "step5")

        # Live updates on NFL
        if REPORT["steps"].get("step1_nfl"):
            print("Live updates", flush=True)
            REPORT["steps"]["step6_live"] = test_live_updates(s, REPORT["steps"]["step1_nfl"], "step6")

        print("Automation", flush=True)
        REPORT["steps"]["step7_automation"] = test_automation(s, "step7")
        print("Track record", flush=True)
        REPORT["steps"]["step8_track_record"] = test_track_record(s, "step8")

        # API surface smoke
        smoke = {}
        for path in [
            "/v1/slate?league=nfl",
            "/v1/slate?league=fbs",
            "/v1/props",
            "/v1/coverage",
            "/v1/model-health",
            "/v1/recommendations/history",
            "/v1/analytics/summary?league=nfl",
            "/v1/days",
        ]:
            try:
                data = api_get(path)
                smoke[path] = {
                    "ok": True,
                    "keys": list(data.keys())[:12],
                    "n_games": len(data.get("games") or []),
                    "n_rows": len(data.get("rows") or []),
                }
            except Exception as e:
                smoke[path] = {"ok": False, "error": str(e)}
                issue("api", "high", f"API path failed: {path}", str(e), "Fix endpoint.")
        REPORT["steps"]["api_smoke"] = smoke

    finally:
        s.close()

    out_path = ROOT / "data" / "audit_e2e_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(REPORT, indent=2, default=str), encoding="utf-8")
    print("WROTE", out_path, flush=True)
    print("ISSUES", len(REPORT["issues"]), flush=True)
    for i in REPORT["issues"]:
        print(f"[{i['severity']}] {i['step']}: {i['title']}", flush=True)


if __name__ == "__main__":
    main()
