"""Canonical game identity across ESPN and nflverse (and CFBD) providers.

Historical game rows are preserved. Duplicate upcoming rows are linked to one
canonical id; child facts are retargeted when safe; alias rows stay as
``data_tier='alias'`` for traceability.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, joinedload

from spe.db.base import as_utc, new_id
from spe.db.models import (
    FeatureRow,
    Game,
    InjuryEvent,
    Outcome,
    ProviderIdMap,
    SimulationRun,
    Team,
    WeatherSnapshot,
)
from spe.ingest.store import map_provider
from spe.integrity.assignments import linked_team_ids

SLATE_TZ = ZoneInfo("America/Chicago")
ALIAS_TIER = "alias"

# Same NFL club under nflverse vs ESPN abbreviations.
_NFL_TEAM_CANON = {
    "nfl:LAR": "nfl:LA",
    "nfl:WSH": "nfl:WAS",
    "nfl:JAC": "nfl:JAX",
}


def normalize_team_id(team_id: str) -> str:
    return _NFL_TEAM_CANON.get(team_id, team_id)


def kickoff_day(kickoff: datetime) -> str:
    aware = as_utc(kickoff)
    if aware is None:
        return ""
    return aware.astimezone(SLATE_TZ).date().isoformat()


def matchup_key(game: Game, teams: dict[str, Team] | None = None) -> tuple[str, str, str, str] | None:
    day = kickoff_day(game.kickoff_utc)
    if not day:
        return None
    home = normalize_team_id(game.home_team_id)
    away = normalize_team_id(game.away_team_id)
    if teams:
        # Prefer canonical club ids via linked aliases when the row itself is odd.
        home_links = linked_team_ids(teams.get(game.home_team_id)) or {game.home_team_id}
        away_links = linked_team_ids(teams.get(game.away_team_id)) or {game.away_team_id}
        home = normalize_team_id(sorted(home_links, key=_team_sort_key)[0])
        away = normalize_team_id(sorted(away_links, key=_team_sort_key)[0])
    return (game.league_id, day, home, away)


def _team_sort_key(team_id: str) -> tuple[int, str]:
    # Prefer nfl:LA over nfl:LAR, nfl:WAS over nfl:WSH, non-espn prefixes.
    prefer = 0 if team_id.startswith("nfl:") and team_id not in _NFL_TEAM_CANON else 1
    return (prefer, team_id)


def choose_canonical(games: list[Game]) -> Game:
    """Prefer nflverse-style ids for NFL; otherwise the non-espn / richest row."""

    def score(game: Game) -> tuple[int, int, str]:
        gid = game.game_id
        provider_rank = 0
        if gid.startswith("nfl:") and not gid.startswith("nfl:-"):
            provider_rank = 3
        elif gid.startswith("fbs:") and not gid.startswith("espn:"):
            provider_rank = 3
        elif gid.startswith("espn:"):
            provider_rank = 1
        elif gid.startswith("nfl-") or gid.startswith("fbs-"):
            provider_rank = 0  # seed demo
        else:
            provider_rank = 2
        tier_rank = 0 if game.data_tier in {"demo", ALIAS_TIER} else 1
        return (tier_rank, provider_rank, gid)

    return sorted(games, key=score, reverse=True)[0]


def resolve_canonical_game_id(session: Session, game_id: str) -> str:
    """Follow provider_id_map alias → canonical when present."""
    row = (
        session.query(ProviderIdMap)
        .filter(
            ProviderIdMap.entity_type == "game_alias",
            ProviderIdMap.external_id == game_id,
        )
        .one_or_none()
    )
    if row and row.canonical_id:
        return row.canonical_id
    game = session.get(Game, game_id)
    if game is None:
        return game_id
    if game.data_tier == ALIAS_TIER:
        mapped = (
            session.query(ProviderIdMap)
            .filter(
                ProviderIdMap.entity_type == "game_alias",
                ProviderIdMap.external_id == game_id,
            )
            .one_or_none()
        )
        if mapped:
            return mapped.canonical_id
    return game_id


def remap_provider(
    session: Session,
    provider: str,
    entity_type: str,
    external_id: str,
    canonical_id: str,
    *,
    confidence: float = 1.0,
    notes: str | None = None,
) -> None:
    """Insert or update a provider map so external ids always resolve to canonical."""
    existing = (
        session.query(ProviderIdMap)
        .filter_by(provider=provider, entity_type=entity_type, external_id=str(external_id))
        .one_or_none()
    )
    if existing:
        if existing.canonical_id != canonical_id:
            existing.canonical_id = canonical_id
            existing.confidence = confidence
            if notes:
                existing.notes = notes
        return
    session.add(
        ProviderIdMap(
            map_id=new_id(),
            provider=provider,
            entity_type=entity_type,
            external_id=str(external_id),
            canonical_id=canonical_id,
            confidence=confidence,
            review_status="auto",
            notes=notes,
        )
    )


def find_duplicate_groups(session: Session, *, upcoming_only: bool = True) -> list[list[Game]]:
    """Cluster same clubs within a kickoff window.

    ESPN placeholder kickoffs sometimes land on the prior calendar day in
    America/Chicago, so day-only keys miss true duplicates.
    """
    teams = {t.team_id: t for t in session.query(Team).all()}
    q = session.query(Game).options(joinedload(Game.home_team), joinedload(Game.away_team))
    if upcoming_only:
        q = q.filter(Game.data_tier.in_(("upcoming", "season", "experimental", ALIAS_TIER)))
        q = q.filter(Game.status.notin_(("final", "canceled", "cancelled")))
    by_clubs: dict[tuple[str, str, str], list[Game]] = defaultdict(list)
    for game in q.all():
        if game.data_tier == "demo":
            continue
        key = matchup_key(game, teams)
        if key is None:
            continue
        league, _day, home, away = key
        by_clubs[(league, home, away)].append(game)

    groups: list[list[Game]] = []
    window = timedelta(hours=20)
    for rows in by_clubs.values():
        rows = sorted(rows, key=lambda g: as_utc(g.kickoff_utc) or g.kickoff_utc)
        cluster: list[Game] = []
        cluster_anchor: datetime | None = None
        for game in rows:
            kick = as_utc(game.kickoff_utc) or game.kickoff_utc
            if not cluster:
                cluster = [game]
                cluster_anchor = kick
                continue
            assert cluster_anchor is not None
            if abs(kick - cluster_anchor) <= window:
                cluster.append(game)
                # Prefer the later / more precise kickoff as anchor when merging.
                if kick > cluster_anchor:
                    cluster_anchor = kick
            else:
                if len(cluster) > 1:
                    groups.append(cluster)
                cluster = [game]
                cluster_anchor = kick
        if len(cluster) > 1:
            groups.append(cluster)
    return groups


def repair_canonical_games(session: Session, *, upcoming_only: bool = True) -> dict[str, Any]:
    """Link duplicates, retarget child rows onto the canonical game, mark aliases."""
    summary: dict[str, Any] = {
        "groups": 0,
        "aliases_marked": 0,
        "outcomes_moved": 0,
        "outcomes_skipped_dup": 0,
        "injuries_moved": 0,
        "weather_moved": 0,
        "features_moved": 0,
        "simulations_moved": 0,
        "maps_written": 0,
        "examples": [],
    }
    for group in find_duplicate_groups(session, upcoming_only=upcoming_only):
        summary["groups"] += 1
        canonical = choose_canonical(group)
        aliases = [g for g in group if g.game_id != canonical.game_id]
        for alias in aliases:
            moved = _retarget_children(session, alias.game_id, canonical.game_id)
            summary["outcomes_moved"] += moved["outcomes_moved"]
            summary["outcomes_skipped_dup"] += moved["outcomes_skipped_dup"]
            summary["injuries_moved"] += moved["injuries_moved"]
            summary["weather_moved"] += moved["weather_moved"]
            summary["features_moved"] += moved["features_moved"]
            summary["simulations_moved"] += moved["simulations_moved"]

            remap_provider(
                session,
                "caliber",
                "game_alias",
                alias.game_id,
                canonical.game_id,
                notes="phase1_canonical_repair",
            )
            summary["maps_written"] += 1
            # Preserve ESPN external id → canonical.
            if alias.game_id.startswith("espn:"):
                espn_ext = alias.game_id.rsplit(":", 1)[-1]
                remap_provider(
                    session,
                    "espn",
                    "game",
                    espn_ext,
                    canonical.game_id,
                    notes="phase1_canonical_repair",
                )
                summary["maps_written"] += 1
            if canonical.game_id.startswith("nfl:"):
                map_provider(session, "nflverse", "game", canonical.game_id.split("nfl:", 1)[-1], canonical.game_id)
            if alias.data_tier != ALIAS_TIER:
                alias.data_tier = ALIAS_TIER
                summary["aliases_marked"] += 1
            if len(summary["examples"]) < 12:
                summary["examples"].append(
                    {
                        "canonical": canonical.game_id,
                        "alias": alias.game_id,
                        "kickoff": str(canonical.kickoff_utc),
                        "moved": moved,
                    }
                )
        # Ensure canonical is slate-visible when it was upcoming.
        if canonical.data_tier == ALIAS_TIER:
            canonical.data_tier = "upcoming"
        session.flush()
    return summary


def _retarget_children(session: Session, alias_id: str, canonical_id: str) -> dict[str, int]:
    out = {
        "outcomes_moved": 0,
        "outcomes_skipped_dup": 0,
        "injuries_moved": 0,
        "weather_moved": 0,
        "features_moved": 0,
        "simulations_moved": 0,
    }
    existing_keys = {
        (
            o.market_id,
            o.player_id,
            o.side,
            o.line,
            (o.description or "")[:256],
        )
        for o in session.query(Outcome).filter(Outcome.game_id == canonical_id).all()
    }
    for outcome in session.query(Outcome).filter(Outcome.game_id == alias_id).all():
        key = (
            outcome.market_id,
            outcome.player_id,
            outcome.side,
            outcome.line,
            (outcome.description or "")[:256],
        )
        if key in existing_keys:
            # Keep the alias outcome row (historical), but stop treating it as the live game.
            # Forecasts on the alias remain for audit; slate uses canonical.
            out["outcomes_skipped_dup"] += 1
            continue
        outcome.game_id = canonical_id
        existing_keys.add(key)
        out["outcomes_moved"] += 1

    for injury in session.query(InjuryEvent).filter(InjuryEvent.game_id == alias_id).all():
        injury.game_id = canonical_id
        out["injuries_moved"] += 1

    for weather in session.query(WeatherSnapshot).filter(WeatherSnapshot.game_id == alias_id).all():
        weather.game_id = canonical_id
        out["weather_moved"] += 1

    for feat in session.query(FeatureRow).filter(FeatureRow.game_id == alias_id).all():
        feat.game_id = canonical_id
        out["features_moved"] += 1

    for run in session.query(SimulationRun).filter(SimulationRun.game_id == alias_id).all():
        run.game_id = canonical_id
        out["simulations_moved"] += 1

    session.flush()
    return out


def exclude_non_canonical_tiers() -> tuple[str, ...]:
    return ("demo", ALIAS_TIER, "historical", "season")
