from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy.exc import OperationalError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from spe.adapters.registry import get_adapter, list_adapters
from spe.config import settings
from spe.db.models import (
    FeatureRow,
    Forecast,
    Game,
    InjuryEvent,
    ModelHealthSnapshot,
    ModelVersion,
    Outcome,
    Player,
    Recommendation,
    Settlement,
    SimulationRun,
    Team,
)
from spe.db.session import init_db
from spe.licensing import LICENSES
from spe.market.odds import american_to_decimal, ev_per_dollar
from spe.simulate.engine import PlayerRole, simulate_game

from app.deps import db_session, disclaimer
from app.queries import (
    eligible_roster_index,
    forecast_payload,
    games_for_slate,
    kickoff_day,
    latest_forecast,
    latest_tick,
    latest_weather,
    list_props,
    prop_row,
    ruled_out_keys,
    slate_game,
    slate_today,
    without_duplicate_props,
)
from app.schemas import ParlayEvaluate, SimulationCreate

app = FastAPI(
    title="Caliber API",
    version="0.1.0",
    description="Odds-aware calibrated probability engine for NFL and FBS. Research forecasts only.",
)

_cors_origins = []
for part in str(settings.web_origin or "").split(","):
    origin = part.strip()
    if origin and origin not in _cors_origins:
        _cors_origins.append(origin)
for origin in ("http://localhost:3000", "http://127.0.0.1:3000"):
    if origin not in _cors_origins:
        _cors_origins.append(origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    try:
        init_db()
    except OperationalError:
        # A long ingest can hold SQLite. Keep serving; the research tables appear on the next open.
        return


@app.exception_handler(OperationalError)
async def sqlite_operational_error_handler(request, exc: OperationalError):  # type: ignore[no-untyped-def]
    """Map SQLite lock waits to 503 instead of an opaque 500 after the web aborts."""
    from fastapi.responses import JSONResponse

    msg = str(getattr(exc, "orig", None) or exc).lower()
    if "locked" in msg or "busy" in msg:
        return JSONResponse(
            status_code=503,
            content={
                "detail": "database_busy",
                "message": "SQLite is busy with an ingest/write job. Retry shortly; data was not replaced with empty rows.",
                "path": str(request.url.path),
            },
        )
    return JSONResponse(status_code=500, content={"detail": "database_error", "message": str(exc)})


def _research_detail(session: Session, game_id: str):
    from spe.intelligence.present import research_detail

    return research_detail(session, game_id)


def _intelligence_usage(session: Session):
    from spe.intelligence.present import improvement_summary, usage_summary

    summary = usage_summary(session)
    summary["configured"] = bool(settings.openai_api_key)
    summary["improvement"] = improvement_summary(session)
    return summary


def _research_reviews(session: Session):
    from spe.intelligence.present import review_rows

    return review_rows(session)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "caliber-api", "env": settings.app_env}


@app.get("/v1/slate")
def slate(
    date: str | None = None,
    league: str = Query(default="all"),
    include_historical: bool = Query(default=False),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    games = games_for_slate(session, date, league, include_historical=include_historical)
    versions = {
        league: version_id
        for league in ("nfl", "fbs")
        if (version_id := f"m3-{league}-anytime_td") and session.get(ModelVersion, version_id) is not None
    }
    latest_as_of = session.query(Forecast).order_by(Forecast.as_of.desc()).first()
    freshness = latest_as_of.as_of if latest_as_of else None
    return {
        "date": date or "all",
        "league": league,
        "games": [slate_game(session, g, freshness) for g in games],
        "model_versions": versions,
        "data_status": "live_slate" if not include_historical else "includes_historical",
        "disclaimer": disclaimer(),
    }


@app.get("/v1/coverage")
def coverage(session: Session = Depends(db_session)) -> dict[str, Any]:
    from spe.ingest.coverage import coverage_report

    payload = coverage_report(session)
    payload["disclaimer"] = disclaimer()
    return payload


@app.get("/v1/analytics/summary")
def analytics_summary(
    league: str = Query(default="nfl"),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    from spe.analytics.definitions import PLAYER_METRICS, TEAM_METRICS, UNAVAILABLE
    from spe.db.models import BacktestRun, FeatureRow, PlayerAnalytics, TeamAnalytics
    from sqlalchemy import func

    top = (
        session.query(TeamAnalytics)
        .filter(TeamAnalytics.league_id == league, TeamAnalytics.grain == "season", TeamAnalytics.split == "all")
        .order_by(TeamAnalytics.season_year.desc())
        .limit(40)
        .all()
    )
    last_bt = (
        session.query(BacktestRun)
        .filter(BacktestRun.league_id == league)
        .order_by(BacktestRun.created_at.desc())
        .first()
    )
    return {
        "league_id": league,
        "team_analytics": session.query(func.count(TeamAnalytics.id)).filter(TeamAnalytics.league_id == league).scalar() or 0,
        "player_analytics": session.query(func.count(PlayerAnalytics.id)).filter(PlayerAnalytics.league_id == league).scalar() or 0,
        "pregame_features": session.query(func.count(FeatureRow.feature_id)).scalar() or 0,
        "metrics_team": list(TEAM_METRICS),
        "metrics_player": list(PLAYER_METRICS),
        "unavailable": list(UNAVAILABLE),
        "season_sample": [
            {
                "team_id": r.team_id,
                "season_year": r.season_year,
                "off_epa_per_play": (r.metrics or {}).get("off_epa_per_play"),
                "adj_off_epa": (r.metrics or {}).get("adj_off_epa"),
                "sample_size": r.sample_size,
            }
            for r in top[:12]
        ],
        "latest_backtest": None
        if not last_bt
        else {
            "run_id": last_bt.run_id,
            "market_id": last_bt.market_id,
            "n": last_bt.n,
            "brier": last_bt.brier,
            "log_loss": last_bt.log_loss,
            "ece": last_bt.ece,
            "notes": last_bt.notes,
        },
        "disclaimer": disclaimer(),
    }


@app.get("/v1/games/{game_id}")
def game_detail(game_id: str, session: Session = Depends(db_session)) -> dict[str, Any]:
    game = session.get(Game, game_id)
    if not game:
        raise HTTPException(404, "game not found")
    latest_as_of = session.query(Forecast).order_by(Forecast.as_of.desc()).first()
    roster = eligible_roster_index(session, game)
    injuries = (
        session.query(InjuryEvent)
        .filter(InjuryEvent.game_id == game_id)
        .order_by(InjuryEvent.reported_at.desc())
        .all()
    )
    outcomes = session.query(Outcome).filter(Outcome.game_id == game_id).all()
    from spe.models.prop_projection import COUNTING_MARKETS, logs_for_players

    stat_logs = logs_for_players(
        session,
        {o.player_id for o in outcomes if o.player_id and o.market_id in COUNTING_MARKETS},
    )
    unavailable = ruled_out_keys(session, [game_id])
    props = without_duplicate_props(
        [
            p
            for p in (
                prop_row(session, o, roster_index=roster, stat_logs=stat_logs, ruled_out=unavailable)
                for o in outcomes
            )
            if p
        ]
    )
    features = [
        f
        for f in session.query(FeatureRow).filter(FeatureRow.game_id == game_id).all()
        if not f.player_id or f.player_id in roster
    ]
    disagreement = []
    for o in outcomes:
        if o.player_id and o.player_id not in roster:
            continue
        fc = latest_forecast(session, o.outcome_id)
        tick = latest_tick(session, o.outcome_id)
        if fc and tick:
            from spe.market.odds import american_to_implied

            implied = american_to_implied(tick.price_american)
            if abs(fc.p - implied) >= 0.05:
                disagreement.append(
                    {
                        "outcome_id": o.outcome_id,
                        "model_p": fc.p,
                        "market_implied": implied,
                        "delta": fc.p - implied,
                    }
                )
    return {
        "game": slate_game(session, game, latest_as_of.as_of if latest_as_of else None),
        "injuries": [
            {
                "player_id": i.player_id,
                "player_name": (session.get(Player, i.player_id).name if session.get(Player, i.player_id) else i.player_id),
                "designation": i.designation,
                "practice_status": i.practice_status,
                "confidence": i.confidence,
                "source": i.source,
                "coverage_complete": i.coverage_complete,
                "reported_at": i.reported_at.isoformat(),
                "quality_status": i.quality_status,
            }
            for i in injuries
            if i.player_id in roster
        ],
        "weather": latest_weather(session, game),
        "key_features": [
            {
                "player_id": f.player_id,
                "as_of": f.as_of.isoformat(),
                "version": f.feature_version,
                "coverage": f.coverage_mask,
                "missingness": f.missingness,
                "blob": f.feature_blob,
            }
            for f in features
        ],
        "props": props,
        "model_disagreement": disagreement,
        "research": _research_detail(session, game_id),
        "disclaimer": disclaimer(),
    }


@app.get("/v1/days")
def days(
    league: str = Query(default="all"),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    games = games_for_slate(session, None, league)
    dates = sorted({kickoff_day(g.kickoff_utc) for g in games})
    return {"today": slate_today(), "timezone": "America/Chicago", "dates": dates}


@app.get("/v1/props")
def props(
    market: str | None = None,
    min_odds: int | None = None,
    min_ev: float | None = None,
    league: str | None = Query(default="all"),
    date: str | None = None,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    rows = list_props(session, market, min_odds, min_ev, league, date)
    return {"market": market or "all", "date": date or "all", "rows": rows, "disclaimer": disclaimer()}


_PROP_PICK_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_PROP_PICK_TTL_S = 600.0
_PROP_PICK_NOTE = (
    "One prop per market for each game still to come. "
    "A chance far from the price stays off. "
    "A stale price is named, and the sentence says the number is old."
)


@app.get("/v1/props/picks")
def prop_picks(
    league: str = Query(default="all"),
    date: str | None = None,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """The day's prop calls. One per market per open game."""
    from spe.props.game_picks import build_game_prop_picks

    selected = date or slate_today()
    league_id = league or "all"
    key = (league_id, selected)
    cached = _PROP_PICK_CACHE.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < _PROP_PICK_TTL_S:
        return cached[1]

    games = games_for_slate(session, selected, league_id)
    picks: list[dict[str, Any]] = []
    if games:
        rows = list_props(session, None, None, None, league_id, selected)
        picks = build_game_prop_picks(rows, {game.game_id for game in games})
    payload = {
        "date": selected,
        "league": league_id,
        "games_on_slate": len(games),
        "picks": picks,
        "note": _PROP_PICK_NOTE,
        "disclaimer": disclaimer(),
    }
    _PROP_PICK_CACHE[key] = (now, payload)
    return payload


@app.get("/v1/anytime-td/research")
def anytime_td_research(
    league: str | None = Query(default="all"),
    refresh: bool = Query(default=False),
) -> dict[str, Any]:
    """Research Anytime TD Finder payload with dual probs and reliability warnings.

    FBS primary estimate is locked C_recent (experimental research). Rebuilds the
    on-disk finder when missing, stale relative to the C_recent lock, or refresh=1.
    NFL / ATS / official forecasts unchanged. Validated Play stays off.
    """
    import json

    from spe.db.session import session_factory
    from spe.research.atd_guardrails import build_finder_payload, finder_payload_path
    from spe.research.fbs_game_level_scope import SCOPE_VERSION
    from spe.research.m5_fbs_lock_handoff import LOCKED_VERSION

    path = finder_payload_path()
    payload: dict[str, Any] | None = None
    if path.exists() and not refresh:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            payload = None
    needs_rebuild = (
        payload is None
        or payload.get("fbs_primary_model") != "m5_c_recent"
        or (payload.get("fbs_serving") or {}).get("serving", {}).get("locked_version") != LOCKED_VERSION
        or payload.get("fbs_scope_version") != SCOPE_VERSION
        or refresh
    )
    # FBS filter must not serve a pre-C_recent cache.
    if league == "fbs" and needs_rebuild:
        needs_rebuild = True
    elif league == "fbs" and payload is not None and payload.get("fbs_primary_model") != "m5_c_recent":
        needs_rebuild = True

    if needs_rebuild:
        session = session_factory()
        try:
            session.connection().exec_driver_sql("PRAGMA busy_timeout=120000")
            payload = build_finder_payload(session, refresh_fbs_c_recent=True)
            session.rollback()
        except OperationalError:
            session.rollback()
            raise
        finally:
            session.close()
        payload["payload_source"] = "database_build_c_recent"
    else:
        assert payload is not None
        payload["payload_source"] = "file"

    if league and league != "all":
        games = [g for g in (payload.get("games") or []) if g.get("league_id") == league]
        payload = {**payload, "games": games, "n_games": len(games), "league_filter": league}

    # Drop kicked-off / live / final games from the research board (file can lag scoreboard).
    from datetime import UTC, datetime

    from spe.db.base import as_utc
    from spe.db.models import Game

    session = session_factory()
    try:
        now = datetime.now(UTC)
        keep: list[dict[str, Any]] = []
        for g in payload.get("games") or []:
            gid = str(g.get("game_id") or "")
            game = session.get(Game, gid) if gid else None
            if game is None:
                keep.append(g)
                continue
            status = str(game.status or "").lower()
            if status in {"final", "canceled", "cancelled", "in_progress"}:
                continue
            kick = as_utc(game.kickoff_utc)
            if kick is not None and kick <= now:
                continue
            keep.append(g)
        payload = {**payload, "games": keep, "n_games": len(keep), "pregame_only": True}
    finally:
        session.close()

    payload["disclaimer"] = payload.get("disclaimer") or disclaimer()
    payload["play_enabled"] = False
    payload["promoted"] = False
    payload["validated_play"] = False
    payload["fbs_primary_model"] = "m5_c_recent"
    payload["fbs_primary_label"] = "player_td_v1 + C_recent blend — experimental research"
    return payload


@app.get("/v1/players/{player_id}/forecast")
def player_forecast(
    player_id: str,
    game_id: str,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    player = session.get(Player, player_id)
    if not player:
        raise HTTPException(404, "player not found")
    outcomes = (
        session.query(Outcome)
        .filter(Outcome.player_id == player_id, Outcome.game_id == game_id)
        .all()
    )
    features = (
        session.query(FeatureRow)
        .filter(FeatureRow.player_id == player_id, FeatureRow.game_id == game_id)
        .order_by(FeatureRow.as_of.desc())
        .first()
    )
    injury = (
        session.query(InjuryEvent)
        .filter(InjuryEvent.player_id == player_id)
        .order_by(InjuryEvent.reported_at.desc())
        .first()
    )
    forecasts = []
    for o in outcomes:
        fc = latest_forecast(session, o.outcome_id)
        tick = latest_tick(session, o.outcome_id)
        if fc:
            payload = forecast_payload(session, fc, tick)
            payload["markets"] = [o.market_id]
            payload["side"] = o.side
            payload["line"] = o.line
            forecasts.append(payload)
    return {
        "player_id": player_id,
        "name": player.name,
        "game_id": game_id,
        "distributions": (features.feature_blob if features else {}),
        "usage": (features.feature_blob.get("usage") if features else {}),
        "injury": {
            "status": injury.designation if injury else "report_missing",
            "confidence": injury.confidence if injury else 0.0,
            "coverage_complete": injury.coverage_complete if injury else False,
            "note": "Missing report is not healthy.",
        },
        "forecasts": forecasts,
        "disclaimer": disclaimer(),
    }


@app.post("/v1/simulations")
def create_simulation(body: SimulationCreate, session: Session = Depends(db_session)) -> dict[str, Any]:
    game = session.get(Game, body.game_id)
    if not game:
        raise HTTPException(404, "game not found")
    version = (
        session.query(ModelVersion)
        .filter(ModelVersion.league_id == game.league_id)
        .first()
    )
    if not version:
        raise HTTPException(500, "no model version for league")
    from spe.identity.roster import eligible_players_for_game

    # Kickoff roster only. Skip the heavy week-wide eligibility audit for interactive sims.
    eligible = eligible_players_for_game(
        session,
        game,
        include_inactive_for_props=False,
        apply_eligibility_audit=False,
    )
    roles: list[PlayerRole] = []
    player_names: dict[str, str] = {}
    for row in eligible:
        p = session.get(Player, row.player_id)
        if not p:
            continue
        player_names[p.player_id] = p.name
        feat = (
            session.query(FeatureRow)
            .filter(FeatureRow.player_id == p.player_id, FeatureRow.game_id == game.game_id)
            .first()
        )
        usage = (feat.feature_blob.get("usage") if feat else {}) or {}
        avail = (feat.feature_blob.get("availability") if feat else {}) or {}
        roles.append(
            PlayerRole(
                player_id=p.player_id,
                team_id=row.team_id,
                position_group=p.position_group,
                availability=float(avail.get("prob", 0.9)),
                snap_share=float(usage.get("snap_share", 0.5)),
                td_weight=float(usage.get("td_weight", 0.15)),
                rec_lambda=float(usage.get("rec_lambda", 3.0 if p.position_group in {"WR", "TE", "RB"} else 0.2)),
                rec_yards_mu=float(usage.get("rec_yards_mu", 40.0)),
                rush_yards_mu=float(usage.get("rush_yards_mu", 20.0 if p.position_group == "RB" else 4.0)),
            )
        )
    if not roles:
        raise HTTPException(400, "no eligible players on the kickoff roster for this game")
    result = simulate_game(
        game.league_id,
        game.home_team_id,
        game.away_team_id,
        roles,
        draw_count=min(body.draw_count, 50_000),
        seed=body.seed,
        overrides=body.scenario_overrides,
    )
    run = SimulationRun(
        game_id=game.game_id,
        as_of=body.as_of or datetime.now(UTC),
        seed=body.seed,
        draw_count=result.draw_count,
        assumptions={"rules": game.league_id, "engine": "drive_dirichlet_v0"},
        scenario="research" if result.research_only else "official",
        overrides=result.overrides,
        model_version_id=version.model_version_id,
        summary=_simulation_summary(result, player_names),
        convergence=result.convergence,
        research_only=result.research_only,
    )
    session.add(run)
    session.flush()
    return {
        "run_id": run.run_id,
        "status": "complete",
        "research_only": run.research_only,
        "note": "Overrides never silently change the official pick." if run.research_only else "",
        "disclaimer": disclaimer(),
    }


@app.get("/v1/simulations/{run_id}")
def get_simulation(run_id: str, session: Session = Depends(db_session)) -> dict[str, Any]:
    run = session.get(SimulationRun, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    game = session.get(Game, run.game_id)
    home = session.get(Team, game.home_team_id) if game else None
    away = session.get(Team, game.away_team_id) if game else None
    summary = _enrich_simulation_summary(session, run.summary or {})
    return {
        "run_id": run.run_id,
        "game_id": run.game_id,
        "matchup": {
            "away": away.abbreviation if away else "AWAY",
            "home": home.abbreviation if home else "HOME",
            "away_name": away.name if away else "Away",
            "home_name": home.name if home else "Home",
        },
        "as_of": run.as_of,
        "seed": run.seed,
        "draw_count": run.draw_count,
        "scenario": run.scenario,
        "overrides": run.overrides,
        "research_only": run.research_only,
        "model_version": run.model_version_id,
        "summary": summary,
        "convergence": run.convergence,
        "provenance": run.assumptions,
        "disclaimer": disclaimer(),
    }


def _simulation_summary(result: Any, player_names: dict[str, str]) -> dict[str, Any]:
    return {
        "player_td_prob": result.player_td_prob,
        "anytime_td": _anytime_td_rows(result.player_td_prob, player_names),
        "joint_td_pairs": len(result.joint_td),
        "score_quantiles": result.score_quantiles,
    }


def _anytime_td_rows(probs: dict[str, float], names: dict[str, str]) -> list[dict[str, Any]]:
    ranked = sorted(
        (
            {
                "player_id": pid,
                "player_name": names.get(pid, pid),
                "probability": float(prob),
            }
            for pid, prob in probs.items()
            if float(prob) > 0
        ),
        key=lambda row: row["probability"],
        reverse=True,
    )
    return ranked[:12]


def _enrich_simulation_summary(session: Session, summary: dict[str, Any]) -> dict[str, Any]:
    """Attach player names for older runs that only stored raw probability maps."""
    out = dict(summary)
    anytime = out.get("anytime_td")
    if isinstance(anytime, list) and anytime:
        # Keep only the readable shortlist for the desk.
        out["anytime_td"] = [
            row
            for row in anytime
            if isinstance(row, dict) and float(row.get("probability") or 0) > 0
        ][:12]
        return out
    probs = out.get("player_td_prob") or {}
    if not isinstance(probs, dict):
        return out
    names: dict[str, str] = {}
    for pid in probs:
        player = session.get(Player, pid)
        if player:
            names[pid] = player.name
    out["anytime_td"] = _anytime_td_rows({str(k): float(v) for k, v in probs.items()}, names)
    return out


_PARLAY_CARD_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_PARLAY_CARD_TTL_S = 600.0
_PARLAY_CARD_NOTE = (
    "Anytime touchdown tickets use each player's chance. "
    "Spread tickets use the model's cover chance for that side. They are research leans, not plays. "
    "A cover and a prop from the same game stay off."
)


@app.get("/v1/parlays/card")
def parlay_card(
    league: str = Query(default="all"),
    date: str | None = None,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """Tickets already built for the slate. The page only reads this."""
    from app.parlay_joint import anytime_td_joint
    from spe.parlay.card import best_same_game_pair, build_parlay_card

    selected = date or slate_today()
    league_id = league or "all"
    key = (league_id, selected)
    cached = _PARLAY_CARD_CACHE.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < _PARLAY_CARD_TTL_S:
        return cached[1]

    games = games_for_slate(session, selected, league_id)
    payload: dict[str, Any] = {
        "date": selected,
        "league": league_id,
        "built_at": datetime.now(UTC).isoformat(),
        "games_on_slate": len(games),
        "eligible_legs": 0,
        "games_with_legs": 0,
        "tickets": [],
        "note": _PARLAY_CARD_NOTE,
        "disclaimer": disclaimer(),
    }
    if games:
        rows = list_props(session, None, None, None, league_id, selected)
        pair = best_same_game_pair(rows)
        joints: dict[tuple[str, str, str], float] = {}
        if pair is not None:
            game_id, player_a, player_b = pair
            game = session.get(Game, game_id)
            joint = anytime_td_joint(session, game, player_a, player_b) if game is not None else None
            if joint is not None:
                lo, hi = sorted((player_a, player_b))
                joints[(game_id, lo, hi)] = float(joint)
        card = build_parlay_card(rows, joints, _spreads_for_slate(session, league_id, selected))
        payload["eligible_legs"] = card["eligible_legs"]
        payload["games_with_legs"] = card["games_with_legs"]
        payload["tickets"] = card["tickets"]
    _PARLAY_CARD_CACHE[key] = (now, payload)
    return payload


def _spreads_for_slate(session: Session, league_id: str, selected: str) -> list[dict[str, Any]]:
    """Today's spread rows. A board miss leaves the touchdown tickets up."""
    try:
        from spe.models.ats.board_cache import load_board
        from spe.models.ats.predict import attach_spread_pick

        rows = list(load_board(session).get("rows") or [])
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if league_id not in {"all", ""} and row.get("league_id") != league_id:
            continue
        kick = row.get("kickoff_utc")
        if not kick:
            continue
        try:
            kickoff = datetime.fromisoformat(str(kick).replace("Z", "+00:00"))
        except ValueError:
            continue
        if kickoff_day(kickoff) != selected:
            continue
        out.append(attach_spread_pick(dict(row)))
    return out


@app.post("/v1/parlays/evaluate")
def evaluate_parlay(body: ParlayEvaluate, session: Session = Depends(db_session)) -> dict[str, Any]:
    if len(body.legs) < 2:
        raise HTTPException(400, "parlay requires at least two legs")
    game = session.get(Game, body.game_id)
    if not game:
        raise HTTPException(404, "game not found")
    roster = eligible_roster_index(session, game)
    for leg in body.legs:
        if leg.player_id not in roster:
            raise HTTPException(400, f"player {leg.player_id} is not on the kickoff roster")
    naive = 1.0
    legs_out = []
    for leg in body.legs:
        outcome = (
            session.query(Outcome)
            .filter(
                Outcome.game_id == body.game_id,
                Outcome.player_id == leg.player_id,
                Outcome.market_id == leg.market_id,
            )
            .first()
        )
        fc = latest_forecast(session, outcome.outcome_id) if outcome else None
        p = fc.p if fc else None
        if p is None:
            raise HTTPException(400, f"missing forecast for {leg.player_id} {leg.market_id}")
        naive *= p
        legs_out.append({"player_id": leg.player_id, "market_id": leg.market_id, "p": p})
    joint = None
    method = "joint_unavailable"
    warning = (
        "Correlated joint probability is unavailable. "
        "Independent product is shown for reference only and must not be treated as a priced joint."
    )
    if all(l.market_id == "anytime_td" for l in body.legs) and len(body.legs) == 2:
        from app.parlay_joint import anytime_td_joint

        a, b = body.legs[0].player_id, body.legs[1].player_id
        joint = anytime_td_joint(session, game, a, b)
        if joint is not None:
            method = "same_draw_joint"
            warning = ""
    # Never silently substitute independent product as the joint probability.
    joint_available = joint is not None
    p_use = joint if joint_available else None
    book_dec = body.book_decimal
    if book_dec is None and body.book_american is not None:
        book_dec = american_to_decimal(body.book_american)
    ev = None
    if book_dec is not None and p_use is not None:
        ev = p_use * (book_dec - 1) - (1 - p_use)
    return {
        "game_id": body.game_id,
        "legs": legs_out,
        "joint_probability": p_use,
        "naive_independent": naive,
        "joint_available": joint_available,
        "method": method,
        "warning": warning,
        "book_quote_decimal": book_dec,
        "ev": ev,
        "risk": {
            "dependence": "teammate and game-script correlation is priced only when method=same_draw_joint",
            "payout_size_is_not_edge": True,
            "independent_is_not_joint": True,
        },
        "disclaimer": disclaimer(),
    }


@app.get("/v1/model-health")
def model_health(session: Session = Depends(db_session)) -> dict[str, Any]:
    from spe.models.train import is_reportable_health

    rows = [
        row
        for row in session.query(ModelHealthSnapshot).order_by(ModelHealthSnapshot.as_of.desc()).all()
        if is_reportable_health(row.notes)
    ]
    latest: dict[tuple[str, str], ModelHealthSnapshot] = {}
    for row in reversed(rows):
        latest[(row.league_id, row.market_id)] = row
    rows = list(latest.values())
    adapters = {pid: get_adapter(pid).health() for pid in list_adapters()}
    licenses = {
        k: {
            "license_name": v.license_name,
            "commercial_redisplay": v.commercial_redisplay,
            "attribution_required": v.attribution_required,
            "notes": v.notes,
        }
        for k, v in LICENSES.items()
    }
    return {
        "leagues": ["nfl", "fbs"],
        "snapshots": [
            {
                "league_id": r.league_id,
                "market_id": r.market_id,
                "as_of": r.as_of,
                "brier": r.brier,
                "log_loss": r.log_loss,
                "ece": r.ece,
                "skill_vs_novig": r.skill_vs_novig,
                "n": r.n,
                "coverage": r.coverage,
                "freshness_minutes": r.freshness_minutes,
                "clv": r.clv,
                "notes": r.notes,
            }
            for r in rows
        ],
        "adapters": adapters,
        "licenses": licenses,
        "gate": "NFL results cannot conceal weak FBS results. Both must pass independently.",
        "intelligence": _intelligence_usage(session),
        "disclaimer": disclaimer(),
    }


@app.get("/v1/recommendations/history")
def rec_history(session: Session = Depends(db_session)) -> dict[str, Any]:
    recs = session.query(Recommendation).order_by(Recommendation.created_at.desc()).all()
    out = []
    for r in recs:
        fc = session.get(Forecast, r.forecast_id)
        outcome = session.get(Outcome, fc.outcome_id) if fc else None
        player = session.get(Player, outcome.player_id) if outcome and outcome.player_id else None
        game = session.get(Game, outcome.game_id) if outcome else None
        settle = (
            session.query(Settlement).filter(Settlement.outcome_id == outcome.outcome_id).one_or_none()
            if outcome
            else None
        )
        out.append(
            {
                "recommendation_id": r.recommendation_id,
                "created_at": r.created_at,
                "status": (
                    "demo_play" if r.data_tier == "demo" and r.status == "play" else r.status
                ),
                "ev": r.ev,
                "ev_after_haircut": r.ev_after_haircut,
                "reason_codes": r.reason_codes,
                "player": player.name if player else None,
                "market": outcome.market_id if outcome else None,
                "league_id": game.league_id if game else None,
                "game_id": game.game_id if game else None,
                "settlement": settle.result if settle else "open",
                "p": fc.p if fc else None,
                "immutable": r.immutable,
                "data_tier": r.data_tier,
                "status_display": (
                    "demo_play" if r.data_tier == "demo" and r.status == "play" else r.status
                ),
            }
        )
    return {"rows": out, "reviews": _research_reviews(session), "disclaimer": disclaimer()}


@app.get("/v1/shadow/track-record")
def shadow_track_record_endpoint(session: Session = Depends(db_session)) -> dict[str, Any]:
    from spe.shadow.report import shadow_track_record

    payload = shadow_track_record(session)
    payload["disclaimer"] = disclaimer()
    return payload


@app.get("/v1/atd/desk-status")
def atd_desk_status(session: Session = Depends(db_session)) -> dict[str, Any]:
    """Compact Anytime TD desk health for the product UI. Play stays off."""
    import json
    from pathlib import Path

    from spe.shadow.atd_desk_takes import is_desk_take
    from spe.shadow.fbs_atd_production_readiness import evaluate_production_readiness
    from spe.db.models import ShadowPrediction, ShadowSettlement

    ready = evaluate_production_readiness(session)
    preds = session.query(ShadowPrediction).filter(ShadowPrediction.market_id == "anytime_td").all()
    settled_ids = {
        s.shadow_id
        for s in session.query(ShadowSettlement.shadow_id).filter(
            ShadowSettlement.shadow_id.in_([p.shadow_id for p in preds] or ["__none__"])
        )
    }
    open_takes = sum(1 for p in preds if is_desk_take(p) and p.shadow_id not in settled_ids)

    odds = None
    ops_path = Path("data") / "atd_sim_dev" / "live_weekend" / "do_now_ops_report.json"
    if ops_path.exists():
        try:
            ops = json.loads(ops_path.read_text(encoding="utf-8"))
            odds = (ops.get("steps") or {}).get("odds")
        except (OSError, json.JSONDecodeError):
            odds = None

    return {
        "mode": "official" if ready.get("play_enabled") else "research",
        "play_enabled": bool(ready.get("play_enabled")),
        "validated_play": bool(ready.get("validated_play")),
        "production_ready": bool(ready.get("production_ready")),
        "blend_settled": ready.get("blend_settled"),
        "hard_failures": ready.get("hard_failures") or [],
        "remaining": ready.get("remaining") or [],
        "open_takes": open_takes,
        "odds": odds,
        "note": ready.get("note"),
        "disclaimer": disclaimer(),
    }


@app.get("/v1/ats")
def ats_finder(
    league: str = Query("all"),
    sort: str = Query("kickoff"),
    side: str = Query("all"),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """ATS Finder board. Research predictions only; Validated Play stays disabled.

    The saved board is served immediately. League and side filters run on that
    file so a page view does not rebuild the strength frame.
    """
    from spe.models.ats.board_cache import load_board

    payload = load_board(session)
    rows = _with_recorded_results(session, list(payload.get("rows") or []))
    if league not in {"all", ""}:
        rows = [r for r in rows if r.get("league_id") == league]
    # Favorites/dogs use sportsbook handicap h (negative = that side favored).
    # Do not use raw nflverse L (positive = home favored) for this filter.
    def _home_h(r: dict) -> float | None:
        h = r.get("home_handicap")
        if h is not None:
            return float(h)
        return None

    if side == "favorites":
        rows = [
            r
            for r in rows
            if _home_h(r) is not None
            and _spread_side_is(r, _home_h(r), favorite=True)
        ]
    elif side == "underdogs":
        rows = [
            r
            for r in rows
            if _home_h(r) is not None
            and _spread_side_is(r, _home_h(r), favorite=False)
        ]
    if sort == "cover_prob":
        rows.sort(key=lambda r: float(r.get("pick_probability") or r.get("p_cover") or 0), reverse=True)
    elif sort == "model_vs_market":
        rows.sort(key=lambda r: abs(float(r.get("model_vs_market") or 0)), reverse=True)
    elif sort == "ev":
        rows.sort(key=lambda r: float(r.get("expected_value") or -999), reverse=True)
    else:
        rows.sort(key=lambda r: str(r.get("kickoff_utc") or ""))
    payload["rows"] = rows
    payload["filters"] = {"league": league, "sort": sort, "side": side}
    payload["disclaimer"] = disclaimer()
    return payload


def _with_recorded_results(session: Session, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The saved board can be hours old. A finished score still has to grade both calls."""
    from spe.models.ats.predict import apply_recorded_result

    ids = [row.get("game_id") for row in rows if row.get("game_id")]
    games = {
        game.game_id: game
        for game in session.query(Game).filter(Game.game_id.in_(ids or ["__none__"])).all()
    }
    out = []
    for row in rows:
        game = games.get(row.get("game_id"))
        if game is None:
            out.append(apply_recorded_result(dict(row), status=None, home_score=None, away_score=None))
            continue
        out.append(
            apply_recorded_result(
                dict(row),
                status=game.status,
                home_score=game.home_score,
                away_score=game.away_score,
            )
        )
    return out


def _spread_side_is(row: dict[str, Any], home_handicap: float, *, favorite: bool) -> bool:
    """True when the spread pick is the favorite, or the dog when favorite is false."""
    side = row.get("ats_side") or row.get("favored_side")
    laying = (side == "home" and home_handicap < 0) or (side == "away" and home_handicap > 0)
    return laying if favorite else not laying


@app.get("/v1/ats/track-record")
def ats_track_record_endpoint(
    league: str | None = None,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    from spe.shadow.ats import ats_track_record

    payload = ats_track_record(session, league_id=league)
    payload["disclaimer"] = disclaimer()
    return payload


@app.get("/v1/ats/{game_id}/research")
def ats_game_research(
    game_id: str,
    use_ai: bool = Query(False),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    from spe.models.ats.predict import predict_upcoming_ats
    from spe.models.ats.research import explain_ats_matchup

    board = predict_upcoming_ats(session, include_simulation=False)
    prediction = next((r for r in board.get("rows") or [] if r["game_id"] == game_id), None)
    if prediction is None:
        raise HTTPException(status_code=404, detail="No ATS research row for this game")
    payload = explain_ats_matchup(session, prediction, use_ai=use_ai)
    payload["prediction"] = prediction
    payload["disclaimer"] = disclaimer()
    return payload
