"""Serve the trained league models on upcoming props.

Appends a forecast. Never updates an existing forecast or an odds tick.
Validated Play stays off: every new recommendation is no-bet and research-only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from spe.analytics.engine import GAME_COMPLETE_AFTER
from spe.db.base import as_utc, new_id
from spe.db.models import (
    Forecast,
    Game,
    InjuryEvent,
    ModelVersion,
    OddsTick,
    Outcome,
    Player,
    Recommendation,
    Roster,
    RosterWeek,
    Season,
    Team,
    TeamAnalytics,
)
from spe.identity.match import match_name
from spe.integrity.assignments import assignment_problems, odds_events, participating_ids
from spe.models.prop_projection import COUNTING_MARKETS, logs_for_players
from spe.models.train import FittedMarket, load_artifacts, predict_row
from spe.recommend.engine import RecommendInput, recommend

MARKETS = ("anytime_td", "receptions", "receiving_yards", "rushing_yards")
RESEARCH_TIER = "research"


def publish_model_forecasts(session: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    summary: dict[str, Any] = {
        "status": "ok",
        "written": 0,
        "unchanged": 0,
        "skipped": 0,
        "play_enabled": False,
        "research_only": True,
        "by_market": {},
        "missing_artifacts": [],
        "identity_skipped": 0,
    }
    games = (
        session.query(Game)
        .filter(
            Game.data_tier == "upcoming",
            Game.status.in_(("scheduled", "preview", "pregame")),
            Game.league_id.in_(("nfl", "fbs")),
        )
        .all()
    )
    if not games:
        summary["status"] = "no_games"
        return summary
    game_by_id = {game.game_id: game for game in games}
    outcomes = (
        session.query(Outcome)
        .filter(
            Outcome.game_id.in_(game_by_id),
            Outcome.market_id.in_(MARKETS),
        )
        .all()
    )
    team_ids = {game.home_team_id for game in games} | {game.away_team_id for game in games}
    teams = {row.team_id: row for row in session.query(Team).filter(Team.team_id.in_(team_ids)).all()}
    from spe.identity.roster import eligible_players_for_game
    from spe.integrity.assignments import linked_team_ids as assignment_links

    links = {
        team_id: sorted(assignment_links(teams.get(team_id)) or {team_id})
        for team_id in team_ids
    }
    roster_rows: list[tuple[str, str, str, str]] = []
    catalogs: dict[str, list[tuple[str, str]]] = {}
    fbs_roles: dict[str, dict[str, str]] = {}
    for game in games:
        eligible = eligible_players_for_game(session, game, include_inactive_for_props=True)
        if game.league_id == "fbs":
            fbs_roles[game.game_id] = {row.player_id: row.role for row in eligible}
        catalogs[game.game_id] = [(row.player_id, row.name) for row in eligible]
        for row in eligible:
            roster_rows.append((row.player_id, row.team_id, row.name, row.position_group))
    roster_team = {(player_id, team_id): team_id for player_id, team_id, _name, _pos in roster_rows}
    resolved = 0
    for outcome in outcomes:
        if outcome.player_id:
            continue
        game = game_by_id[outcome.game_id]
        player_id = _resolve_player(outcome.description or "", catalogs.get(game.game_id, []))
        if not player_id:
            continue
        outcome.player_id = player_id
        resolved += 1
    summary["players_linked"] = resolved
    player_ids = {row.player_id for row in outcomes if row.player_id}
    players = {
        row.player_id: row
        for row in session.query(Player).filter(Player.player_id.in_(player_ids or {""})).all()
    }
    outcome_ids = [row.outcome_id for row in outcomes]
    latest = _latest_model_forecasts(session, outcome_ids)
    ticks = _latest_ticks(session, outcome_ids)
    injuries = _latest_injuries(session, [game.game_id for game in games], player_ids)
    complete_reports = _games_with_complete_injury_reports(session, [game.game_id for game in games])
    membership = _membership(session, player_ids)
    season_year = {
        row.season_id: row.year
        for row in session.query(Season).filter(Season.season_id.in_({game.season_id for game in games} or {""})).all()
    }
    events = odds_events(session)
    defense = {league: _defense_history(session, league) for league in ("nfl", "fbs")}
    artifacts = {league: load_artifacts(league) for league in ("nfl", "fbs")}
    stat_logs = logs_for_players(
        session,
        {row.player_id for row in outcomes if row.player_id and row.market_id in COUNTING_MARKETS},
    )
    from spe.identity.roles import workload_factor
    from spe.market.price_age import max_fresh_minutes
    from spe.ops.availability import availability_for_player, games_with_inactive_list, recommend_availability_fields

    inactive_lists = games_with_inactive_list(session, [game.game_id for game in games])

    for outcome in outcomes:
        game = game_by_id[outcome.game_id]
        fitted = artifacts.get(game.league_id, {}).get(outcome.market_id)
        if not outcome.player_id:
            summary["skipped"] += 1
            continue
        if fitted is None:
            if game.league_id not in summary["missing_artifacts"]:
                summary["missing_artifacts"].append(game.league_id)
            summary["skipped"] += 1
            continue
        if outcome.market_id != "anytime_td" and outcome.line is None:
            summary["skipped"] += 1
            continue
        player = players.get(outcome.player_id or "")
        tick = ticks.get(outcome.outcome_id)
        event_home, event_away = _event_sides(tick, events)
        year = season_year.get(game.season_id)
        week_team = None
        if year is not None and game.week is not None:
            week_team = membership["weeks"].get((outcome.player_id, year, game.week))
        problems = assignment_problems(
            game=game,
            player=player,
            description=outcome.description or "",
            participant_ids=participating_ids(game, teams),
            roster_rows=membership["rosters"].get(outcome.player_id or "", []),
            roster_week_team_id=week_team,
            event_home=event_home,
            event_away=event_away,
            teams=teams,
        )
        if problems:
            summary["skipped"] += 1
            summary["identity_skipped"] += 1
            continue
        home_links = links.get(game.home_team_id, [game.home_team_id])
        away_links = links.get(game.away_team_id, [game.away_team_id])
        team_id = _team_for(outcome.player_id or "", home_links, away_links, roster_team)
        opponent_id = _opponent(team_id, home_links, away_links, defense.get(game.league_id, {}))
        z = _opponent_z(defense.get(game.league_id, {}), opponent_id, fitted, now)
        pred = predict_row(
            fitted,
            outcome.player_id or "",
            team_id,
            player.position_group if player else "",
            z,
            int(fitted.player_n.get(outcome.player_id or "", 0)),
            line=None if outcome.market_id == "anytime_td" else outcome.line,
        )
        if outcome.market_id in COUNTING_MARKETS and pred.get("p") is not None:
            from spe.models.prop_projection import adjust_over_prediction

            role = fbs_roles.get(game.game_id, {}).get(outcome.player_id or "")
            pred = adjust_over_prediction(
                pred,
                market_id=outcome.market_id,
                line=outcome.line,
                sigma=fitted.sigma,
                kickoff=game.kickoff_utc,
                logs=stat_logs.get(outcome.player_id or "", []),
                workload_factor=workload_factor(role),
            )
        if pred.get("status") == "rejected_no_player_history" or pred.get("p") is None:
            summary["skipped"] += 1
            continue
        p, p_low, p_high = _side_probability(outcome.side, pred)
        version_id = f"m3-{game.league_id}-{outcome.market_id}"
        _ensure_version(session, version_id, game.league_id, outcome.market_id, fitted)
        avail = availability_for_player(
            league_id=game.league_id,
            kickoff=game.kickoff_utc,
            player_injury=injuries.get((game.game_id, outcome.player_id or "")),
            game_has_complete_report=bool(complete_reports.get(game.game_id)),
            now=now,
            inactive_list_received=game.game_id in inactive_lists,
        )
        status, avail_p, confidence = recommend_availability_fields(avail)
        previous = latest.get((outcome.outcome_id, version_id))
        if previous and _same_prediction(
            previous,
            p,
            outcome.line,
            availability_status=status,
            injury_coverage=str(avail["coverage"]),
        ):
            summary["unchanged"] += 1
            continue
        result = recommend(
            RecommendInput(
                p=p,
                p_low=p_low,
                p_high=p_high,
                american=tick.price_american if tick else None,
                price_captured_at=tick.captured_at if tick else None,
                game_status=game.status if game.status in {"scheduled", "preview", "pregame"} else "scheduled",
                availability_status=status,
                availability_prob=avail_p,
                injury_confidence=confidence,
                source_fresh=True,
                grading_supported=True,
                calibration_n=0,
                max_price_age_minutes=max_fresh_minutes(game.kickoff_utc, now),
            ),
            now=now,
        )
        reasons = _merge_reason_codes(list(avail["abstain_reasons"]), list(result.reason_codes))
        forecast = Forecast(
            forecast_id=new_id(),
            outcome_id=outcome.outcome_id,
            as_of=now,
            model_version_id=version_id,
            p=p,
            p_low=p_low,
            p_high=p_high,
            sim_count=0,
            data_cutoff=now,
            availability_status=status,
            reason_codes=reasons,
            components={
                "mean": pred["mean"],
                "line": outcome.line,
                "line_source": "anytime_td" if outcome.market_id == "anytime_td" else "sportsbook",
                "side": outcome.side,
                "opp_def_z": z,
                "projection_games": pred.get("projection_games"),
                "projection_source": pred.get("projection_source"),
                "model_mean": pred.get("model_mean"),
                "research_only": True,
                "play_enabled": False,
                "position_group": player.position_group if player else "",
                "injury_coverage": avail["coverage"],
            },
            is_official=True,
            data_tier=RESEARCH_TIER,
        )
        session.add(forecast)
        session.add(
            Recommendation(
                forecast_id=forecast.forecast_id,
                odds_tick_id=tick.tick_id if tick else None,
                ev=result.ev,
                ev_after_haircut=result.ev_after_haircut,
                threshold=result.threshold,
                status="no_bet",
                reason_codes=reasons,
                kelly_fractional=None,
                created_at=now,
                immutable=True,
                data_tier=RESEARCH_TIER,
            )
        )
        latest[(outcome.outcome_id, version_id)] = forecast
        summary["written"] += 1
        key = f"{game.league_id}:{outcome.market_id}"
        summary["by_market"][key] = int(summary["by_market"].get(key, 0)) + 1
    session.flush()
    return summary


def _latest_model_forecasts(session: Session, outcome_ids: list[str]) -> dict[tuple[str, str], Forecast]:
    found: dict[tuple[str, str], Forecast] = {}
    if not outcome_ids:
        return found
    rows = (
        session.query(Forecast)
        .filter(Forecast.outcome_id.in_(outcome_ids), Forecast.model_version_id.like("m3-%"))
        .all()
    )
    for row in rows:
        key = (row.outcome_id, row.model_version_id)
        current = found.get(key)
        if current is None or as_utc(row.as_of) >= as_utc(current.as_of):
            found[key] = row
    return found


def _same_prediction(
    previous: Forecast,
    p: float,
    line: float | None,
    *,
    availability_status: str | None = None,
    injury_coverage: str | None = None,
) -> bool:
    stored_line = (previous.components or {}).get("line")
    if previous.data_tier != RESEARCH_TIER or abs(previous.p - p) >= 1e-6:
        return False
    if availability_status is not None and previous.availability_status != availability_status:
        return False
    if injury_coverage is not None and (previous.components or {}).get("injury_coverage") != injury_coverage:
        return False
    if stored_line is None or line is None:
        return stored_line is None and line is None
    return abs(float(stored_line) - float(line)) < 1e-6


def _side_probability(side: str, pred: dict[str, float]) -> tuple[float, float, float]:
    p = float(pred["p"])
    low = float(pred["p_low"])
    high = float(pred["p_high"])
    if side in {"under", "no"}:
        return 1.0 - p, max(1e-6, 1.0 - high), min(1.0, 1.0 - low)
    return p, low, high


def _linked_team_ids(team: Team | None) -> list[str]:
    """Historical roster ids for an ESPN scoreboard team, plus the team itself."""
    if team is None:
        return []
    found = [team.team_id]
    if team.league_id == "fbs":
        for alias in team.aliases or []:
            espn_id = alias.get("espn_id") if isinstance(alias, dict) else None
            if espn_id:
                found.append(f"fbs:{espn_id}")
    out: list[str] = []
    for team_id in found:
        if team_id not in out:
            out.append(team_id)
    return out


def _game_catalogs(
    games: list[Game],
    links: dict[str, list[str]],
    roster_rows: list[tuple[str, str, str, str]],
) -> dict[str, list[tuple[str, str]]]:
    by_team: dict[str, list[tuple[str, str]]] = {}
    for player_id, team_id, name, _position in roster_rows:
        by_team.setdefault(team_id, []).append((player_id, name))
    catalogs: dict[str, list[tuple[str, str]]] = {}
    for game in games:
        seen: set[str] = set()
        catalog: list[tuple[str, str]] = []
        for team_id in links.get(game.home_team_id, []) + links.get(game.away_team_id, []):
            for player_id, name in by_team.get(team_id, []):
                if player_id in seen:
                    continue
                seen.add(player_id)
                catalog.append((player_id, name))
        catalogs[game.game_id] = catalog
    return catalogs


def _resolve_player(description: str, catalog: list[tuple[str, str]]) -> str | None:
    if not description or not catalog:
        return None
    decision = match_name(description, catalog)
    if decision.review_status != "auto" or not decision.canonical_id:
        return None
    return decision.canonical_id


def _team_for(
    player_id: str,
    home_links: list[str],
    away_links: list[str],
    roster_team: dict[tuple[str, str], str],
) -> str:
    for team_id in home_links + away_links:
        if (player_id, team_id) in roster_team:
            return team_id
    return ""


def _opponent(
    team_id: str,
    home_links: list[str],
    away_links: list[str],
    history: dict[str, list[tuple[datetime, float]]],
) -> str:
    if team_id in home_links:
        other = away_links
    elif team_id in away_links:
        other = home_links
    else:
        return ""
    for candidate in other:
        if candidate in history:
            return candidate
    return other[0] if other else ""


def _defense_history(session: Session, league_id: str) -> dict[str, list[tuple[datetime, float]]]:
    rows = (
        session.query(TeamAnalytics.team_id, TeamAnalytics.metrics, Game.kickoff_utc)
        .join(Game, Game.game_id == TeamAnalytics.game_id)
        .filter(
            TeamAnalytics.league_id == league_id,
            TeamAnalytics.grain == "game",
            Game.league_id == league_id,
            Game.status == "final",
        )
        .all()
    )
    history: dict[str, list[tuple[datetime, float]]] = {}
    for team_id, metrics, kickoff in rows:
        value = (metrics or {}).get("def_epa_per_play")
        if value is None or kickoff is None:
            continue
        history.setdefault(team_id, []).append((as_utc(kickoff) or kickoff, float(value)))
    for values in history.values():
        values.sort(key=lambda item: item[0])
    return history


def _opponent_z(
    history: dict[str, list[tuple[datetime, float]]],
    opponent_id: str,
    fitted: FittedMarket,
    as_of: datetime,
) -> float:
    if not opponent_id:
        return 0.0
    cutoff = as_utc(as_of) or as_of
    vals = [
        value
        for kickoff, value in history.get(opponent_id, [])
        if kickoff + GAME_COMPLETE_AFTER <= cutoff
    ]
    if not vals or fitted.z_std < 1e-8:
        return 0.0
    return (sum(vals) / len(vals) - fitted.z_mean) / fitted.z_std


def _ensure_version(
    session: Session,
    version_id: str,
    league_id: str,
    market_id: str,
    fitted: FittedMarket,
) -> ModelVersion:
    row = session.get(ModelVersion, version_id)
    if row is not None:
        return row
    row = ModelVersion(
        model_version_id=version_id,
        code_sha="m3-hierarchical-v1",
        training_cutoff=datetime.fromisoformat(fitted.train_end),
        data_snapshot="historical_pregame_complete",
        hyperparameters={"k_player": fitted.k_player, "opp_beta": fitted.opp_beta, "play_enabled": False},
        calibration_version="platt_v1" if market_id == "anytime_td" else "none",
        league_id=league_id,
        family=fitted.family,
    )
    session.add(row)
    session.flush()
    return row


def _membership(session: Session, player_ids: set[str]) -> dict[str, Any]:
    rosters: dict[str, list[Roster]] = {}
    weeks: dict[tuple[str, int, int], str] = {}
    if not player_ids:
        return {"rosters": rosters, "weeks": weeks}
    for row in session.query(Roster).filter(Roster.player_id.in_(player_ids)).all():
        rosters.setdefault(row.player_id, []).append(row)
    for row in session.query(RosterWeek).filter(RosterWeek.player_id.in_(player_ids)).all():
        weeks[(row.player_id, row.season_year, row.week)] = row.team_id
    return {"rosters": rosters, "weeks": weeks}


def _event_sides(tick: OddsTick | None, events: dict[str, tuple[str | None, str | None]]) -> tuple[str | None, str | None]:
    if tick is None or tick.provider != "odds_api" or not tick.provider_record_id:
        return None, None
    event_id = tick.provider_record_id.split(":", 1)[0]
    return events.get(event_id, (None, None))


def _latest_ticks(session: Session, outcome_ids: list[str]) -> dict[str, OddsTick]:
    found: dict[str, OddsTick] = {}
    if not outcome_ids:
        return found
    rows = session.query(OddsTick).filter(OddsTick.outcome_id.in_(outcome_ids)).all()
    for row in rows:
        current = found.get(row.outcome_id)
        if current is None or (as_utc(row.captured_at) or row.captured_at) >= (as_utc(current.captured_at) or current.captured_at):
            found[row.outcome_id] = row
    return found


def _latest_injuries(
    session: Session,
    game_ids: list[str],
    player_ids: set[str],
) -> dict[tuple[str, str], InjuryEvent]:
    found: dict[tuple[str, str], InjuryEvent] = {}
    if not game_ids or not player_ids:
        return found
    rows = (
        session.query(InjuryEvent)
        .filter(InjuryEvent.game_id.in_(game_ids), InjuryEvent.player_id.in_(player_ids))
        .all()
    )
    for row in rows:
        key = (row.game_id, row.player_id)
        current = found.get(key)
        if current is None or (as_utc(row.reported_at) or row.reported_at) >= (as_utc(current.reported_at) or current.reported_at):
            found[key] = row
    return found


def _games_with_complete_injury_reports(session: Session, game_ids: list[str]) -> dict[str, bool]:
    if not game_ids:
        return {}
    rows = (
        session.query(InjuryEvent.game_id)
        .filter(InjuryEvent.game_id.in_(game_ids), InjuryEvent.coverage_complete.is_(True))
        .distinct()
        .all()
    )
    return {game_id: True for (game_id,) in rows}


def _merge_reason_codes(avail_reasons: list[str], recommend_reasons: list[str]) -> list[str]:
    specific = {
        "INJURY_REPORT_NOT_YET_SCHEDULED",
        "INJURY_REPORT_EXPECTED_MISSING",
        "INJURY_SOURCE_UNAVAILABLE",
        "INJURY_COVERAGE_INCOMPLETE",
        "CONFIRMED_INACTIVE",
        "UNCERTAIN_PLAYER_AVAILABILITY",
    }
    out: list[str] = ["RESEARCH_ONLY", "PLAY_DISABLED"]
    seen: set[str] = set(out)
    for reason in avail_reasons + recommend_reasons:
        if reason in seen:
            continue
        if reason == "INJURY_COVERAGE_INCOMPLETE" and (set(avail_reasons) & (specific - {"INJURY_COVERAGE_INCOMPLETE"})):
            continue
        seen.add(reason)
        out.append(reason)
    return out


def _availability_from(row: InjuryEvent | None) -> tuple[str, float, float | None]:
    """Legacy helper kept for tests. Prefer availability_for_player."""
    from spe.ops.availability import classify_injury_row

    status, prob, confidence = classify_injury_row(row)
    mapped = {
        "confirmed_inactive": "doubtful",
        "expected_active": "probable",
        "uncertain": "questionable",
        "doubtful": "doubtful",
        "unknown": "unknown",
    }.get(status, "unknown")
    return mapped, prob, confidence
