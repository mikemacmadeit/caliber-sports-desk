"""API-Sports american-football injuries for FBS (and optional NFL fill-in).

Missing or plan-blocked responses are never treated as healthy players.
A successful empty team report can mark coverage_complete for that club's
upcoming game only when the provider returns an explicit injury list payload.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from spe.adapters.api_sports import ApiSportsAdapter
from spe.adapters.base import AdapterError
from spe.db.base import as_utc, new_id
from spe.db.models import Game, InjuryEvent, Player, Roster, Team
from spe.identity.match import match_name
from spe.ingest.snapshots import append_raw
from spe.ingest.store import finish_run, start_run, upsert_coverage
from spe.ingest.values import as_str

PROVIDER = "api_sports"
NCAA_LEAGUE_ID = 2
NFL_LEAGUE_ID = 1


def ingest_api_sports_injuries(
    session: Session,
    *,
    adapter: ApiSportsAdapter | None = None,
    league_id: str = "fbs",
    season: int | None = None,
    now: datetime | None = None,
    horizon_hours: int = 192,
    fixture_injuries: dict[str, list[dict[str, Any]]] | None = None,
    fixture_team_map: dict[str, str] | None = None,
    max_teams: int | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    season = int(season or now.year)
    adapter = adapter or ApiSportsAdapter()
    summary: dict[str, Any] = {
        "status": "ok",
        "league_id": league_id,
        "season": season,
        "teams_attempted": 0,
        "teams_with_payload": 0,
        "injuries_upserted": 0,
        "players_matched": 0,
        "players_unmatched": 0,
        "plan_blocked": False,
        "coverage_complete_games": 0,
        "notes": [],
        "play_enabled": False,
    }
    if not adapter.api_key and fixture_injuries is None:
        summary["status"] = "needs_key"
        summary["notes"].append("API_SPORTS_KEY is not configured")
        upsert_coverage(
            session,
            provider=PROVIDER,
            dataset="injuries",
            league_id=league_id,
            season_year=season,
            row_count=0,
            status="needs_key",
            notes="Set API_SPORTS_KEY to ingest FBS injury reports.",
        )
        return summary

    # Do not thrash a plan-blocked endpoint. Cooldown until coverage notes age out.
    if fixture_injuries is None and _plan_blocked_cooldown_active(session, league_id, season, now):
        summary["status"] = "plan_blocked"
        summary["plan_blocked"] = True
        summary["skipped_known_blocked"] = True
        summary["notes"].append(
            "Skipped API-Sports injury fetch: recent plan_blocked coverage still active. "
            "Availability remains unknown — never treated as healthy."
        )
        return summary

    run = start_run(session, PROVIDER, "injuries", league_id, season)
    api_league = NCAA_LEAGUE_ID if league_id == "fbs" else NFL_LEAGUE_ID
    if fixture_team_map is not None:
        team_map = {str(k): str(v) for k, v in fixture_team_map.items()}
    elif fixture_injuries is not None:
        team_map = {str(k): str(k) for k in fixture_injuries}
    else:
        try:
            team_map = _map_api_teams(session, adapter, api_league, season)
        except AdapterError as exc:
            text = str(exc)
            if "Free plans do not have access" in text or "plan" in text.lower():
                summary["plan_blocked"] = True
                summary["status"] = "plan_blocked"
                summary["notes"].append(f"team map blocked: {text}")
                finish_run(session, run, status="plan_blocked", upserted=0, skipped=0)
                upsert_coverage(
                    session,
                    provider=PROVIDER,
                    dataset="injuries",
                    league_id=league_id,
                    season_year=season,
                    row_count=0,
                    status="plan_blocked",
                    notes=(
                        "API-Sports Free plan cannot read current-season NCAA injuries/teams. "
                        "Missing reports are never treated as healthy. Upgrade required for live FBS coverage."
                    ),
                )
                session.flush()
                return summary
            raise
    summary["teams_mapped"] = len(team_map)

    upcoming = _upcoming_games(session, league_id, now, horizon_hours)
    needed_team_ids = {game.home_team_id for game in upcoming} | {game.away_team_id for game in upcoming}
    targets = [(api_id, our_id) for api_id, our_id in team_map.items() if our_id in needed_team_ids]
    if not targets and fixture_injuries is not None:
        targets = [(api_id, our_id) for api_id, our_id in team_map.items() if str(api_id) in fixture_injuries]
    if max_teams is not None:
        targets = targets[: max(0, int(max_teams))]

    catalogs = _roster_catalogs(session, {our_id for _, our_id in targets})
    game_by_team = _game_for_team(upcoming)
    seen = {
        rec_id
        for (rec_id,) in session.query(InjuryEvent.provider_record_id).filter(InjuryEvent.provider == PROVIDER)
    }

    for api_team_id, our_team_id in targets:
        summary["teams_attempted"] += 1
        try:
            rows, meta = _fetch_team_injuries(
                adapter,
                api_team_id=api_team_id,
                season=season,
                fixture=(fixture_injuries or {}).get(str(api_team_id)),
            )
        except AdapterError as exc:
            text = str(exc)
            if "Free plans do not have access" in text or "plan" in text.lower():
                summary["plan_blocked"] = True
                summary["status"] = "plan_blocked"
                summary["notes"].append(text)
                break
            summary["notes"].append(f"team {api_team_id}: {text}")
            continue

        append_raw(
            session,
            provider=PROVIDER,
            endpoint=str(meta.get("endpoint") or "/injuries"),
            provider_record_id=f"injuries:{api_league}:{season}:{api_team_id}",
            payload={"team_id": api_team_id, "n_records": len(rows), "meta": meta},
            observed_at=now,
            credit_cost=0,
        )
        summary["teams_with_payload"] += 1
        game = game_by_team.get(our_team_id)
        catalog = catalogs.get(our_team_id, [])
        matched_here = 0
        for row in rows:
            player_name = _player_name(row)
            designation = _designation(row)
            reported = _reported_at(row, now)
            if not player_name or not designation:
                continue
            decision = match_name(player_name, catalog) if catalog else None
            if decision is None or decision.review_status != "auto" or not decision.canonical_id:
                summary["players_unmatched"] += 1
                continue
            player_id = decision.canonical_id
            if session.get(Player, player_id) is None:
                summary["players_unmatched"] += 1
                continue
            rec_id = f"apisports:{league_id}:{season}:{api_team_id}:{player_id}:{designation}:{reported.date().isoformat()}"
            if rec_id[:160] in seen:
                continue
            seen.add(rec_id[:160])
            session.add(
                InjuryEvent(
                    injury_event_id=new_id(),
                    game_id=game.game_id if game else None,
                    player_id=player_id,
                    designation=designation[:32],
                    practice_status=as_str((row.get("player") or {}).get("status")) or None,
                    reported_at=reported,
                    source="api_sports.injuries",
                    confidence=0.65 if _has_timestamp(row) else 0.45,
                    coverage_complete=True,
                    provider=PROVIDER,
                    provider_record_id=rec_id[:160],
                    observed_at=now,
                    effective_at=reported,
                    quality_status="ok" if _has_timestamp(row) else "partial",
                )
            )
            summary["injuries_upserted"] += 1
            summary["players_matched"] += 1
            matched_here += 1

        # Explicit empty injury list still counts as a received report for that club's
        # upcoming game. Plan-blocked / failed fetches never do.
        if game is not None and meta.get("explicit_list") and (matched_here > 0 or len(rows) == 0):
            summary["coverage_complete_games"] += 1
            if matched_here == 0 and len(rows) == 0:
                marker = f"apisports:{league_id}:{season}:{api_team_id}:coverage:{game.game_id}"
                if marker[:160] not in seen:
                    anchor = _ensure_coverage_marker_player(session, league_id)
                    seen.add(marker[:160])
                    session.add(
                        InjuryEvent(
                            injury_event_id=new_id(),
                            game_id=game.game_id,
                            player_id=anchor,
                            designation="report_clear",
                            practice_status=None,
                            reported_at=now,
                            source="api_sports.injuries.coverage",
                            confidence=0.55,
                            coverage_complete=True,
                            provider=PROVIDER,
                            provider_record_id=marker[:160],
                            observed_at=now,
                            effective_at=now,
                            quality_status="ok",
                        )
                    )

    finish_run(
        session,
        run,
        status="ok" if summary["status"] in {"ok", "plan_blocked"} else summary["status"],
        upserted=int(summary["injuries_upserted"]),
        skipped=int(summary["players_unmatched"]),
    )
    upsert_coverage(
        session,
        provider=PROVIDER,
        dataset="injuries",
        league_id=league_id,
        season_year=season,
        row_count=int(summary["injuries_upserted"]),
        status=(
            "plan_blocked"
            if summary["plan_blocked"]
            else ("available" if summary["injuries_upserted"] or summary["teams_with_payload"] else "empty")
        ),
        notes=(
            "Missing reports are never healthy. "
            + ("Free plan blocked current-season injuries. " if summary["plan_blocked"] else "")
            + f"teams_attempted={summary['teams_attempted']} matched={summary['players_matched']}."
        ),
    )
    session.flush()
    return summary


def _plan_blocked_cooldown_active(
    session: Session,
    league_id: str,
    season: int,
    now: datetime,
    *,
    cooldown_hours: int = 24,
) -> bool:
    """True when a recent plan_blocked coverage row says the endpoint is unavailable."""
    from spe.db.models import DataCoverage

    row = (
        session.query(DataCoverage)
        .filter(
            DataCoverage.provider == PROVIDER,
            DataCoverage.dataset == "injuries",
            DataCoverage.league_id == league_id,
            DataCoverage.season_year == season,
        )
        .order_by(DataCoverage.last_ingested_at.desc())
        .first()
    )
    if row is None or str(row.status or "") != "plan_blocked":
        return False
    stamped = as_utc(row.last_ingested_at)
    if stamped is None:
        return True
    return (now - stamped) < timedelta(hours=cooldown_hours)


def _fetch_team_injuries(
    adapter: ApiSportsAdapter,
    *,
    api_team_id: str | int,
    season: int,
    fixture: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if fixture is not None:
        return fixture, {"endpoint": "fixture", "explicit_list": True, "season": season}
    # Current-season access requires a paid plan on Free accounts; still attempt the
    # documented team+season shape so an upgraded key works without code changes.
    payload = adapter.get(
        "/injuries",
        params={"team": int(api_team_id), "season": int(season)},
        sport="american-football",
    )
    rows = payload.get("response") if isinstance(payload.get("response"), list) else []
    return rows, {
        "endpoint": "/injuries",
        "explicit_list": True,
        "season": season,
        "results": payload.get("results"),
    }


def _map_api_teams(
    session: Session,
    adapter: ApiSportsAdapter,
    api_league: int,
    season: int,
) -> dict[str, str]:
    """Map API-Sports team id -> our Team.team_id via fuzzy name match."""
    our_teams = session.query(Team).filter(Team.league_id == ("fbs" if api_league == NCAA_LEAGUE_ID else "nfl")).all()
    catalog = [(team.team_id, team.name) for team in our_teams]
    for team in our_teams:
        for alias in team.aliases or []:
            if isinstance(alias, str):
                catalog.append((team.team_id, alias))
            elif isinstance(alias, dict):
                for key in ("location", "nick"):
                    if alias.get(key):
                        catalog.append((team.team_id, str(alias[key])))
    out: dict[str, str] = {}
    payload = None
    last_error: Exception | None = None
    for try_season in (season, season - 1, 2024, 2023, 2022):
        if try_season < 2015:
            continue
        try:
            payload = adapter.get(
                "/teams",
                params={"league": api_league, "season": try_season},
                sport="american-football",
            )
            break
        except AdapterError as exc:
            last_error = exc
            continue
    if payload is None:
        raise last_error or AdapterError("API-Sports team map unavailable")
    for row in payload.get("response") or []:
        api_id = row.get("id")
        name = as_str(row.get("name"))
        if api_id is None or not name:
            continue
        decision = match_name(name, catalog)
        if decision.review_status == "auto" and decision.canonical_id:
            out[str(api_id)] = decision.canonical_id
            continue
        # Try stripping mascot-style trailing token already handled by match_name aliases.
        location = name.split()[0] if name else ""
        if location:
            decision = match_name(location, catalog)
            if decision.review_status == "auto" and decision.canonical_id:
                out[str(api_id)] = decision.canonical_id
    return out


def _upcoming_games(session: Session, league_id: str, now: datetime, horizon_hours: int) -> list[Game]:
    return (
        session.query(Game)
        .filter(
            Game.league_id == league_id,
            Game.data_tier != "demo",
            Game.kickoff_utc >= now - timedelta(hours=3),
            Game.kickoff_utc <= now + timedelta(hours=horizon_hours),
            Game.status.in_(["scheduled", "preview", "pregame"]),
        )
        .all()
    )


def _game_for_team(games: list[Game]) -> dict[str, Game]:
    out: dict[str, Game] = {}
    for game in sorted(games, key=lambda g: as_utc(g.kickoff_utc) or datetime.max.replace(tzinfo=UTC)):
        out.setdefault(game.home_team_id, game)
        out.setdefault(game.away_team_id, game)
    return out


def _roster_catalogs(session: Session, team_ids: set[str]) -> dict[str, list[tuple[str, str]]]:
    if not team_ids:
        return {}
    rows = (
        session.query(Roster.team_id, Roster.player_id, Player.name)
        .join(Player, Player.player_id == Roster.player_id)
        .filter(Roster.team_id.in_(list(team_ids)))
        .all()
    )
    out: dict[str, list[tuple[str, str]]] = {}
    for team_id, player_id, name in rows:
        out.setdefault(team_id, []).append((player_id, name))
    return out


def _ensure_coverage_marker_player(session: Session, league_id: str) -> str:
    """Synthetic player used only to anchor game-level coverage_complete rows."""
    player_id = f"{league_id}:coverage-marker"
    if session.get(Player, player_id) is None:
        session.add(
            Player(
                player_id=player_id,
                name="Coverage Marker",
                position_group="UNK",
                position="UNK",
            )
        )
        session.flush()
    return player_id


def _player_name(row: dict[str, Any]) -> str:
    player = row.get("player") if isinstance(row.get("player"), dict) else {}
    return as_str(player.get("name")) or as_str(row.get("name")) or ""


def _designation(row: dict[str, Any]) -> str:
    player = row.get("player") if isinstance(row.get("player"), dict) else {}
    raw = (
        as_str(row.get("status"))
        or as_str(player.get("status"))
        or as_str(row.get("type"))
        or as_str((row.get("injury") or {}).get("type") if isinstance(row.get("injury"), dict) else None)
        or "unknown"
    )
    return raw[:32]


def _reported_at(row: dict[str, Any], fallback: datetime) -> datetime:
    for key in ("date", "update", "updated", "timestamp"):
        raw = row.get(key)
        if not raw and isinstance(row.get("player"), dict):
            raw = row["player"].get(key)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    return fallback


def _has_timestamp(row: dict[str, Any]) -> bool:
    return any(row.get(key) for key in ("date", "update", "updated", "timestamp"))
