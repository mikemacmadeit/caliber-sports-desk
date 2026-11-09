"""Point-in-time packets. Rows timestamped after as_of stay out, including backtests."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.db.models import (
    DepthChartSnapshot,
    DepthChartWeek,
    Forecast,
    Game,
    InjuryEvent,
    NewsArticle,
    OddsTick,
    Outcome,
    Player,
    PlayerAnalytics,
    Recommendation,
    TeamAnalytics,
    WeatherSnapshot,
)

METRIC_KEYS = (
    "targets",
    "carries",
    "receptions",
    "rec_yards",
    "rush_yards",
    "snap_share",
    "routes",
    "off_epa_per_play",
    "def_epa_per_play",
    "pace_sec_per_play",
    "success_rate",
    "off_success_rate",
    "def_success_rate",
    "points_per_scoring_opp",
    "red_zone_td_rate",
    "rz_targets",
    "rz_carries",
    "gl_carries",
)

TEAM_MATCHUP_KEYS = (
    "off_epa_per_play",
    "def_epa_per_play",
    "pace_sec_per_play",
    "success_rate",
    "off_success_rate",
    "def_success_rate",
    "points_per_scoring_opp",
    "red_zone_td_rate",
)

PROP_FOCUS = (
    "anytime_td",
    "receptions",
    "receiving_yards",
    "rushing_yards",
)


def research_as_of(game: Game, as_of: datetime | None) -> datetime:
    kick = as_utc(game.kickoff_utc) or datetime.now(UTC)
    now = datetime.now(UTC)
    cutoff = kick - timedelta(seconds=1)
    chosen = as_of or min(now, cutoff)
    chosen = as_utc(chosen) or cutoff
    if chosen >= kick:
        return cutoff
    return chosen


def build_research_context(session: Session, game: Game, as_of: datetime) -> dict[str, Any]:
    from spe.identity.roster import eligible_players_for_game

    season_year = _season_year(game)
    # Canonical kickoff roster — AI may only cite these player_ids for this game.
    eligible = eligible_players_for_game(session, game, include_inactive_for_props=True)
    allowed_ids = {row.player_id for row in eligible}
    team_by_player = {row.player_id: row.team_id for row in eligible}
    injuries = [
        row
        for row in (
            session.query(InjuryEvent)
            .filter(InjuryEvent.game_id == game.game_id, InjuryEvent.reported_at <= as_of)
            .order_by(InjuryEvent.reported_at.desc())
            .limit(60)
            .all()
        )
        if row.player_id in allowed_ids
    ][:30]
    weather = (
        session.query(WeatherSnapshot)
        .filter(WeatherSnapshot.game_id == game.game_id, WeatherSnapshot.issued_at <= as_of)
        .order_by(WeatherSnapshot.issued_at.desc())
        .first()
    )
    week = game.week or 0
    team_rows = []
    player_rows = []
    if week > 0:
        team_rows = _team_tendency_rows(session, game, season_year, week)
        player_rows = [
            row
            for row in _player_workload_rows(session, game, season_year, week)
            if row.player_id in allowed_ids
        ]
    depth = [
        row
        for row in (
            session.query(DepthChartSnapshot)
            .filter(DepthChartSnapshot.game_id == game.game_id, DepthChartSnapshot.snapshot_at <= as_of)
            .limit(80)
            .all()
        )
        if row.player_id in allowed_ids
    ][:40]
    depth_weeks = [
        row for row in _depth_weeks(session, game, season_year, week) if row.get("player_id") in allowed_ids
    ]
    # League-wide headlines are not game-scoped identity sources — omit to prevent contamination.
    news: list[NewsArticle] = []
    prices = [row for row in _prices(session, game.game_id, as_of) if not row["player_id"] or row["player_id"] in allowed_ids]
    players = _players_from_roster(session, game, eligible)
    matchups = _matchups(game, team_rows)
    scoring = _scoring_expectations(game, team_rows)
    depth_changes = _depth_changes(depth_weeks)
    packet: dict[str, Any] = {
        "game": {
            "game_id": game.game_id,
            "league_id": game.league_id,
            "season_year": season_year,
            "week": game.week,
            "kickoff_utc": as_utc(game.kickoff_utc).isoformat() if as_utc(game.kickoff_utc) else None,
            "home": game.home_team.name,
            "away": game.away_team.name,
            "home_team_id": game.home_team_id,
            "away_team_id": game.away_team_id,
            "venue": game.venue.name,
            "roof": game.venue.roof_type,
            "surface": game.venue.surface,
            "data_tier": game.data_tier,
            "is_home_field": True,
        },
        "league_rules": {
            "league_id": game.league_id,
            "separate_from": "fbs" if game.league_id == "nfl" else "nfl",
            "note": "Do not transfer calibrated parameters or conclusions across leagues.",
            "prop_markets": list(PROP_FOCUS),
            "identity_rule": "Only cite player_ids listed in players[]. Never invent or import players from other games.",
        },
        "as_of": as_of.isoformat(),
        "players": players,
        "injuries": [
            {
                "ref": row.injury_event_id,
                "player_id": row.player_id,
                "team_id": team_by_player.get(row.player_id),
                "designation": row.designation,
                "practice_status": row.practice_status,
                "confidence": row.confidence,
                "reported_at": as_utc(row.reported_at).isoformat() if as_utc(row.reported_at) else None,
                "coverage_complete": row.coverage_complete,
            }
            for row in injuries
        ],
        "weather": None
        if weather is None
        else {
            "ref": weather.id,
            "summary": weather.summary,
            "temperature_c": weather.temperature_c,
            "wind_mps": weather.wind_mps,
            "precip_probability": weather.precip_probability,
            "roof_status": weather.roof_status,
            "quality_status": weather.quality_status,
            "issued_at": as_utc(weather.issued_at).isoformat() if as_utc(weather.issued_at) else None,
            "alerts": weather.alerts or [],
        },
        "home_field": {
            "home_team_id": game.home_team_id,
            "venue": game.venue.name,
            "roof": game.venue.roof_type,
            "surface": game.venue.surface,
            "note": "Home designation is verified from the schedule. Do not invent crowd or travel effects.",
        },
        "matchups": matchups,
        "scoring_expectations": scoring,
        "team_tendencies": [_metrics(row.id, row.team_id, row.week, row.metrics) for row in team_rows],
        "player_workload": [
            _metrics(row.id, row.player_id, row.week, row.metrics, team_id=row.team_id) for row in player_rows
        ],
        "depth_chart": [
            {
                "ref": row.id,
                "player_id": row.player_id,
                "position": row.position,
                "rank": row.rank,
                "snapshot_at": as_utc(row.snapshot_at).isoformat() if as_utc(row.snapshot_at) else None,
            }
            for row in depth
        ],
        "depth_chart_weeks": depth_weeks,
        "depth_chart_changes": depth_changes,
        "prices": prices,
        "news": [
            {
                "ref": row.article_id,
                "title": row.title,
                "seen_at": as_utc(row.seen_at).isoformat() if as_utc(row.seen_at) else None,
                "verified_injury": False,
            }
            for row in news
        ],
        "investigation_topics": [
            "player injuries and availability",
            "offensive and defensive matchups",
            "player workload and usage changes",
            "coaching tendencies",
            "team scoring expectations",
            "recent performance trends",
            "home-field advantages",
            "weather conditions",
            "player roles and depth-chart changes",
        ],
        "gaps": _gaps(injuries, weather, team_rows, player_rows, depth, depth_weeks, game.league_id),
    }
    packet["evidence"] = _evidence_index(packet)
    packet["evidence_catalog"] = [
        {"source": source, "ref": ref}
        for source, refs in packet["evidence"].items()
        for ref in refs
    ]
    # Schedule/venue facts may be cited via the game id under games.
    packet["evidence"]["home_field"] = [packet["game"]["game_id"]]
    packet["evidence_catalog"].append({"source": "home_field", "ref": packet["game"]["game_id"]})
    return packet


def build_explanation_context(
    session: Session,
    game: Game,
    *,
    limit: int = 12,
    focus_player_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return a bounded set of important props. Full boards overwhelm the model echo check."""
    from spe.identity.roster import eligible_players_for_game

    allowed = {row.player_id for row in eligible_players_for_game(session, game, include_inactive_for_props=True)}
    rows = (
        session.query(Forecast, Outcome, Recommendation)
        .join(Outcome, Outcome.outcome_id == Forecast.outcome_id)
        .outerjoin(Recommendation, Recommendation.forecast_id == Forecast.forecast_id)
        .filter(Outcome.game_id == game.game_id, Forecast.is_official.is_(True))
        .all()
    )
    latest: dict[str, tuple] = {}
    for forecast, outcome, rec in rows:
        if outcome.player_id and outcome.player_id not in allowed:
            continue
        if "IDENTITY_CONFLICT" in (forecast.reason_codes or []):
            continue
        current = latest.get(outcome.outcome_id)
        if current is None or as_utc(forecast.as_of) >= as_utc(current[0].as_of):
            latest[outcome.outcome_id] = (forecast, outcome, rec)
    player_ids = {outcome.player_id for forecast, outcome, rec in latest.values() if outcome.player_id}
    names = {
        row.player_id: row.name
        for row in session.query(Player).filter(Player.player_id.in_(player_ids or {""})).all()
    }
    packet = []
    for forecast, outcome, rec in latest.values():
        status = rec.status if rec else "no_recommendation"
        if forecast.data_tier == "demo" and status == "play":
            status = "demo_play"
        packet.append(
            {
                "forecast_id": forecast.forecast_id,
                "player_id": outcome.player_id,
                "player_name": names.get(outcome.player_id or "", outcome.player_id),
                "market_id": outcome.market_id,
                "side": outcome.side,
                "line": outcome.line,
                "pick": None,
                "p": forecast.p,
                "p_low": forecast.p_low,
                "p_high": forecast.p_high,
                "model_mean": (forecast.components or {}).get("mean"),
                "recommendation_status": status,
                "reason_codes": list(rec.reason_codes if rec else forecast.reason_codes or []),
                "data_tier": forecast.data_tier,
            }
        )
    from spe.models.prop_projection import annotate_prompt_forecasts

    annotate_prompt_forecasts(session, game.league_id, game.kickoff_utc, packet)
    focus = focus_player_ids or set()

    def sort_key(row: dict[str, Any]) -> tuple:
        focused = 0 if row.get("player_id") in focus else 1
        playish = 0 if row["recommendation_status"] in {"play", "demo_play"} else 1
        extremity = -abs(float(row["p"]) - 0.5)
        return (focused, playish, extremity)

    packet.sort(key=sort_key)
    return packet[:limit]


def fingerprint(packet: dict[str, Any]) -> str:
    """Hash of material context used to skip re-analysis when nothing changed."""
    body = {
        "injuries": [(row["ref"], row["designation"], row.get("reported_at")) for row in packet["injuries"]],
        "weather": None
        if not packet["weather"]
        else (packet["weather"]["ref"], packet["weather"]["summary"], packet["weather"].get("wind_mps")),
        "prices": [(row["ref"], row["price_american"]) for row in packet["prices"]],
        "depth": [(row.get("player_id"), row.get("rank"), row.get("depth_rank")) for row in packet.get("depth_chart_weeks", [])[:20]],
        "depth_changes": packet.get("depth_chart_changes", []),
        "news": [row["ref"] for row in packet.get("news", [])],
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def packet_hash(kind: str, version: str, model: str, payload: Any) -> str:
    raw = json.dumps({"kind": kind, "version": version, "model": model, "payload": payload}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _season_year(game: Game) -> int:
    season = getattr(game, "season", None)
    if season is not None and getattr(season, "year", None):
        return int(season.year)
    kick = as_utc(game.kickoff_utc)
    if kick is None:
        return datetime.now(UTC).year
    # NFL/FBS seasons span calendar years; fall kickoffs belong to that season year.
    return kick.year if kick.month >= 8 else kick.year - 1


def _prices(session: Session, game_id: str, as_of: datetime) -> list[dict[str, Any]]:
    ticks = (
        session.query(OddsTick, Outcome)
        .join(Outcome, Outcome.outcome_id == OddsTick.outcome_id)
        .filter(Outcome.game_id == game_id, OddsTick.captured_at <= as_of)
        .order_by(OddsTick.captured_at.desc())
        .limit(40)
        .all()
    )
    seen: set[str] = set()
    chosen: list[tuple] = []
    for tick, outcome in ticks:
        if outcome.outcome_id in seen:
            continue
        seen.add(outcome.outcome_id)
        chosen.append((tick, outcome))
        if len(chosen) == 20:
            break
    from spe.models.prop_projection import pick_sentence

    player_ids = {outcome.player_id for _tick, outcome in chosen if outcome.player_id}
    names = {
        row.player_id: row.name
        for row in session.query(Player).filter(Player.player_id.in_(player_ids or {""})).all()
    }
    rows = []
    for tick, outcome in chosen:
        rows.append(
            {
                "ref": tick.tick_id,
                "outcome_id": outcome.outcome_id,
                "player_id": outcome.player_id,
                "player_name": names.get(outcome.player_id or ""),
                "market_id": outcome.market_id,
                "side": outcome.side,
                "price_american": tick.price_american,
                "line": outcome.line,
                "pick": pick_sentence(names.get(outcome.player_id or ""), outcome.side, outcome.line, outcome.market_id),
                "captured_at": as_utc(tick.captured_at).isoformat() if as_utc(tick.captured_at) else None,
            }
        )
    return rows


def _players_from_roster(session, game, eligible) -> list[dict[str, Any]]:
    """Only kickoff-roster players enter the AI packet. Team IDs come from eligibility."""
    allowed_teams = {game.home_team_id, game.away_team_id}
    # Cap packet size but keep skill-position bias for prop research.
    skill = {"QB", "RB", "WR", "TE"}
    skill_rows = [row for row in eligible if row.position_group in skill]
    other_rows = [row for row in eligible if row.position_group not in skill]
    chosen = (skill_rows + other_rows)[:80]
    out = []
    for row in chosen:
        player = session.get(Player, row.player_id)
        if not player:
            continue
        team_ok = row.team_id in allowed_teams
        if not team_ok:
            continue
        out.append(
            {
                "player_id": row.player_id,
                "name": player.name,
                "position_group": player.position_group,
                "position": player.position,
                "team_id": row.team_id,
                "on_game_roster": True,
                "eligibility": getattr(row, "eligibility", "eligible"),
            }
        )
    return out


def _players(session, game, injuries, analytics, depth, depth_weeks, prices) -> list[dict[str, str]]:
    # Legacy helper kept for tests; prefer _players_from_roster.
    from spe.identity.roster import eligible_players_for_game

    return _players_from_roster(session, game, eligible_players_for_game(session, game, include_inactive_for_props=True))


def _metrics(ref: str, entity_id: str, week: int, metrics: dict, team_id: str | None = None) -> dict[str, Any]:
    kept = {key: metrics.get(key) for key in METRIC_KEYS if metrics.get(key) is not None}
    row: dict[str, Any] = {"ref": ref, "entity_id": entity_id, "week": week, "metrics": kept}
    if team_id:
        row["team_id"] = team_id
    return row


def _latest_team_metrics(team_rows: list[TeamAnalytics], team_id: str) -> dict[str, Any]:
    for row in team_rows:
        if row.team_id == team_id:
            kept = {key: row.metrics.get(key) for key in TEAM_MATCHUP_KEYS if row.metrics.get(key) is not None}
            return {"ref": row.id, "team_id": team_id, "week": row.week, "metrics": kept}
    return {"team_id": team_id, "metrics": {}, "missing": True}


def _matchups(game: Game, team_rows: list[TeamAnalytics]) -> dict[str, Any]:
    home = _latest_team_metrics(team_rows, game.home_team_id)
    away = _latest_team_metrics(team_rows, game.away_team_id)
    return {
        "home_offense_vs_away_defense": {
            "offense": {
                "team_id": game.home_team_id,
                "metrics": _pick(home["metrics"], ("off_epa_per_play", "pace_sec_per_play", "success_rate", "off_success_rate")),
            },
            "defense": {"team_id": game.away_team_id, "metrics": _pick(away["metrics"], ("def_epa_per_play", "def_success_rate"))},
            "sources": [ref for ref in (home.get("ref"), away.get("ref")) if ref],
        },
        "away_offense_vs_home_defense": {
            "offense": {
                "team_id": game.away_team_id,
                "metrics": _pick(away["metrics"], ("off_epa_per_play", "pace_sec_per_play", "success_rate", "off_success_rate")),
            },
            "defense": {"team_id": game.home_team_id, "metrics": _pick(home["metrics"], ("def_epa_per_play", "def_success_rate"))},
            "sources": [ref for ref in (away.get("ref"), home.get("ref")) if ref],
        },
        "note": "Matchup rows use prior-week verified team analytics only. Missing keys stay missing.",
    }


def _scoring_expectations(game: Game, team_rows: list[TeamAnalytics]) -> dict[str, Any]:
    home = _latest_team_metrics(team_rows, game.home_team_id)
    away = _latest_team_metrics(team_rows, game.away_team_id)
    return {
        "home": {
            "team_id": game.home_team_id,
            "points_per_scoring_opp": home["metrics"].get("points_per_scoring_opp"),
            "red_zone_td_rate": home["metrics"].get("red_zone_td_rate"),
            "off_epa_per_play": home["metrics"].get("off_epa_per_play"),
            "ref": home.get("ref"),
        },
        "away": {
            "team_id": game.away_team_id,
            "points_per_scoring_opp": away["metrics"].get("points_per_scoring_opp"),
            "red_zone_td_rate": away["metrics"].get("red_zone_td_rate"),
            "off_epa_per_play": away["metrics"].get("off_epa_per_play"),
            "ref": away.get("ref"),
        },
        "note": "These are prior scoring-rate features, not predicted totals or probabilities.",
    }


def _team_tendency_rows(session: Session, game: Game, season_year: int, week: int) -> list[TeamAnalytics]:
    """Same-season verified team metrics only. Prefer week grain, then game grain."""
    for grain in ("week", "game"):
        rows = (
            session.query(TeamAnalytics)
            .filter(
                TeamAnalytics.team_id.in_([game.home_team_id, game.away_team_id]),
                TeamAnalytics.league_id == game.league_id,
                TeamAnalytics.grain == grain,
                TeamAnalytics.week > 0,
                TeamAnalytics.week < week,
                TeamAnalytics.season_year == season_year,
            )
            .order_by(TeamAnalytics.week.desc())
            .limit(8)
            .all()
        )
        if rows:
            return rows
    return []


def _player_workload_rows(session: Session, game: Game, season_year: int, week: int) -> list[PlayerAnalytics]:
    """Prefer week grain; fall back to game grain and usage_pregame when week rows are absent."""
    for grain in ("week", "game", "usage_pregame"):
        rows = (
            session.query(PlayerAnalytics)
            .filter(
                PlayerAnalytics.team_id.in_([game.home_team_id, game.away_team_id]),
                PlayerAnalytics.league_id == game.league_id,
                PlayerAnalytics.grain == grain,
                PlayerAnalytics.week > 0,
                PlayerAnalytics.week < week,
                PlayerAnalytics.season_year == season_year,
            )
            .order_by(PlayerAnalytics.week.desc())
            .limit(24)
            .all()
        )
        if rows:
            return rows
    return []


def _depth_weeks(session: Session, game: Game, season_year: int, week: int) -> list[dict[str, Any]]:
    if week <= 0:
        return []
    rows = (
        session.query(DepthChartWeek)
        .filter(
            DepthChartWeek.team_id.in_([game.home_team_id, game.away_team_id]),
            DepthChartWeek.season_year == season_year,
            DepthChartWeek.week.in_([week - 1, week] if week > 1 else [week]),
        )
        .order_by(DepthChartWeek.week.desc(), DepthChartWeek.depth_rank.asc())
        .limit(60)
        .all()
    )
    return [
        {
            "ref": row.id,
            "player_id": row.player_id,
            "team_id": row.team_id,
            "position": row.position,
            "depth_position": row.depth_position,
            "depth_rank": row.depth_rank,
            "week": row.week,
            "formation": row.formation,
        }
        for row in rows
    ]


def _depth_changes(depth_weeks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_player: dict[str, list[dict[str, Any]]] = {}
    for row in depth_weeks:
        by_player.setdefault(row["player_id"], []).append(row)
    changes = []
    for player_id, rows in by_player.items():
        weeks = sorted({row["week"] for row in rows})
        if len(weeks) < 2:
            continue
        earlier = next(row for row in rows if row["week"] == weeks[0])
        later = next(row for row in rows if row["week"] == weeks[-1])
        if earlier.get("depth_rank") == later.get("depth_rank") and earlier.get("position") == later.get("position"):
            continue
        changes.append(
            {
                "player_id": player_id,
                "team_id": later.get("team_id"),
                "from_week": earlier["week"],
                "to_week": later["week"],
                "from_rank": earlier.get("depth_rank"),
                "to_rank": later.get("depth_rank"),
                "from_position": earlier.get("position"),
                "to_position": later.get("position"),
                "refs": [earlier["ref"], later["ref"]],
            }
        )
    return changes[:20]


def _pick(metrics: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: metrics[key] for key in keys if key in metrics}


def _gaps(injuries, weather, teams, players, depth, depth_weeks, league_id: str) -> list[str]:
    gaps = []
    if not injuries:
        gaps.append("injuries")
    if weather is None:
        gaps.append("weather")
    if not teams:
        gaps.append("team_tendencies")
    if not players:
        gaps.append("player_workload")
    if not depth and not depth_weeks:
        gaps.append("depth_chart")
    gaps.append("coaching_staff")
    if league_id == "fbs":
        gaps.append("fbs_injury_coverage_often_incomplete")
    return gaps


def _evidence_index(packet: dict[str, Any]) -> dict[str, list[str]]:
    matchup_refs = []
    for side in packet["matchups"].values():
        if isinstance(side, dict):
            matchup_refs.extend(side.get("sources") or [])
    scoring_refs = [
        packet["scoring_expectations"]["home"].get("ref"),
        packet["scoring_expectations"]["away"].get("ref"),
    ]
    depth_change_refs = []
    for row in packet.get("depth_chart_changes", []):
        depth_change_refs.extend(row.get("refs") or [])
    return {
        "games": [packet["game"]["game_id"]],
        "injury_events": [row["ref"] for row in packet["injuries"]],
        "weather_snapshots": [] if not packet["weather"] else [packet["weather"]["ref"]],
        "team_analytics": [row["ref"] for row in packet["team_tendencies"]],
        "player_analytics": [row["ref"] for row in packet["player_workload"]],
        "depth_charts": [row["ref"] for row in packet["depth_chart"]],
        "depth_chart_weeks": [row["ref"] for row in packet.get("depth_chart_weeks", [])],
        "depth_chart_changes": depth_change_refs,
        "matchups": [ref for ref in matchup_refs if ref],
        "scoring_expectations": [ref for ref in scoring_refs if ref],
        "odds_ticks": [row["ref"] for row in packet["prices"]],
        "news_articles": [row["ref"] for row in packet["news"]],
    }
