from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from spe.config import settings
from spe.db.base import as_utc
from spe.db.models import (
    Bookmaker,
    Forecast,
    Game,
    InjuryEvent,
    Market,
    ModelHealthSnapshot,
    ModelVersion,
    OddsTick,
    Outcome,
    Player,
    PlayerGameStat,
    PublishedStat,
    Roster,
    ShadowPrediction,
    ShadowSettlement,
    Recommendation,
    Settlement,
    SimulationRun,
    Team,
    Venue,
    WeatherSnapshot,
)
from spe.market.odds import ev_per_dollar
from spe.recommend.engine import RecommendInput, american_hold_factor, recommend as rec_fn


_WEATHER: dict[str, tuple[float, dict[str, float | bool | None]]] = {}


def _game_weather(session: Session, game: Game) -> dict[str, float | bool | None]:
    import time

    now = time.time()
    cached = _WEATHER.get(game.game_id)
    if cached and now - cached[0] < 900:
        return cached[1]
    snap = (
        session.query(WeatherSnapshot)
        .filter(WeatherSnapshot.game_id == game.game_id)
        .order_by(WeatherSnapshot.issued_at.desc())
        .first()
    )
    venue = session.get(Venue, game.venue_id) if game.venue_id else None
    indoors = bool(venue and venue.roof_type == "dome")
    if snap is not None and snap.roof_status in {"closed", "dome", "indoor"}:
        indoors = True
    payload: dict[str, float | bool | None] = {
        "wind_mps": snap.wind_mps if snap else None,
        "precip_probability": snap.precip_probability if snap else None,
        "indoors": indoors,
    }
    _WEATHER[game.game_id] = (now, payload)
    return payload


def _team_payload(team: Team) -> dict[str, Any]:
    return {
        "team_id": team.team_id,
        "name": team.name,
        "abbreviation": team.abbreviation,
        "conference": team.conference,
        "league_id": team.league_id,
        "mark": _team_mark(team),
    }


def _team_mark(team: Team) -> dict[str, str] | None:
    mark = next((item for item in (team.aliases or []) if isinstance(item, dict) and item.get("espn_id")), None)
    if mark is None and team.league_id != "nfl":
        return None
    espn_id = str(mark.get("espn_id")) if mark else ""
    if team.league_id == "nfl":
        logo = f"https://a.espncdn.com/i/teamlogos/nfl/500/{team.abbreviation.lower()}.png"
    elif espn_id:
        logo = f"https://a.espncdn.com/i/teamlogos/ncaa/500/{espn_id}.png"
    else:
        return None
    return {
        "location": str(mark.get("location") or "") if mark else "",
        "nick": str(mark.get("nick") or "") if mark else "",
        "primary": str(mark.get("primary") or "") if mark else "",
        "secondary": str(mark.get("secondary") or "") if mark else "",
        "logo": logo,
    }


def attach_team_marks(session: Session, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add each club's colors and logo. ATS rows otherwise only have a name."""
    ids = [
        team_id
        for row in rows
        for team_id in (row.get("home_team_id"), row.get("away_team_id"))
        if team_id
    ]
    if not ids:
        return rows
    teams = {
        team.team_id: team
        for team in session.query(Team).filter(Team.team_id.in_(list(dict.fromkeys(ids)))).all()
    }
    for row in rows:
        home = teams.get(row.get("home_team_id"))
        away = teams.get(row.get("away_team_id"))
        if home is not None:
            row["home_abbr"] = home.abbreviation
            row["home_mark"] = _team_mark(home)
        if away is not None:
            row["away_abbr"] = away.abbreviation
            row["away_mark"] = _team_mark(away)
    return rows


def desk_club_marks(game: Game, row: dict[str, Any]) -> dict[str, Any]:
    """Colors and logos for a today card. The live club wins when it has both."""
    away, home = game.away_team, game.home_team
    team_abbr = str(row.get("team") or "").upper()
    player = opponent = None
    if away is not None and (away.abbreviation or "").upper() == team_abbr:
        player, opponent = away, home
    elif home is not None and (home.abbreviation or "").upper() == team_abbr:
        player, opponent = home, away
    return {
        "away_abbr": away.abbreviation if away is not None else row.get("away_abbr"),
        "home_abbr": home.abbreviation if home is not None else row.get("home_abbr"),
        "opponent": opponent.abbreviation if opponent is not None else row.get("opponent"),
        "away_mark": _prefer_mark(_team_mark(away) if away is not None else None, row.get("away_mark")),
        "home_mark": _prefer_mark(_team_mark(home) if home is not None else None, row.get("home_mark")),
        "team_mark": _prefer_mark(_team_mark(player) if player is not None else None, row.get("team_mark")),
        "opponent_mark": _prefer_mark(_team_mark(opponent) if opponent is not None else None, row.get("opponent_mark")),
    }


def attach_game_marks(session: Session, rows: list[dict[str, Any]]) -> None:
    """Put each club's colors and logo on rows that only carried an abbreviation."""
    ids = [row.get("game_id") for row in rows if row.get("game_id")]
    if not ids:
        return
    games = {
        game.game_id: game
        for game in session.scalars(
            select(Game)
            .options(joinedload(Game.home_team), joinedload(Game.away_team))
            .where(Game.game_id.in_(list(dict.fromkeys(ids))))
        ).unique()
    }
    for row in rows:
        game = games.get(row.get("game_id"))
        if game is None:
            continue
        row.update(desk_club_marks(game, row))


def _prefer_mark(*candidates: Any) -> dict[str, Any] | None:
    marks = [mark for mark in candidates if isinstance(mark, dict) and (mark.get("logo") or mark.get("primary"))]
    for mark in marks:
        if mark.get("primary") and mark.get("logo"):
            return mark
    return marks[0] if marks else None


SLATE_TZ = ZoneInfo("America/Chicago")


def slate_today() -> str:
    return datetime.now(SLATE_TZ).date().isoformat()


def kickoff_day(dt: datetime) -> str:
    aware = as_utc(dt)
    if aware is None:
        return ""
    return aware.astimezone(SLATE_TZ).date().isoformat()


def games_for_slate(
    session: Session,
    date: str | None,
    league: str | None,
    *,
    include_historical: bool = False,
    keep_finished: bool = False,
) -> list[Game]:
    """Upcoming board, or one kickoff day's games including ones that already ended.

    A finished game is retagged historical. The open board stays clear of that
    archive. A dated slate keeps that day's finals so they stay on the page.
    """
    stmt = select(Game).options(
        joinedload(Game.home_team),
        joinedload(Game.away_team),
        joinedload(Game.venue),
    )
    if league and league not in {"all", ""}:
        stmt = stmt.where(Game.league_id == league)
    stmt = stmt.where(Game.data_tier != "demo")
    stmt = stmt.where(Game.data_tier != "alias")
    if date:
        start, end = _slate_day_bounds(date)
        stmt = stmt.where(Game.kickoff_utc >= start, Game.kickoff_utc < end)
    if include_historical or (keep_finished and date):
        stmt = stmt.where(Game.status.notin_(("canceled", "cancelled")))
    else:
        stmt = stmt.where(Game.data_tier.notin_(("historical", "season")))
        stmt = stmt.where(Game.status.notin_(("final", "canceled", "cancelled")))
    games = list(session.scalars(stmt).unique())
    games.sort(key=lambda g: g.kickoff_utc)
    games = _without_duplicate_matchups(games)
    if date:
        games = [g for g in games if kickoff_day(g.kickoff_utc) == date]
    return games


def _slate_day_bounds(date: str) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(date).replace(tzinfo=SLATE_TZ)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def _without_duplicate_matchups(games: list[Game]) -> list[Game]:
    """Keep one row per club matchup within a kickoff window. Prefer nflverse ids."""
    from datetime import timedelta

    from spe.db.base import as_utc
    from spe.identity.games import choose_canonical, matchup_key

    teams = {}
    for game in games:
        if game.home_team is not None:
            teams[game.home_team_id] = game.home_team
        if game.away_team is not None:
            teams[game.away_team_id] = game.away_team
    by_clubs: dict[tuple[str, str, str], list[Game]] = {}
    passthrough: list[Game] = []
    for game in games:
        key = matchup_key(game, teams)
        if key is None:
            passthrough.append(game)
            continue
        league, _day, home, away = key
        by_clubs.setdefault((league, home, away), []).append(game)

    kept: list[Game] = list(passthrough)
    window = timedelta(hours=20)
    for rows in by_clubs.values():
        rows = sorted(rows, key=lambda g: as_utc(g.kickoff_utc) or g.kickoff_utc)
        cluster: list[Game] = []
        anchor = None
        for game in rows:
            kick = as_utc(game.kickoff_utc) or game.kickoff_utc
            if not cluster:
                cluster = [game]
                anchor = kick
                continue
            if anchor is not None and abs(kick - anchor) <= window:
                cluster.append(game)
                if kick > anchor:
                    anchor = kick
            else:
                kept.append(choose_canonical(cluster) if len(cluster) > 1 else cluster[0])
                cluster = [game]
                anchor = kick
        if cluster:
            kept.append(choose_canonical(cluster) if len(cluster) > 1 else cluster[0])
    kept.sort(key=lambda g: g.kickoff_utc)
    return kept


def latest_weather(session: Session, game: Game) -> dict[str, Any] | None:
    row = (
        session.query(WeatherSnapshot)
        .filter(WeatherSnapshot.game_id == game.game_id)
        .order_by(WeatherSnapshot.issued_at.desc())
        .first()
    )
    if not row:
        return None
    return {
        "temperature_c": row.temperature_c,
        "wind_mps": row.wind_mps,
        "gust_mps": row.gust_mps,
        "precip_mm": row.precip_mm,
        "precip_probability": row.precip_probability,
        "humidity": row.humidity,
        "summary": row.summary,
        "alerts": row.alerts or [],
        "roof_status": row.roof_status,
        "issued_at": row.issued_at.isoformat(),
        "forecast_for": row.forecast_for.isoformat(),
        "provider": row.provider,
        "quality_status": row.quality_status,
    }


def slate_game(session: Session, game: Game, freshness: datetime | None) -> dict[str, Any]:
    minutes = None
    if freshness:
        minutes = (datetime.now(UTC) - as_utc(freshness)).total_seconds() / 60.0
    return {
        "game_id": game.game_id,
        "league_id": game.league_id,
        "week": game.week,
        "kickoff_utc": game.kickoff_utc,
        "status": game.status,
        "home_score": game.home_score,
        "away_score": game.away_score,
        "home": _team_payload(game.home_team),
        "away": _team_payload(game.away_team),
        "venue": {
            "venue_id": game.venue.venue_id,
            "name": game.venue.name,
            "roof_type": game.venue.roof_type,
            "surface": game.venue.surface,
            "lat": game.venue.lat,
            "lon": game.venue.lon,
        },
        "weather": latest_weather(session, game),
        "model_freshness_minutes": minutes,
        "implied_total": None,
        "conference": game.home_team.conference if game.league_id == "fbs" else None,
        "data_tier": game.data_tier,
        "research": _research_summary(session, game.game_id),
    }


def latest_forecast(session: Session, outcome_id: str) -> Forecast | None:
    if "board_batch" in session.info:
        return session.info["board_batch"]["forecasts"].get(outcome_id)
    rows = (
        session.query(Forecast)
        .filter(Forecast.outcome_id == outcome_id, Forecast.is_official.is_(True))
        .order_by(Forecast.as_of.desc())
        .all()
    )
    for row in rows:
        if "IDENTITY_CONFLICT" in (row.reason_codes or []):
            continue
        return row
    return None


def latest_tick(session: Session, outcome_id: str) -> OddsTick | None:
    if "board_batch" in session.info:
        from spe.market.odds import american_to_decimal
        quotes = [q for q in session.info["board_batch"]["quotes"].get(outcome_id, []) if not q.is_live and abs(q.price_american) >= 100]
        outcome = session.get(Outcome, outcome_id)
        game = session.get(Game, outcome.game_id) if outcome else None
        return max(quotes, key=lambda t: (
            bool(price_freshness(t, kickoff=game.kickoff_utc if game else None)["is_current_opportunity"]) and not t.is_live,
            american_to_decimal(t.price_american) if abs(t.price_american) >= 100 else 0,
            as_utc(t.captured_at),
        ), default=None)
    return (
        session.query(OddsTick)
        .filter(OddsTick.outcome_id == outcome_id)
        .order_by(OddsTick.captured_at.desc())
        .first()
    )


def price_freshness(
    tick: OddsTick | None,
    *,
    now: datetime | None = None,
    kickoff: datetime | None = None,
) -> dict[str, Any]:
    """Sportsbook freshness metadata for every displayed price.

    Distinguishes:
    - provider/bookmaker timestamp (source_at)
    - last successful observation (observed_at / captured_at after reobserve)
    - last price-change tick (only when a new OddsTick row was appended)

    A successful reobservation of an unchanged quote keeps the quote verified.
    Failed / skipped / budget-blocked fetches must not invent timestamps.
    """
    now = now or datetime.now(UTC)
    from spe.market.price_age import max_fresh_minutes

    max_age = max_fresh_minutes(kickoff, now) if kickoff is not None else float(settings.max_price_age_minutes)
    if tick is None:
        return {
            "status": "missing",
            "age_minutes": None,
            "observed_age_minutes": None,
            "provider_age_minutes": None,
            "captured_at": None,
            "observed_at": None,
            "source_timestamp": None,
            "max_age_minutes": max_age,
            "is_current_opportunity": False,
            "verification": "unverified",
        }
    observed = as_utc(tick.observed_at) or as_utc(tick.captured_at)
    provider_ts = as_utc(tick.source_at)
    # Opportunity freshness is driven by last successful observation, not by whether
    # the bookmaker price changed since the prior tick.
    age = None if observed is None else (now - observed).total_seconds() / 60.0
    provider_age = None if provider_ts is None else (now - provider_ts).total_seconds() / 60.0
    stale = age is None or age < 0 or age > max_age or tick.is_live
    quality = str(getattr(tick, "quality_status", None) or "ok")
    if quality in {"failed", "budget_blocked", "skipped", "unverified"}:
        stale = True
        verification = quality
    else:
        verification = "observed" if not stale else "stale"
    return {
        "status": "stale" if stale else "fresh",
        "age_minutes": age,
        "observed_age_minutes": age,
        "provider_age_minutes": provider_age,
        "captured_at": (as_utc(tick.captured_at) or observed).isoformat()
        if (as_utc(tick.captured_at) or observed)
        else None,
        "observed_at": observed.isoformat() if observed else None,
        "source_timestamp": provider_ts.isoformat() if provider_ts else (observed.isoformat() if observed else None),
        "max_age_minutes": max_age,
        "is_current_opportunity": not stale,
        "verification": verification,
    }


def forecast_payload(session: Session, forecast: Forecast, tick: OddsTick | None) -> dict[str, Any]:
    rec = session.info["board_batch"]["recommendations"].get(forecast.forecast_id) if "board_batch" in session.info else (
        session.query(Recommendation)
        .filter(Recommendation.forecast_id == forecast.forecast_id)
        .order_by(Recommendation.created_at.desc())
        .first()
    )
    outcome = session.get(Outcome, forecast.outcome_id)
    game = session.get(Game, outcome.game_id) if outcome is not None else None
    freshness = price_freshness(tick, kickoff=game.kickoff_utc if game is not None else None)
    price = tick.price_american if tick else None
    # A posted number still has an edge. A stale number is not a live call.
    opportunity = bool(freshness.get("is_current_opportunity"))
    ev = None
    haircut = None
    if price is not None:
        ev = ev_per_dollar(forecast.p, price)
        width = float(forecast.p_high) - float(forecast.p_low)
        haircut = ev - 0.5 * width * american_hold_factor(price)
    rec_status = rec.status if rec else None
    tier = forecast.data_tier or "demo"
    if rec and rec.data_tier == "demo" and rec_status == "play":
        rec_status = "demo_play"
    elif tier == "demo" and rec_status == "play":
        rec_status = "demo_play"
    reasons = list(forecast.reason_codes or [])
    if tier == "demo" and "DEMO_NOT_VALIDATED" not in reasons:
        reasons.append("DEMO_NOT_VALIDATED")
    if freshness["status"] == "stale" and "PRICE_STALE" not in reasons:
        reasons.append("PRICE_STALE")
    if freshness["status"] == "missing" and "NO_CURRENT_PRICE" not in reasons:
        reasons.append("NO_CURRENT_PRICE")
    if not opportunity and rec_status == "play":
        rec_status = "no_bet"
    # Research-tier / demo forecasts stay labeled separately from official M3 board rows.
    research_only = tier in {"research", "demo", "experimental"}
    return {
        "prediction_as_of": forecast.as_of,
        "model_version": forecast.model_version_id,
        "data_cutoff": forecast.data_cutoff,
        "markets": [forecast.outcome_id],
        "p_estimate": forecast.p,
        "confidence_interval": (forecast.p_low, forecast.p_high),
        "book_price": price,
        "source_timestamp": tick.source_at if tick else None,
        "price_freshness": freshness,
        "availability_status": forecast.availability_status,
        "reason_codes": reasons,
        "disclaimer": settings.disclaimer,
        "ev": ev,
        "ev_after_haircut": haircut,
        "recommendation_status": rec_status,
        "data_tier": tier,
        "research_only": research_only,
        "validated_play": False,
        "play_enabled": False,
    }


def eligible_roster_index(session: Session, game: Game) -> dict[str, Any]:
    """player_id → EligiblePlayer for the kickoff roster. Never the multi-season dump."""
    from spe.identity.roster import eligible_players_for_game

    return {
        row.player_id: row
        for row in eligible_players_for_game(session, game, include_inactive_for_props=True)
    }


def ruled_out_keys(session: Session, game_ids: list[str]) -> set[tuple[str, str]]:
    """Players whose latest report for that game is an explicit out."""
    from spe.ops.availability import designation_rules_player_out

    ids = [game_id for game_id in dict.fromkeys(game_ids) if game_id]
    if not ids:
        return set()
    latest: dict[tuple[str, str], InjuryEvent] = {}
    rows = session.query(InjuryEvent).filter(InjuryEvent.game_id.in_(ids)).all()
    for row in rows:
        key = (row.game_id or "", row.player_id)
        current = latest.get(key)
        if current is None or (as_utc(row.reported_at) or row.reported_at) >= (as_utc(current.reported_at) or current.reported_at):
            latest[key] = row
    return {key for key, row in latest.items() if designation_rules_player_out(row.designation)}


def _mark_ruled_out(payload: dict[str, Any]) -> None:
    payload["p_estimate"] = 0.0
    payload["confidence_interval"] = (0.0, 0.0)
    payload["availability_status"] = "out"
    reasons = list(payload.get("reason_codes") or [])
    if "RULED_OUT" not in reasons:
        reasons.append("RULED_OUT")
    payload["reason_codes"] = reasons
    payload["recommendation_status"] = "no_bet"
    payload["validated_play"] = False
    payload["play_enabled"] = False
    price = payload.get("book_price")
    if isinstance(price, int):
        payload["ev"] = ev_per_dollar(0.0, price)
        payload["ev_after_haircut"] = payload["ev"]


def prop_row(
    session: Session,
    outcome: Outcome,
    *,
    roster_index: dict[str, Any] | None = None,
    stat_logs: dict[str, list] | None = None,
    ruled_out: set[tuple[str, str]] | None = None,
    include_started: bool = False,
) -> dict[str, Any] | None:
    game = session.get(Game, outcome.game_id)
    if not game or game.data_tier in {"demo", "alias"}:
        return None
    if not include_started and game.data_tier in {"historical", "season"}:
        return None
    # Pregame desk only — live/final games are not priced opportunities here.
    if not include_started and game.status in {"final", "canceled", "cancelled", "in_progress"}:
        return None
    kick = as_utc(game.kickoff_utc)
    if not include_started and kick is not None and kick <= datetime.now(UTC):
        return None
    if include_started and game.status in {"canceled", "cancelled"}:
        return None
    player = session.get(Player, outcome.player_id) if outcome.player_id else None
    roster = roster_index if roster_index is not None else eligible_roster_index(session, game)
    # Quarantine: never display a player who is not on either kickoff roster.
    if outcome.player_id and outcome.player_id not in roster:
        return None
    eligible = roster.get(outcome.player_id) if outcome.player_id else None
    team = session.get(Team, eligible.team_id) if eligible else None
    home = session.get(Team, game.home_team_id)
    away = session.get(Team, game.away_team_id)
    opp = None
    if team and home and away:
        opp = away if team.team_id == home.team_id else home
    forecast = latest_forecast(session, outcome.outcome_id)
    tick = latest_tick(session, outcome.outcome_id)
    is_atd = outcome.market_id == "anytime_td"
    if not forecast and not is_atd:
        return None
    # Hide retired scoring-rate leftovers from live boards.
    if forecast and not is_atd and str(forecast.model_version_id or "").startswith("slate-td-rate"):
        return None
    book = session.get(Bookmaker, tick.bookmaker_id) if tick else None
    if is_atd and not forecast:
        # Identity and a real quote are enough to request a Desk prediction.
        # Existing tonight and overlay scores stay in place when a forecast is already stored.
        payload = {
            "p_estimate": None, "model_version": "awaiting_opportunity_score",
            "prediction_as_of": None, "data_cutoff": None,
            "confidence_interval": (0.0, 1.0), "uncertainty_status": "unvalidated",
            "book_price": tick.price_american if tick else None,
            "source_timestamp": tick.source_at if tick else None,
            "price_freshness": price_freshness(tick, kickoff=game.kickoff_utc),
            "availability_status": "unknown",
            "reason_codes": ["DESK_EXPERIMENTAL", "PLAY_DISABLED"],
            "research_only": True, "validated_play": False, "play_enabled": False,
            "recommendation_status": "no_bet", "data_tier": "research",
            "ev": None, "ev_after_haircut": None, "desk_requires_opportunity_score": True,
            "disclaimer": settings.disclaimer,
        }
        model_mean = None
    else:
        payload = forecast_payload(session, forecast, tick)
        model_mean = (forecast.components or {}).get("mean")
    # FBS Anytime TD: replace M3 team-prior clones with player-specific C_recent.
    if forecast and game.league_id == "fbs" and outcome.market_id == "anytime_td":
        from spe.research.fbs_atd_props_overlay import apply_fbs_atd_research_overlay

        payload = apply_fbs_atd_research_overlay(
            payload,
            game_id=game.game_id,
            player_id=outcome.player_id,
            league_id=game.league_id,
            market_id=outcome.market_id,
        )
        if payload is None:
            return None
    if forecast and game.league_id == "nfl" and outcome.market_id == "anytime_td":
        from spe.models.player_td.current_role import apply_current_role, load_current_role_tables
        from spe.models.player_td.tonight import apply_tonight_chance, tables_ready

        weather = _game_weather(session, game)
        started = kick is not None and kick <= datetime.now(UTC)
        through_week = (game.week - 1) if started and game.week else None
        if tables_ready():
            payload = apply_tonight_chance(
                payload,
                player_name=player.name if player else None,
                team_abbr=team.abbreviation if team else None,
                position=player.position_group if player else None,
                opponent=opp.abbreviation if opp else None,
                weather=weather,
                availability=payload.get("availability_status"),
                through_week=through_week,
            )
        else:
            depth, touches, defense = load_current_role_tables()
            payload = apply_current_role(
                payload,
                player_name=player.name if player else None,
                team_abbr=team.abbreviation if team else None,
                position=player.position_group if player else None,
                depth=depth,
                touches=touches,
                defense=defense,
                weather=weather,
                opponent=opp.abbreviation if opp else None,
            )
        price = payload.get("book_price")
        chance = payload.get("p_estimate")
        if price is not None and isinstance(chance, (int, float)):
            payload["ev"] = ev_per_dollar(float(chance), int(price))
            interval = payload.get("confidence_interval") or (chance, chance)
            width = float(interval[1]) - float(interval[0])
            payload["ev_after_haircut"] = payload["ev"] - 0.5 * width * american_hold_factor(int(price))
    if outcome.player_id and (game.game_id, outcome.player_id) in (ruled_out or set()):
        _mark_ruled_out(payload)
    freshness = payload.get("price_freshness") or {}
    row = {
        "outcome_id": outcome.outcome_id,
        "game_id": game.game_id,
        "league_id": game.league_id,
        "market_id": outcome.market_id,
        "player_id": outcome.player_id,
        "player_name": player.name if player else "Team",
        "position": player.position_group if player else None,
        "team": team.abbreviation if team else "?",
        "team_id": team.team_id if team else None,
        "opponent_id": opp.team_id if opp else None,
        "team_mark": _team_mark(team) if team else None,
        "opponent": (opp.abbreviation if opp else "?"),
        "away_abbr": away.abbreviation if away else "?",
        "home_abbr": home.abbreviation if home else "?",
        "away_mark": _team_mark(away) if away else None,
        "home_mark": _team_mark(home) if home else None,
        "kickoff_utc": game.kickoff_utc,
        "side": outcome.side,
        "line": outcome.line,
        "bookmaker": book.name if book else None,
        "book_price": tick.price_american if tick else None,
        "source_timestamp": tick.source_at if tick else None,
        "price_freshness": freshness,
        "offers": [
            {"bookmaker": session.get(Bookmaker, q.bookmaker_id).name,
             "book_price": q.price_american, "source_timestamp": q.source_at,
             "price_freshness": price_freshness(q, kickoff=game.kickoff_utc)}
            for q in session.info.get("board_batch", {}).get("quotes", {}).get(outcome.outcome_id, [])
            if not q.is_live and abs(q.price_american) >= 100 and session.get(Bookmaker, q.bookmaker_id)
        ],
        "forecast": payload,
        "best_available": bool(freshness.get("is_current_opportunity")),
        "data_tier": "research" if is_atd else forecast.data_tier if forecast.data_tier == "research" else game.data_tier,
        "research_signal": _research_signal(session, game.game_id, outcome.player_id, outcome.market_id),
        "model_mean": model_mean,
        "pick": None,
        "model_lean": None,
        "projection": None,
        "projection_games": None,
        "projection_unit": None,
        "projection_source": None,
    }
    from spe.models.prop_projection import COUNTING_MARKETS, apply_row_projection, logs_for_players

    logs = stat_logs
    if logs is None and outcome.market_id in COUNTING_MARKETS and outcome.player_id:
        logs = logs_for_players(session, [outcome.player_id])
    apply_row_projection(row, logs or {})
    return row


def _research_summary(session: Session, game_id: str):
    from spe.intelligence.present import research_summary

    return research_summary(session, game_id)


def _research_signal(session: Session, game_id: str, player_id: str | None, market_id: str):
    from spe.intelligence.present import signal_for, MARKET_METRIC, _opportunity, _signal
    batch = session.info.get("board_batch")
    if batch is not None:
        opportunity = batch["opportunities"].get((game_id, player_id, market_id))
        signal = batch["signals"].get((game_id, player_id, MARKET_METRIC.get(market_id)))
        return _opportunity(opportunity) if opportunity else (_signal(signal) if signal else None)
    return signal_for(session, game_id, player_id, market_id)


def _attach_prop_results(session: Session, rows: list[dict[str, Any]]) -> None:
    """Grade a finished line from the recorded stat. A missing box stays pending."""
    from spe.shadow.settle import _grade

    game_ids = {row.get("game_id") for row in rows if row.get("game_id")}
    if not game_ids:
        return
    games = {
        game.game_id: game
        for game in session.scalars(select(Game).where(Game.game_id.in_(game_ids)))
    }
    best: dict[tuple[str, str], PlayerGameStat] = {}
    for stat in session.scalars(select(PlayerGameStat).where(PlayerGameStat.game_id.in_(game_ids))):
        key = (stat.game_id, stat.player_id)
        prev = best.get(key)
        if prev is None or (stat.revision or 0) >= (prev.revision or 0):
            best[key] = stat
    published = {
        (stat.game_id, stat.player_id, stat.market_id): stat.actual
        for stat in session.scalars(select(PublishedStat).where(PublishedStat.game_id.in_(game_ids)))
    }
    for row in rows:
        game = games.get(row.get("game_id"))
        if game is None or game.status not in {"final", "post"}:
            row["result"] = "pending"
            continue
        actual = published.get((row.get("game_id"), row.get("player_id"), row.get("market_id")))
        if actual is None:
            stat = best.get((row.get("game_id"), row.get("player_id")))
            if stat is None:
                row["result"] = "pending"
                continue
            market = row.get("market_id")
            if market == "anytime_td":
                actual = float((stat.rush_td or 0) + (stat.rec_td or 0) + (stat.pass_td or 0))
            elif market == "receptions":
                actual = float(stat.receptions or 0)
            elif market == "rushing_yards":
                actual = float(stat.rush_yards or 0)
            elif market == "receiving_yards":
                actual = float(stat.rec_yards or 0)
            else:
                row["result"] = "pending"
                continue
        row["actual"] = actual
        row["result"] = _grade(str(row.get("side") or "over"), row.get("line"), float(actual), str(row.get("market_id")))


def grade_parlay_tickets(session: Session, tickets: list[dict[str, Any]]) -> None:
    """Mark finished parlay legs the same way the singles board is marked.

    One lost leg loses the ticket. A ticket is a win only after every leg has
    settled and none of them lost. A push drops out of a ticket that still has
    a winner. An open leg stays unmarked.
    """
    legs = [
        leg
        for ticket in tickets
        if isinstance(ticket, dict)
        for leg in ticket.get("legs") or []
        if isinstance(leg, dict)
    ]
    if not legs:
        return
    props = [leg for leg in legs if leg.get("market_id") != "point_spread"]
    if props:
        _attach_prop_results(session, props)
    game_ids = {leg.get("game_id") for leg in legs if leg.get("game_id")}
    games = {
        game.game_id: game
        for game in session.scalars(select(Game).where(Game.game_id.in_(game_ids)))
    }
    for leg in legs:
        game = games.get(leg.get("game_id"))
        status = None if game is None else str(game.status or "").lower()
        leg["game_status"] = status
        if game is not None:
            leg["home_score"] = game.home_score
            leg["away_score"] = game.away_score
        if leg.get("market_id") != "point_spread":
            leg.setdefault("result", "pending")
            continue
        leg["result"] = _grade_spread_leg(leg, game)
    for ticket in tickets:
        if isinstance(ticket, dict):
            ticket["result"] = _parlay_ticket_result(ticket.get("legs") or [])


def _grade_spread_leg(leg: dict[str, Any], game: Game | None) -> str:
    from spe.models.ats.predict import grade_displayed_pick

    if game is None:
        return "pending"
    status = str(game.status or "").lower()
    if status not in {"final", "post"}:
        return "pending"
    side = str(leg.get("side") or "")
    line = leg.get("line")
    if side not in {"home", "away"} or line is None or game.home_score is None or game.away_score is None:
        return "pending"
    home_handicap = float(line) if side == "home" else -float(line)
    graded = grade_displayed_pick(
        status="final",
        home_score=game.home_score,
        away_score=game.away_score,
        pick_market="spread",
        favored_side=side,
        home_handicap=home_handicap,
    )
    return {"correct": "win", "miss": "loss", "push": "push"}.get(graded or "", "pending")


def _parlay_ticket_result(legs: list[dict[str, Any]]) -> str:
    results = [str(leg.get("result") or "pending") for leg in legs if isinstance(leg, dict)]
    if not results:
        return "pending"
    if any(result == "loss" for result in results):
        return "loss"
    if any(result not in {"win", "push"} for result in results):
        return "pending"
    if any(result == "win" for result in results):
        return "win"
    return "push"


def list_props(
    session: Session,
    market: str | None,
    min_odds: int | None,
    min_ev: float | None,
    league: str | None,
    date: str | None = None,
) -> list[dict[str, Any]]:
    from datetime import timedelta

    # A chosen day keeps its lines after kickoff so the card can be graded.
    # An undated scan stays limited to games that have not started.
    # A finished game is retagged historical. A chosen day still needs those lines.
    hidden = ("demo", "alias") if date else ("demo", "alias", "historical", "season")
    stmt = select(Outcome).join(Game, Game.game_id == Outcome.game_id).where(
        Game.data_tier.notin_(hidden),
    )
    if date:
        stmt = stmt.where(Game.status.notin_(("canceled", "cancelled", "postponed")))
    else:
        stmt = stmt.where(
            Game.kickoff_utc > datetime.now(UTC),
            Game.status.in_(("scheduled", "pre", "upcoming")),
        )
    if market:
        stmt = stmt.where(Outcome.market_id == market)
    if league and league not in {"all", ""}:
        stmt = stmt.where(Game.league_id == league)
    if date:
        start = datetime.fromisoformat(date).replace(tzinfo=SLATE_TZ)
        end = start + timedelta(days=1)
        stmt = stmt.where(
            Game.kickoff_utc >= start.astimezone(UTC).replace(tzinfo=None),
            Game.kickoff_utc < end.astimezone(UTC).replace(tzinfo=None),
        )
    outcomes = list(session.scalars(stmt))
    from spe.models.prop_projection import COUNTING_MARKETS, logs_for_players

    stat_logs = logs_for_players(
        session,
        {outcome.player_id for outcome in outcomes if outcome.player_id and outcome.market_id in COUNTING_MARKETS},
    )
    unavailable = ruled_out_keys(session, [outcome.game_id for outcome in outcomes])
    rows: list[dict[str, Any]] = []
    roster_cache: dict[str, dict[str, Any]] = {}
    from app.board_batch import board_batch
    with board_batch(session, outcomes):
        for outcome in outcomes:
            game_id = outcome.game_id
            if game_id not in roster_cache:
                game = session.get(Game, game_id)
                roster_cache[game_id] = eligible_roster_index(session, game) if game else {}
            row = prop_row(
                session,
                outcome,
                roster_index=roster_cache[game_id],
                stat_logs=stat_logs,
                ruled_out=unavailable,
                include_started=bool(date),
            )
            if not row:
                continue
            if date and kickoff_day(row["kickoff_utc"]) != date:
                continue
            if min_odds is not None and (row["book_price"] is None or row["book_price"] < min_odds):
                continue
            ev = row["forecast"].get("ev")
            if row["market_id"] != "anytime_td" and min_ev is not None and (ev is None or ev < min_ev):
                continue
            rows.append(row)
    rows = without_duplicate_props(rows)
    _attach_td_usage(session, rows)
    from spe.models.player_td.desk_live import enrich_desk_rows

    enrich_desk_rows(session, rows)
    if min_ev is not None:
        rows = [r for r in rows if r["forecast"].get("ev") is not None and r["forecast"]["ev"] >= min_ev]
    rows = [
        r for r in rows
        if not (r.get("forecast") or {}).get("desk_requires_opportunity_score")
        or (r.get("desk_analysis") or {}).get("candidate_probability") is not None
    ]
    _attach_prop_results(session, rows)
    _apply_prop_defense(session, rows)
    rows.sort(
        key=lambda r: (
            r["forecast"].get("ev") is None,
            -(r["forecast"].get("ev") if r["forecast"].get("ev") is not None else r["forecast"].get("p_estimate") or 0),
        )
    )
    return rows


def _apply_prop_defense(session: Session, rows: list[dict[str, Any]]) -> None:
    """Move each counting prop by the defense on the schedule. A rebuilt board keeps it."""
    from spe.models.prop_projection import COUNTING_MARKETS, apply_matchup, load_defense

    tables: dict[str, Any] = {}
    for row in rows:
        if row.get("market_id") not in COUNTING_MARKETS or row.get("projection_source") != "recent":
            continue
        league_id = str(row.get("league_id") or "")
        if not league_id:
            continue
        if league_id not in tables:
            tables[league_id] = load_defense(session, league_id)
        apply_matchup(row, tables[league_id])


def list_desk_calls(
    session: Session,
    league: str | None,
    date: str | None,
) -> dict[str, Any]:
    """One frozen scorer per game, plus every graded desk take for the win-loss column.

    The name is the row marked before kickoff. It is not recomputed from today's board.
    """
    from datetime import timedelta

    from spe.shadow.atd_desk_takes import is_desk_take

    day = date or slate_today()
    start = datetime.fromisoformat(day).replace(tzinfo=SLATE_TZ)
    end = start + timedelta(days=1)
    stmt = (
        select(Game)
        .options(joinedload(Game.home_team), joinedload(Game.away_team))
        .where(Game.kickoff_utc >= start.astimezone(UTC).replace(tzinfo=None))
        .where(Game.kickoff_utc < end.astimezone(UTC).replace(tzinfo=None))
        .where(Game.data_tier.notin_(("demo", "alias")))
    )
    if league and league not in {"all", ""}:
        stmt = stmt.where(Game.league_id == league)
    games = [
        game
        for game in session.scalars(stmt).unique()
        if kickoff_day(game.kickoff_utc) == day and game.status not in {"canceled", "cancelled"}
    ]
    calls = _desk_rows(session, games, is_desk_take)
    settled_games = _games_for_desk_takes(session, league)
    settled = [
        row
        for row in _desk_rows(session, settled_games, is_desk_take)
        if row["settlement"] in {"win", "loss"}
    ]
    return {"date": day, "calls": calls, "settled": settled}


def _games_for_desk_takes(session: Session, league: str | None) -> list[Game]:
    from sqlalchemy import text

    ids = [
        row[0]
        for row in session.execute(
            text(
                """
                SELECT DISTINCT game_id FROM shadow_predictions
                WHERE market_id = 'anytime_td'
                  AND json_extract(components, '$.desk_take') IS NOT NULL
                """
            )
        )
    ]
    if not ids:
        return []
    stmt = (
        select(Game)
        .options(joinedload(Game.home_team), joinedload(Game.away_team))
        .where(Game.game_id.in_(ids))
        .where(Game.data_tier.notin_(("demo", "alias")))
    )
    if league and league not in {"all", ""}:
        stmt = stmt.where(Game.league_id == league)
    return list(session.scalars(stmt).unique())


def _desk_rows(session: Session, games: list[Game], is_desk_take) -> list[dict[str, Any]]:
    if not games:
        return []
    by_game = {game.game_id: game for game in games}
    from spe.shadow.desk_ledger import latest_decisions
    decisions = latest_decisions(session, list(by_game))
    preds = list(
        session.scalars(
            select(ShadowPrediction).where(
                ShadowPrediction.game_id.in_(list(by_game)),
                ShadowPrediction.market_id == "anytime_td",
            )
        )
    )
    chosen: dict[str, ShadowPrediction] = {}
    for pred in preds:
        if not is_desk_take(pred):
            continue
        decision = decisions.get(pred.game_id)
        if decision and (decision.payload or {}).get("shadow_id") != pred.shadow_id:
            continue
        prior = chosen.get(pred.game_id)
        if prior is None or _called_at(pred) >= _called_at(prior):
            chosen[pred.game_id] = pred
    if not chosen:
        return []
    picks = list(chosen.values())
    player_ids = [pred.player_id for pred in picks]
    players = {
        player.player_id: player
        for player in session.scalars(select(Player).where(Player.player_id.in_(player_ids)))
    }
    rosters = list(session.scalars(select(Roster).where(Roster.player_id.in_(player_ids))))
    shadow_ids = [pred.shadow_id for pred in picks]
    settles = {
        row.shadow_id: row
        for row in session.scalars(select(ShadowSettlement).where(ShadowSettlement.shadow_id.in_(shadow_ids)))
    }
    rows: list[dict[str, Any]] = []
    for pred in picks:
        game = by_game[pred.game_id]
        player = players.get(pred.player_id)
        comps = pred.components or {}
        team = _player_team(rosters, pred.player_id, game)
        opponent = None
        if team is not None:
            opponent = game.away_team if team.team_id == game.home_team_id else game.home_team
        settle = settles.get(pred.shadow_id)
        chance = comps.get("board_p")
        if not isinstance(chance, (int, float)):
            chance = pred.forecast_probability
        kick = as_utc(game.kickoff_utc)
        rows.append(
            {
                "shadow_id": pred.shadow_id,
                "league_id": game.league_id,
                "game_id": game.game_id,
                "player_id": pred.player_id,
                "player_name": comps.get("player_name") or (player.name if player else pred.player_id),
                "position": player.position if player else None,
                "family": "player_td_v1",
                "p": float(chance) if isinstance(chance, (int, float)) else None,
                "settlement": settle.result if settle is not None else "pending",
                "kickoff_utc": kick.isoformat() if kick else None,
                "week": game.week,
                "away_abbr": game.away_team.abbreviation if game.away_team else None,
                "home_abbr": game.home_team.abbreviation if game.home_team else None,
                "final_score": _final_score(game),
                "team_abbr": team.abbreviation if team is not None else None,
                "team_mark": _team_mark(team) if team is not None else None,
                "opponent_abbr": opponent.abbreviation if opponent is not None else None,
            }
        )
    rows.sort(key=lambda row: (-(row["p"] or 0), str(row["kickoff_utc"] or "")))
    return rows


def _called_at(pred: ShadowPrediction) -> str:
    return str((pred.components or {}).get("desk_take_at") or "")


def _player_team(rosters: list[Roster], player_id: str, game: Game) -> Team | None:
    sides = {game.home_team_id: game.home_team, game.away_team_id: game.away_team}
    mine = [row for row in rosters if row.player_id == player_id and row.team_id in sides]
    if not mine:
        return None
    mine.sort(key=lambda row: row.valid_from, reverse=True)
    return sides.get(mine[0].team_id)


def published_game_props(session: Session, game: Game) -> list[dict[str, Any]]:
    """Use the same published probabilities, quote and selection as the boards."""
    from spe.props.board_cache import load_slate_props

    day = kickoff_day(game.kickoff_utc)
    if as_utc(game.kickoff_utc) <= datetime.now(UTC):
        return [r for r in list_called_scorers(session, game.league_id, day)
                if r.get("game_id") == game.game_id]
    return [r for r in load_slate_props(session, game.league_id, day)
            if r.get("game_id") == game.game_id]


def list_called_scorers(
    session: Session,
    league: str | None,
    date: str | None,
) -> list[dict[str, Any]]:
    """The scorer we called for a game that has already kicked off.

    The live board drops those games. This keeps last night's call on the page,
    using only weeks before that game so the result cannot change the name.
    """
    from datetime import timedelta

    day = date or slate_today()
    days = {day}
    if day == slate_today():
        prior = (datetime.fromisoformat(day).date() - timedelta(days=1)).isoformat()
        days.add(prior)
    start = datetime.fromisoformat(min(days)).replace(tzinfo=SLATE_TZ)
    end = datetime.fromisoformat(max(days)).replace(tzinfo=SLATE_TZ) + timedelta(days=1)
    now = datetime.now(UTC)
    stmt = select(Game).where(
        Game.kickoff_utc >= start.astimezone(UTC).replace(tzinfo=None),
        Game.kickoff_utc < end.astimezone(UTC).replace(tzinfo=None),
        Game.data_tier.notin_(("demo", "alias")),
    )
    if league and league not in {"all", ""}:
        stmt = stmt.where(Game.league_id == league)
    games = [
        game
        for game in session.scalars(stmt)
        if kickoff_day(game.kickoff_utc) in days
        and game.status not in {"canceled", "cancelled"}
        and as_utc(game.kickoff_utc) is not None
        and as_utc(game.kickoff_utc) <= now
    ]
    if not games:
        return []
    from spe.db.models import DeskDecision
    from spe.shadow.desk_ledger import freeze_desk_rows, latest_decisions

    decisions = latest_decisions(session, [game.game_id for game in games])
    missing = [game for game in games if game.game_id not in decisions]
    if missing:
        built = _started_desk_rows(session, missing)
        # One lock per game, stamped just before that kickoff, from the last pregame price.
        for game in missing:
            kick = as_utc(game.kickoff_utc)
            game_rows = [row for row in built if row.get("game_id") == game.game_id]
            if kick is None:
                continue
            if not game_rows:
                session.add(DeskDecision(
                    game_id=game.game_id,
                    decided_at=kick - timedelta(seconds=1),
                    payload={"row": None, "game_id": game.game_id, "called_at": (kick - timedelta(seconds=1)).isoformat()},
                ))
                continue
            freeze_desk_rows(session, game_rows, kick - timedelta(seconds=1), include_started=True)
        decisions = latest_decisions(session, [game.game_id for game in games])
    called: list[dict[str, Any]] = []
    for game in games:
        record = decisions.get(game.game_id)
        saved = (record.payload or {}).get("row") if record is not None else None
        if not saved:
            continue
        called.append(dict(saved))
    by_game = {game.game_id: game for game in games}
    for row in called:
        game = by_game.get(row.get("game_id"))
        chance = (row.get("forecast") or {}).get("p_estimate")
        tonight = (row.get("forecast") or {}).get("tonight") or {}
        summary = str(tonight.get("summary") or "")
        if isinstance(chance, (int, float)):
            shown = f"{round(float(chance) * 100)}% to score tonight."
            called_at = f"Called at {round(float(chance) * 100)}% before kickoff."
            summary = summary.replace(shown, called_at)
            if tonight:
                tonight = dict(tonight)
                tonight["summary"] = summary
                row["forecast"] = dict(row["forecast"])
                row["forecast"]["tonight"] = tonight
        row["game_status"] = game.status if game else None
        row["final_score"] = _final_score(game) if game else None
        row["result"] = _anytime_result(
            session, str(row.get("game_id") or ""), row.get("player_id")
        )
    called.sort(key=lambda row: str(row.get("kickoff_utc") or ""))
    return called


def _started_desk_rows(session: Session, games: list[Game]) -> list[dict[str, Any]]:
    """Score started games from the last pregame price and the work before kickoff."""
    from datetime import timedelta

    from app.board_batch import board_batch
    from spe.models.player_td.desk_live import enrich_desk_rows

    game_ids = [game.game_id for game in games]
    kicks = {game.game_id: as_utc(game.kickoff_utc) for game in games}
    outcomes = list(
        session.scalars(
            select(Outcome).where(Outcome.game_id.in_(game_ids), Outcome.market_id == "anytime_td")
        )
    )
    if not outcomes:
        return []
    roster_cache: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    with board_batch(session, outcomes):
        for outcome in outcomes:
            if outcome.game_id not in roster_cache:
                game = session.get(Game, outcome.game_id)
                roster_cache[outcome.game_id] = eligible_roster_index(session, game) if game else {}
            row = prop_row(
                session,
                outcome,
                roster_index=roster_cache[outcome.game_id],
                include_started=True,
            )
            if not row:
                continue
            price = _last_pregame_price(session, outcome.outcome_id, kicks.get(outcome.game_id))
            if price is None:
                continue
            row["book_price"] = price
            rows.append(row)
    rows = without_duplicate_props(rows)
    _attach_td_usage(session, rows)
    lock_at = max(kick for kick in kicks.values() if kick is not None) - timedelta(seconds=1)
    enrich_desk_rows(session, rows, now=lock_at)
    return [
        row for row in rows
        if not (row.get("forecast") or {}).get("desk_requires_opportunity_score")
        or (row.get("desk_analysis") or {}).get("candidate_probability") is not None
    ]


def _last_pregame_price(session: Session, outcome_id: str, kickoff: datetime | None) -> int | None:
    kick = as_utc(kickoff)
    batch = session.info.get("board_batch")
    if batch is not None:
        quotes = list(batch.get("quotes", {}).get(outcome_id, []))
    else:
        quotes = list(session.scalars(select(OddsTick).where(OddsTick.outcome_id == outcome_id)))
    pregame = [
        tick for tick in quotes
        if not tick.is_live and abs(tick.price_american) >= 100 and (kick is None or (as_utc(tick.captured_at) or kick) <= kick)
    ]
    if not pregame:
        return None
    latest = max(pregame, key=lambda tick: as_utc(tick.captured_at) or datetime.min.replace(tzinfo=UTC))
    return int(latest.price_american)


def _final_score(game: Game) -> str | None:
    if game.home_score is None or game.away_score is None:
        return None
    away = game.away_team.abbreviation if game.away_team else "Away"
    home = game.home_team.abbreviation if game.home_team else "Home"
    return f"{away} {game.away_score}, {home} {game.home_score}"


def _anytime_result(session: Session, game_id: str, player_id: str | None) -> str:
    if not player_id:
        return "pending"
    stats = (
        session.query(PlayerGameStat)
        .filter(PlayerGameStat.game_id == game_id, PlayerGameStat.player_id == player_id)
        .all()
    )
    if not stats:
        return "pending"
    scored = any(int(row.rush_td or 0) + int(row.rec_td or 0) > 0 for row in stats)
    return "scored" if scored else "blank"


def _attach_td_usage(session: Session, rows: list[dict[str, Any]]) -> None:
    """Recent carries, targets, and red-zone work for the anytime reason line."""
    ids = [row.get("player_id") for row in rows if row.get("market_id") == "anytime_td" and row.get("player_id")]
    if not ids:
        return
    from sqlalchemy import bindparam, text

    raw = session.execute(
        text(
            """
            SELECT p.player_id, p.carries, p.targets, p.rz_targets, p.gl_carries,
                   p.rush_td, p.rec_td, g.kickoff_utc
            FROM player_game_stats p
            JOIN games g ON g.game_id = p.game_id
            WHERE p.player_id IN :ids
              AND lower(g.status) = 'final'
            """
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": list(dict.fromkeys(ids))},
    ).mappings().all()
    grouped: dict[str, list] = {}
    for item in raw:
        grouped.setdefault(item["player_id"], []).append(item)
    for row in rows:
        if row.get("market_id") != "anytime_td":
            continue
        kick = as_utc(row.get("kickoff_utc"))
        games = [g for g in grouped.get(row.get("player_id"), []) if kick and as_utc(g["kickoff_utc"]) and as_utc(g["kickoff_utc"]) < kick]
        games.sort(key=lambda g: as_utc(g["kickoff_utc"]), reverse=True)
        recent = games[:6]
        if not recent:
            continue
        row["usage"] = {"games": len(recent)}
        for key in ("carries", "targets", "rz_targets", "gl_carries", "rush_td", "rec_td"):
            known = [float(item[key]) for item in recent if item[key] is not None]
            row["usage"][key] = sum(known) / len(known) if known else None


def without_duplicate_props(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per player market line. Duplicate outcomes from ingest races stay hidden."""
    best: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[tuple[Any, ...]] = []
    for row in rows:
        key = (
            row.get("game_id"),
            row.get("player_id") or row.get("player_name"),
            row.get("market_id"),
            row.get("side"),
            row.get("line"),
        )
        prior = best.get(key)
        if prior is None:
            best[key] = row
            order.append(key)
            continue
        if _prop_preference(row) > _prop_preference(prior):
            best[key] = row
    return [best[key] for key in order]


def _prop_preference(row: dict[str, Any]) -> tuple[Any, ...]:
    forecast = row.get("forecast") or {}
    ev = forecast.get("ev_after_haircut")
    if ev is None:
        ev = forecast.get("ev")
    p = forecast.get("p_estimate") or 0.0
    oid = str(row.get("outcome_id") or "")
    return (
        1 if row.get("book_price") is not None else 0,
        1 if ev is not None else 0,
        float(ev) if ev is not None else float(p),
        1 if oid.startswith(("atd:", "research:")) else 0,
        oid,
    )
