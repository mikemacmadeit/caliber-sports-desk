"""Point-in-time pregame features for real upcoming games.

Uses only games with kickoff strictly before each target's as_of. Does not
rewrite historical feature rows for finished games; replaces ``pregame_v1``
rows for the requested upcoming game ids only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.analytics.definitions import FEATURE_VERSION, SHRINKAGE_K
from spe.analytics.engine import add_usage_trends, player_game_metrics
from spe.analytics.persist import _clean, _store_features
from spe.analytics.snapshots import build_pregame_snapshots, player_features_for_upcoming
from spe.db.base import as_utc
from spe.db.models import FeatureRow, Game, InjuryEvent, Player, WeatherSnapshot
from spe.identity.games import ALIAS_TIER, resolve_canonical_game_id
from spe.identity.roster import eligible_players_for_game, season_year_for_game
from spe.ingest.features import build_player_features, feature_coverage
from spe.ingest.store import upsert_coverage


def generate_upcoming_features(
    session: Session,
    *,
    league_id: str | None = None,
    game_ids: list[str] | None = None,
    horizon_hours: int = 24 * 14,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    targets = _target_games(session, league_id=league_id, game_ids=game_ids, horizon_hours=horizon_hours, now=now)
    if not targets:
        return {"status": "no_upcoming_games", "games": 0, "team_rows": 0, "player_rows": 0}

    # Canonicalize ids so we never write features onto alias duplicates.
    canonical_targets: list[Game] = []
    seen: set[str] = set()
    for game in targets:
        cid = resolve_canonical_game_id(session, game.game_id)
        if cid in seen:
            continue
        row = session.get(Game, cid) or game
        if row.data_tier == ALIAS_TIER:
            continue
        seen.add(row.game_id)
        canonical_targets.append(row)

    by_league: dict[str, list[Game]] = {}
    for game in canonical_targets:
        by_league.setdefault(game.league_id, []).append(game)

    team_total = player_total = 0
    details: list[dict[str, Any]] = []
    for lg, lg_games in by_league.items():
        result = _generate_league(session, lg, lg_games, now=now)
        team_total += result["team_rows"]
        player_total += result["player_rows"]
        details.append(result)

    upsert_coverage(
        session,
        provider="caliber_analytics_v1",
        dataset="upcoming_pregame_features",
        league_id=league_id or "all",
        season_year=now.year,
        row_count=team_total + player_total,
        status="available" if team_total or player_total else "empty",
        notes=f"{FEATURE_VERSION} upcoming-only. Prior kickoffs only.",
    )
    return {
        "status": "ok",
        "games": len(canonical_targets),
        "team_rows": team_total,
        "player_rows": player_total,
        "feature_version": FEATURE_VERSION,
        "leagues": details,
        "game_ids": [g.game_id for g in canonical_targets],
    }


def _target_games(
    session: Session,
    *,
    league_id: str | None,
    game_ids: list[str] | None,
    horizon_hours: int,
    now: datetime,
) -> list[Game]:
    if game_ids:
        rows = session.query(Game).filter(Game.game_id.in_(game_ids)).all()
        return [g for g in rows if g.data_tier not in {"demo", ALIAS_TIER}]
    q = session.query(Game).filter(
        Game.data_tier == "upcoming",
        Game.status.in_(("scheduled", "preview", "pregame")),
        Game.kickoff_utc >= now - timedelta(hours=3),
        Game.kickoff_utc <= now + timedelta(hours=horizon_hours),
    )
    if league_id:
        q = q.filter(Game.league_id == league_id)
    return list(q.order_by(Game.kickoff_utc).all())


def _generate_league(session: Session, league_id: str, targets: list[Game], *, now: datetime) -> dict[str, Any]:
    # Skip full PBP load here — box-score history is enough for upcoming usage
    # features and keeps the repair job within memory limits. Opponent EPA from
    # plays remains available via historical analytics tables when present.
    hist_games, _plays, stats, players = _load_history_frames(session, league_id, include_plays=False)
    if hist_games.empty and stats.empty:
        thin = _thin_eligibility_features(session, targets, now=now)
        return {"league_id": league_id, "team_rows": thin["team_rows"], "player_rows": thin["player_rows"], "mode": "thin"}

    target_df = pd.DataFrame(
        [
            {
                "game_id": g.game_id,
                "home_team_id": g.home_team_id,
                "away_team_id": g.away_team_id,
                "kickoff_utc": g.kickoff_utc,
                "week": g.week or 0,
                "season_year": season_year_for_game(session, g) or 0,
                "league_id": g.league_id,
                "neutral_site": g.neutral_site,
            }
            for g in targets
        ]
    )
    games = pd.concat([hist_games, target_df], ignore_index=True) if not hist_games.empty else target_df
    plays_x = pd.DataFrame()
    team_games = pd.DataFrame()

    work_stats = stats.copy()
    if not work_stats.empty:
        work_stats = player_game_metrics(work_stats, plays=None, players=players)
        work_stats = add_usage_trends(work_stats)

    trows, _historical_prows = build_pregame_snapshots(
        games=games,
        plays=plays_x,
        player_stats=work_stats,
        players=players,
        league_id=league_id,
    )
    target_ids = {g.game_id for g in targets}
    trows = [r for r in trows if r["game_id"] in target_ids]

    blobs_by_game: dict[str, dict[str, Any]] = {}
    for game in targets:
        as_of = _kickoff_utc(game) - timedelta(seconds=1)
        team_row = next((r for r in trows if r["game_id"] == game.game_id and r.get("player_id") is None), None)
        blob = (team_row or {}).get("feature_blob") or {}
        blobs_by_game[game.game_id] = {
            "home_id": game.home_team_id,
            "away_id": game.away_team_id,
            "home": blob.get("home") or {"team_id": game.home_team_id},
            "away": blob.get("away") or {"team_id": game.away_team_id},
            "as_of": as_of,
        }

    eligible_by_game: dict[str, list[tuple[str, str]]] = {}
    for game in targets:
        eligible_by_game[game.game_id] = [
            (p.player_id, p.team_id)
            for p in eligible_players_for_game(session, game)
            if p.position_group in {"QB", "RB", "WR", "TE"}
        ]

    from spe.analytics.snapshots import _league_rate

    stats_for_rate = work_stats
    if not work_stats.empty and "rush_td" in work_stats.columns and "rec_td" in work_stats.columns:
        stats_for_rate = work_stats.copy()
        stats_for_rate["tds"] = stats_for_rate["rush_td"].fillna(0) + stats_for_rate["rec_td"].fillna(0)
    league_rz = _league_rate(stats_for_rate, "rz_targets", "tds") if not stats_for_rate.empty else 0.2
    prows = player_features_for_upcoming(
        stats=work_stats,
        blobs_by_game=blobs_by_game,
        eligible_by_game=eligible_by_game,
        rz_rate=league_rz,
        gl_rate=0.45,
        league_id=league_id,
        k=SHRINKAGE_K.get(league_id, 10.0),
    )
    prows = _enrich_with_live_context(session, targets, prows, now=now)

    _delete_upcoming_features(session, list(target_ids))
    team_n = _store_features(session, [_clean_row(r) for r in trows])
    player_n = _store_features(session, [_clean_row(r) for r in prows])
    return {
        "league_id": league_id,
        "team_rows": team_n,
        "player_rows": player_n,
        "mode": "full",
        "prior_games": int(len(hist_games)),
        "target_games": len(targets),
    }


def _load_history_frames(
    session: Session,
    league_id: str,
    *,
    include_plays: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Historical (and finished) games only — these are valid priors."""
    games = (
        session.query(Game)
        .filter(Game.league_id == league_id, Game.data_tier == "historical", Game.status == "final")
        .all()
    )
    gdf = pd.DataFrame(
        [
            {
                "game_id": g.game_id,
                "home_team_id": g.home_team_id,
                "away_team_id": g.away_team_id,
                "kickoff_utc": g.kickoff_utc,
                "week": g.week or 0,
                "season_year": int(g.season_id.rsplit("-", 1)[-1]) if g.season_id and "-" in g.season_id else 0,
                "league_id": g.league_id,
                "neutral_site": g.neutral_site,
            }
            for g in games
        ]
    )
    if gdf.empty:
        return gdf, pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    conn = session.connection()
    pdf = pd.DataFrame()
    if include_plays:
        pdf = pd.read_sql(
            text(
                """
                SELECT p.game_id, p.play_id, p.game_id AS provider_game_id,
                       p.offense_id, p.defense_id, p.offense_id AS offense_abbr, p.defense_id AS defense_abbr,
                       p.down, p.distance, p.yardline, p.play_type, p.yards,
                       COALESCE(p.epa, p.ppa) AS epa, p.ppa, p.wp, p.success,
                       p.is_pass, p.is_rush, p.is_td, p.is_turnover, p.is_red_zone, p.is_goal_line,
                       p.is_scrimmage, p.first_down, p.game_seconds_remaining, p.period,
                       p.score_before_off AS score_off, p.score_before_def AS score_def,
                       COALESCE(p.drive_id, '') AS drive_id,
                       p.passer_player_id, p.rusher_player_id, p.receiver_player_id,
                       p.is_pass AS pass_attempt, '' AS drive_result
                FROM plays p
                JOIN games g ON g.game_id = p.game_id
                WHERE g.league_id = :lg AND g.data_tier = 'historical' AND g.status = 'final'
                """
            ),
            conn,
            params={"lg": league_id},
        )
        if not pdf.empty:
            pdf["complete_pass"] = (
                pdf["is_pass"].astype(bool) & pdf["receiver_player_id"].notna() & (pdf["yards"].fillna(0) > 0)
            )
            for col in (
                "is_pass",
                "is_rush",
                "is_td",
                "is_turnover",
                "is_red_zone",
                "is_goal_line",
                "is_scrimmage",
                "first_down",
                "success",
            ):
                if col in pdf.columns:
                    pdf[col] = pdf[col].fillna(0).astype(bool)

    sdf = pd.read_sql(
        text(
            """
            SELECT s.game_id, s.player_id, s.attempts, s.targets, s.receptions, s.rec_yards, s.rush_yards,
                   s.carries, s.rush_td, s.rec_td, s.pass_td, s.rz_targets, s.rz_carries, s.gl_carries, s.team_id
            FROM player_game_stats s
            JOIN games g ON g.game_id = s.game_id
            WHERE g.league_id = :lg AND g.data_tier = 'historical' AND g.status = 'final'
            """
        ),
        conn,
        params={"lg": league_id},
    )
    if not sdf.empty and not gdf.empty:
        sdf = sdf.merge(
            gdf[["game_id", "kickoff_utc", "week", "season_year", "home_team_id", "away_team_id", "league_id"]],
            on="game_id",
            how="left",
        )

    players = pd.DataFrame(
        [
            {"player_id": p.player_id, "position_group": p.position_group, "name": p.name}
            for p in session.query(Player).filter(Player.player_id.in_(set(sdf["player_id"]) if not sdf.empty else {""})).all()
        ]
    )
    if not sdf.empty and not players.empty:
        sdf = sdf.merge(players[["player_id", "position_group"]], on="player_id", how="left")
    return gdf, pdf, sdf, players


def _thin_eligibility_features(session: Session, targets: list[Game], *, now: datetime) -> dict[str, int]:
    rows: list[dict[str, Any]] = []
    for game in targets:
        as_of = _kickoff_utc(game) - timedelta(seconds=1)
        team_blob = {
            "available_as_of": as_of.isoformat(),
            "sample_size": 0,
            "missingness": 1.0,
            "source": "eligibility_thin",
            "missing_flags": {"no_prior_plays": True},
            "home": {"team_id": game.home_team_id},
            "away": {"team_id": game.away_team_id},
        }
        rows.append(
            {
                "game_id": game.game_id,
                "player_id": None,
                "feature_version": FEATURE_VERSION,
                "as_of": as_of,
                "feature_blob": team_blob,
                "coverage_mask": {"no_prior_plays": True},
                "sample_size": 0,
                "missingness": 1.0,
                "source": "team_pregame_thin",
            }
        )
        for player in eligible_players_for_game(session, game):
            if player.position_group not in {"QB", "RB", "WR", "TE"}:
                continue
            blob = build_player_features(
                as_of=as_of,
                sample_size=0,
                source="eligibility_thin",
                usage={},
                opponent={},
                availability={"report_missing": True, "prob": 0.5},
                weather=_weather_blob(session, game),
                market_anchor=None,
                missing_flags={"no_prior_player_games": True},
            )
            rows.append(
                {
                    "game_id": game.game_id,
                    "player_id": player.player_id,
                    "feature_version": FEATURE_VERSION,
                    "as_of": as_of,
                    "feature_blob": blob,
                    "coverage_mask": feature_coverage(blob),
                    "sample_size": 0,
                    "missingness": float(blob["missingness"]),
                    "source": "player_pregame_thin",
                }
            )
    _delete_upcoming_features(session, [g.game_id for g in targets])
    n = _store_features(session, [_clean_row(r) for r in rows])
    team_n = sum(1 for r in rows if r["player_id"] is None)
    return {"team_rows": team_n, "player_rows": n - team_n}


def _enrich_with_live_context(
    session: Session,
    targets: list[Game],
    prows: list[dict[str, Any]],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    injuries: dict[tuple[str, str], InjuryEvent] = {}
    for game in targets:
        for row in (
            session.query(InjuryEvent)
            .filter(InjuryEvent.game_id == game.game_id)
            .order_by(InjuryEvent.reported_at.desc())
            .all()
        ):
            injuries.setdefault((game.game_id, row.player_id), row)
    weather = {g.game_id: _weather_blob(session, g) for g in targets}
    eligible = {g.game_id: {p.player_id: p for p in eligible_players_for_game(session, g)} for g in targets}

    out: list[dict[str, Any]] = []
    for row in prows:
        blob = dict(row.get("feature_blob") or {})
        gid = row["game_id"]
        pid = row.get("player_id")
        if pid:
            inj = injuries.get((gid, pid))
            ep = eligible.get(gid, {}).get(pid)
            blob["availability"] = {
                "designation": inj.designation if inj else None,
                "report_missing": inj is None,
                "coverage_complete": bool(inj.coverage_complete) if inj else False,
                "eligible": ep is not None,
                "roster_status": ep.status if ep else None,
                "prob": 0.35 if inj and "out" in (inj.designation or "").lower() else (0.9 if ep else 0.5),
            }
            blob["weather"] = weather.get(gid)
            if ep:
                blob.setdefault("usage", {})
                blob["team_id"] = ep.team_id
        row = dict(row)
        row["feature_blob"] = blob
        out.append(row)
    return out


def _weather_blob(session: Session, game: Game) -> dict[str, Any] | None:
    row = (
        session.query(WeatherSnapshot)
        .filter(WeatherSnapshot.game_id == game.game_id)
        .order_by(WeatherSnapshot.issued_at.desc())
        .first()
    )
    if row is None:
        return None
    return {
        "temperature_c": row.temperature_c,
        "wind_mps": row.wind_mps,
        "summary": row.summary,
        "roof_status": row.roof_status,
        "precip_probability": row.precip_probability,
        "provider": row.provider,
        "issued_at": row.issued_at.isoformat() if row.issued_at else None,
    }


def _delete_upcoming_features(session: Session, game_ids: list[str]) -> None:
    if not game_ids:
        return
    session.query(FeatureRow).filter(
        FeatureRow.game_id.in_(game_ids),
        FeatureRow.feature_version == FEATURE_VERSION,
    ).delete(synchronize_session=False)
    session.flush()


def _clean_row(row: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(row)
    cleaned["feature_blob"] = _clean(row.get("feature_blob") or {})
    cleaned["coverage_mask"] = _clean(row.get("coverage_mask") or {})
    return cleaned


def _kickoff_utc(game: Game) -> datetime:
    kick = as_utc(game.kickoff_utc) or game.kickoff_utc
    if kick.tzinfo is None:
        return kick.replace(tzinfo=UTC)
    return kick
