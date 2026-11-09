"""Current skill players for a slate day, with a shrunk anytime-TD rate.

There is no book price on these rows. The call stays no-bet. The rate is how
often the player scored in games we already have, pulled toward a position prior.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, joinedload

from spe.db.models import (
    Forecast,
    Game,
    InjuryEvent,
    Market,
    ModelVersion,
    OddsTick,
    Outcome,
    Player,
    Recommendation,
    Roster,
    Team,
)
from spe.recommend.engine import RecommendInput, recommend
from spe.ingest.canonical import position_group

SLATE_TZ = ZoneInfo("America/Chicago")
SKILL = ("QB", "RB", "FB", "WR", "TE")
KEEP = {"QB": 2, "RB": 4, "WR": 5, "TE": 2, "FB": 1}
PRIORS = {"QB": 0.12, "RB": 0.18, "WR": 0.11, "TE": 0.07, "FB": 0.06}
PRIOR_GAMES = 8
USER_AGENT = "caliber-scoreboard/1.0"


def publish_slate_scorers(session: Session, date: str | None = None) -> dict[str, int]:
    day = date or datetime.now(SLATE_TZ).date().isoformat()
    if session.get(Market, "anytime_td") is None:
        return {"date": day, "games": 0, "players": 0, "skipped": 0}
    games = _games_on(session, day)
    players = 0
    skipped = 0
    seen_rosters: dict[str, list[dict[str, str]]] = {}
    for game in games:
        for team in (game.away_team, game.home_team):
            athletes = seen_rosters.get(team.team_id)
            if athletes is None:
                athletes = fetch_skill_roster(team)
                seen_rosters[team.team_id] = athletes
                time.sleep(0.08)
            if not athletes:
                skipped += 1
                continue
            chosen = _choose(session, athletes)
            for athlete in chosen:
                if _write_scorer(session, game, team, athlete):
                    players += 1
    session.flush()
    return {"date": day, "games": len(games), "players": players, "skipped": skipped}


def publish_priced_forecasts(session: Session, now: datetime | None = None) -> dict[str, int]:
    """Put the scoring-rate forecast on upcoming outcomes that already have a live price."""
    now = now or datetime.now(UTC)
    horizon = now + timedelta(days=8)
    games = (
        session.query(Game)
        .filter(
            Game.data_tier == "upcoming",
            Game.status.in_(("scheduled", "preview", "pregame")),
            Game.kickoff_utc >= now - timedelta(hours=6),
            Game.kickoff_utc <= horizon,
        )
        .all()
    )
    written = 0
    for game in games:
        version = _ensure_version(session, game.league_id, now)
        outcomes = (
            session.query(Outcome)
            .filter(Outcome.game_id == game.game_id, Outcome.market_id == "anytime_td", Outcome.player_id.is_not(None))
            .all()
        )
        chosen: dict[str, tuple[OddsTick, Outcome]] = {}
        for outcome in outcomes:
            tick = (
                session.query(OddsTick)
                .filter(OddsTick.outcome_id == outcome.outcome_id)
                .order_by(OddsTick.captured_at.desc())
                .first()
            )
            if tick is None or not outcome.player_id:
                continue
            current = chosen.get(outcome.player_id)
            if current is None or tick.captured_at >= current[0].captured_at:
                chosen[outcome.player_id] = (tick, outcome)
        for player_id, (tick, outcome) in chosen.items():
            if _write_priced(session, game, version, outcome, tick, player_id, now):
                written += 1
    session.flush()
    return {"games": len(games), "forecasts": written}


def fetch_skill_roster(team: Team) -> list[dict[str, str]]:
    espn_id = _espn_team_id(team)
    if not espn_id:
        return []
    league = "nfl" if team.league_id == "nfl" else "college-football"
    url = f"https://site.api.espn.com/apis/site/v2/sports/football/{league}/teams/{espn_id}/roster"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return []
    athletes: list[dict[str, str]] = []
    for group in payload.get("athletes") or []:
        for item in group.get("items") or []:
            pos = str(((item.get("position") or {}).get("abbreviation")) or "").upper()
            if pos not in SKILL:
                continue
            espn_player = str(item.get("id") or "").strip()
            name = str(item.get("displayName") or item.get("fullName") or "").strip()
            if not espn_player or not name:
                continue
            athletes.append({"espn_id": espn_player, "name": name[:128], "position": pos})
    return athletes


def _games_on(session: Session, day: str) -> list[Game]:
    start = datetime.fromisoformat(day).replace(tzinfo=SLATE_TZ)
    end = start + timedelta(days=1)
    start_utc = start.astimezone(UTC).replace(tzinfo=None)
    end_utc = end.astimezone(UTC).replace(tzinfo=None)
    stmt = (
        select(Game)
        .options(joinedload(Game.home_team), joinedload(Game.away_team))
        .where(Game.data_tier.notin_(("historical", "season")))
        .where(Game.kickoff_utc >= start_utc, Game.kickoff_utc < end_utc)
    )
    return list(session.scalars(stmt).unique())


def _choose(session: Session, athletes: list[dict[str, str]]) -> list[dict[str, str]]:
    rates = {row["espn_id"]: _rate(session, row["espn_id"], row["position"]) for row in athletes}
    picked: list[dict[str, str]] = []
    for pos, limit in KEEP.items():
        group = [row for row in athletes if row["position"] == pos]
        group.sort(key=lambda row: (-rates[row["espn_id"]][0], row["name"]))
        for row in group[:limit]:
            row["p"], row["games"], row["td_games"] = _pack(rates[row["espn_id"]])
            picked.append(row)
    return picked


def _pack(rate: tuple[float, int, int]) -> tuple[str, str, str]:
    p, games, td_games = rate
    return f"{p:.4f}", str(games), str(td_games)


def _rate(session: Session, espn_id: str, position: str) -> tuple[float, int, int]:
    from spe.db.models import PlayerGameStat

    player_ids = [f"fbs:{espn_id}", f"nfl:{espn_id}"]
    games, td_games = session.execute(
        select(
            func.count(PlayerGameStat.id),
            func.coalesce(
                func.sum(case((PlayerGameStat.rush_td + PlayerGameStat.rec_td > 0, 1), else_=0)),
                0,
            ),
        ).where(PlayerGameStat.player_id.in_(player_ids))
    ).one()
    games_n = int(games or 0)
    scored = int(td_games or 0)
    prior = PRIORS.get(position, 0.08)
    p = (scored + PRIOR_GAMES * prior) / (games_n + PRIOR_GAMES)
    return max(0.01, min(0.75, p)), games_n, scored


def _write_scorer(session: Session, game: Game, team: Team, athlete: dict[str, str]) -> bool:
    player = _ensure_player(session, team, athlete)
    _ensure_roster(session, team.team_id, player.player_id)
    p = float(athlete["p"])
    games_n = int(athlete["games"])
    spread = 0.1 if games_n >= 8 else 0.18
    now = datetime.now(UTC)
    version = _ensure_version(session, team.league_id, now)
    outcome = _ensure_outcome(session, game.game_id, player.player_id, athlete["name"])
    forecast = (
        session.query(Forecast)
        .filter(Forecast.outcome_id == outcome.outcome_id, Forecast.model_version_id == version.model_version_id)
        .one_or_none()
    )
    if forecast is None:
        forecast = Forecast(
            forecast_id=_short("fc", f"{outcome.outcome_id}:{version.model_version_id}"),
            outcome_id=outcome.outcome_id,
            as_of=now,
            model_version_id=version.model_version_id,
            p=p,
            p_low=max(0.01, p - spread),
            p_high=min(0.9, p + spread),
            sim_count=0,
            data_cutoff=now,
            availability_status="unknown",
            reason_codes=["NO_CURRENT_PRICE", "SCORING_RATE"],
            components={"games": games_n, "td_games": int(athlete["td_games"]), "position": athlete["position"]},
            is_official=True,
            data_tier="upcoming",
        )
        session.add(forecast)
        session.flush()
    else:
        forecast.p = p
        forecast.p_low = max(0.01, p - spread)
        forecast.p_high = min(0.9, p + spread)
        forecast.as_of = now
        forecast.data_tier = "upcoming"
        forecast.is_official = True
    existing_rec = (
        session.query(Recommendation).filter(Recommendation.forecast_id == forecast.forecast_id).one_or_none()
    )
    if existing_rec is None:
        session.add(
            Recommendation(
                forecast_id=forecast.forecast_id,
                odds_tick_id=None,
                ev=None,
                ev_after_haircut=None,
                threshold=0.04,
                status="no_bet",
                reason_codes=["NO_CURRENT_PRICE"],
                kelly_fractional=None,
                created_at=now,
                data_tier="upcoming",
            )
        )
    return True


def _write_priced(session, game, version, outcome, tick, player_id: str, now: datetime) -> bool:
    player = session.get(Player, player_id)
    position = player.position if player else "WR"
    p, games_n, td_games = _rate_player(session, player_id, position)
    spread = 0.1 if games_n >= 8 else 0.18
    forecast = (
        session.query(Forecast)
        .filter(Forecast.outcome_id == outcome.outcome_id, Forecast.is_official.is_(True))
        .order_by(Forecast.as_of.desc())
        .first()
    )
    if forecast is None:
        forecast = Forecast(
            forecast_id=_short("fc", f"{outcome.outcome_id}:{version.model_version_id}"),
            outcome_id=outcome.outcome_id,
            as_of=now,
            model_version_id=version.model_version_id,
            p=p,
            p_low=max(0.01, p - spread),
            p_high=min(0.9, p + spread),
            sim_count=0,
            data_cutoff=now,
            availability_status="unknown",
            reason_codes=["SCORING_RATE"],
            components={"games": games_n, "td_games": td_games, "position": position},
            is_official=True,
            data_tier="upcoming",
        )
        session.add(forecast)
        session.flush()
    else:
        forecast.p = p
        forecast.p_low = max(0.01, p - spread)
        forecast.p_high = min(0.9, p + spread)
        forecast.as_of = now
        forecast.data_tier = "upcoming"
        forecast.data_cutoff = now
        forecast.components = {"games": games_n, "td_games": td_games, "position": position}
    status, prob, confidence = _availability(session, game.game_id, player_id)
    forecast.availability_status = status
    result = recommend(
        RecommendInput(
            p=p,
            p_low=forecast.p_low,
            p_high=forecast.p_high,
            american=tick.price_american,
            price_captured_at=tick.captured_at,
            game_status=game.status if game.status in {"scheduled", "preview", "pregame"} else "scheduled",
            availability_status=status,
            availability_prob=prob,
            injury_confidence=confidence,
            source_fresh=True,
            grading_supported=True,
            calibration_n=0,
        ),
        now=now,
    )
    forecast.reason_codes = result.reason_codes
    existing = (
        session.query(Recommendation)
        .filter(Recommendation.forecast_id == forecast.forecast_id)
        .order_by(Recommendation.created_at.desc())
        .first()
    )
    if existing is None:
        session.add(
            Recommendation(
                forecast_id=forecast.forecast_id,
                odds_tick_id=tick.tick_id,
                ev=result.ev,
                ev_after_haircut=result.ev_after_haircut,
                threshold=result.threshold,
                status=result.status,
                reason_codes=result.reason_codes,
                kelly_fractional=result.kelly_fractional,
                created_at=now,
                data_tier="upcoming",
            )
        )
    else:
        existing.odds_tick_id = tick.tick_id
        existing.ev = result.ev
        existing.ev_after_haircut = result.ev_after_haircut
        existing.status = result.status
        existing.reason_codes = result.reason_codes
        existing.kelly_fractional = result.kelly_fractional
        existing.data_tier = "upcoming"
    return True


def _rate_player(session: Session, player_id: str, position: str) -> tuple[float, int, int]:
    from spe.db.models import PlayerGameStat

    games, td_games = session.execute(
        select(
            func.count(PlayerGameStat.id),
            func.coalesce(
                func.sum(case((PlayerGameStat.rush_td + PlayerGameStat.rec_td > 0, 1), else_=0)),
                0,
            ),
        ).where(PlayerGameStat.player_id == player_id)
    ).one()
    games_n = int(games or 0)
    scored = int(td_games or 0)
    prior = PRIORS.get(position_group(position), PRIORS.get(position, 0.08))
    p = (scored + PRIOR_GAMES * prior) / (games_n + PRIOR_GAMES)
    return max(0.01, min(0.75, p)), games_n, scored


def _availability(session: Session, game_id: str, player_id: str) -> tuple[str, float, float | None]:
    row = (
        session.query(InjuryEvent)
        .filter(InjuryEvent.game_id == game_id, InjuryEvent.player_id == player_id)
        .order_by(InjuryEvent.reported_at.desc())
        .first()
    )
    if row is None:
        # A finished report that does not name this player is not a missing report.
        report = (
            session.query(InjuryEvent)
            .filter(InjuryEvent.game_id == game_id, InjuryEvent.coverage_complete.is_(True))
            .first()
        )
        if report is not None:
            return "probable", 0.92, report.confidence
        return "unknown", 0.0, None
    label = f"{row.practice_status or ''} {row.designation or ''}".lower()
    words = set(label.replace("/", " ").split())
    if "did not" in label or "injured reserve" in label or words & {"out", "ir", "pup", "suspended"}:
        return "doubtful", 0.35, row.confidence
    if "limited" in label or "questionable" in label:
        return "questionable", 0.7, row.confidence
    if "full" in label or "probable" in label:
        return "probable", 0.92, row.confidence
    return "unknown", 0.0, row.confidence


def _ensure_player(session: Session, team: Team, athlete: dict[str, str]) -> Player:
    """Prefer GSIS (NFL) or existing CFBD ids. Never mint ``nfl:{espn_numeric}`` as canonical."""
    group = position_group(athlete["position"])
    player_id: str | None = None
    if team.league_id == "nfl":
        from spe.identity.players import resolve_nfl_player_id

        player_id = resolve_nfl_player_id(session, athlete["espn_id"], provider="espn")
    else:
        player_id = f"fbs:{athlete['espn_id']}"
    if not player_id:
        # Unmapped ESPN athlete — keep a review alias id, do not treat as GSIS.
        player_id = f"espn-nfl:{athlete['espn_id']}"
    row = session.get(Player, player_id)
    if row is None:
        row = Player(
            player_id=player_id,
            name=athlete["name"],
            position_group=group,
            position=athlete["position"][:16],
            aliases=[{"espn_id": athlete["espn_id"]}],
        )
        session.add(row)
        session.flush()
    elif row.name != athlete["name"]:
        row.name = athlete["name"]
    return row


def _ensure_roster(session: Session, team_id: str, player_id: str) -> None:
    open_row = (
        session.query(Roster)
        .filter(Roster.team_id == team_id, Roster.player_id == player_id, Roster.valid_to.is_(None))
        .first()
    )
    if open_row:
        return
    now = datetime.now(UTC)
    session.add(
        Roster(
            team_id=team_id,
            player_id=player_id,
            valid_from=now,
            valid_to=None,
            status="active",
        )
    )


def _ensure_outcome(session: Session, game_id: str, player_id: str, name: str) -> Outcome:
    oid = _short("atd", f"{game_id}:{player_id}")
    row = session.get(Outcome, oid)
    if row:
        return row
    row = (
        session.query(Outcome)
        .filter_by(market_id="anytime_td", game_id=game_id, player_id=player_id, side="yes")
        .filter(Outcome.line.is_(None))
        .order_by(Outcome.outcome_id)
        .first()
    )
    if row:
        return row
    row = Outcome(
        outcome_id=oid,
        market_id="anytime_td",
        game_id=game_id,
        player_id=player_id,
        side="yes",
        line=None,
        description=f"{name} anytime td"[:256],
    )
    session.add(row)
    session.flush()
    return row


def _ensure_version(session: Session, league_id: str, now: datetime) -> ModelVersion:
    version_id = f"slate-td-rate-{league_id}"
    row = session.get(ModelVersion, version_id)
    if row:
        return row
    row = ModelVersion(
        model_version_id=version_id,
        code_sha="scoring-rate",
        training_cutoff=now,
        data_snapshot="player_game_stats",
        hyperparameters={"prior_games": PRIOR_GAMES, "priors": PRIORS},
        calibration_version="none",
        league_id=league_id,
        family="anytime_td",
        created_at=now,
    )
    session.add(row)
    session.flush()
    return row


def _espn_team_id(team: Team) -> str | None:
    tail = team.team_id.split(":")[-1]
    if tail.isdigit():
        return tail
    for item in team.aliases or []:
        if isinstance(item, dict) and str(item.get("espn_id") or "").isdigit():
            return str(item["espn_id"])
    return None


def _short(prefix: str, key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:40]
    return f"{prefix}:{digest}"
