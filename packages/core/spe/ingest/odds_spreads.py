"""Live point-spread ingest via existing Odds API credentials.

Uses market key `spreads` only. Does not purchase historical endpoints.
Shares soft-budget accounting and event-level dedupe with odds_live.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from spe.adapters.odds_api import OddsApiAdapter
from spe.config import settings
from spe.db.base import as_utc, new_id
from spe.db.models import Bookmaker, Game, OddsTick, Outcome, Team
from spe.ingest.ats_scores import ensure_point_spread_market
from spe.ingest.odds_budget import (
    budget_status,
    reconcile_actual_cost,
    release_reservation,
    remaining_soft_budget,
    reserve_credits,
    spendable_for_bucket,
)
from spe.ingest.odds_live import _interleave_leagues, _match_events, _market_bucket, _recently_fetched  # shared matching/dedupe
from spe.ingest.snapshots import append_raw
from spe.ingest.store import upsert_coverage
from spe.ingest.values import as_float, as_int, as_str


SPREAD_MARKET_KEY = "spreads"
# Props ingest keys raw snapshots by bare event id. Spreads must use a distinct key
# or a recent props fetch falsely blocks spread refresh for the same event.
SPREAD_DEDUP_SUFFIX = ":spreads"


def _spread_dedupe_id(event_id: str) -> str:
    return f"{event_id}{SPREAD_DEDUP_SUFFIX}"


def _games_without_spread(session: Session, game_ids: list[str]) -> set[str]:
    """Games in this slate that still have no stored spread quote."""
    if not game_ids:
        return set()
    from sqlalchemy import bindparam, text

    found = session.execute(
        text(
            """
            SELECT DISTINCT game_id
            FROM outcomes
            WHERE market_id = 'point_spread'
              AND game_id IN :game_ids
            """
        ).bindparams(bindparam("game_ids", expanding=True)),
        {"game_ids": game_ids},
    ).fetchall()
    have = {row[0] for row in found}
    return {game_id for game_id in game_ids if game_id not in have}


def ingest_odds_spreads(
    session: Session,
    *,
    adapter: OddsApiAdapter | None = None,
    now: datetime | None = None,
    leagues: tuple[str, ...] = ("nfl", "fbs"),
    max_events: int | None = None,
    fixture_events: dict[str, list[dict[str, Any]]] | None = None,
    fixture_odds: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pull current game spreads. Soft credit budget still applies."""
    now = now or datetime.now(UTC)
    adapter = adapter or OddsApiAdapter()
    ensure_point_spread_market(session)
    # Older ticks matched "Michigan" inside "Michigan State" and stored the dog number on the favorite.
    reassign_spread_sides(session)
    if not adapter.api_key and not fixture_events:
        for league in leagues:
            upsert_coverage(
                session,
                provider="odds_api",
                dataset="live_spreads",
                league_id=league,
                season_year=0,
                row_count=0,
                status="needs_key",
                notes="Set ODDS_API_KEY to ingest live spreads. Historical game odds remain paid-plan.",
            )
        return {"status": "needs_key", "ticks_upserted": 0, "credits_spent": 0}

    for book_id in settings.odds_bookmaker_list:
        if session.get(Bookmaker, book_id) is None:
            session.add(Bookmaker(bookmaker_id=book_id, name=book_id))
    session.flush()

    near_reserve = max(0, int(settings.odds_near_reserve_credits))
    totals: dict[str, Any] = {
        "status": "ok",
        "ticks_upserted": 0,
        "ticks_price_changed": 0,
        "ticks_reobserved": 0,
        "events_fetched": 0,
        "events_skipped": 0,
        "skipped_dedup": 0,
        "skipped_for_near_reserve": 0,
        "credits_spent": 0,
        "credits_remaining_budget": remaining_soft_budget(session),
        "near_reserve_credits": near_reserve,
        "budget": budget_status(session),
        "leagues": {},
        "research_only": True,
        "play_enabled": False,
    }
    horizon = now + timedelta(hours=settings.odds_upcoming_hours)
    limit = max_events if max_events is not None else min(20, settings.odds_max_events_per_run)
    pending: list[tuple[str, Game, dict[str, Any]]] = []
    for league in leagues:
        events = adapter.fetch_events(
            league, fixture=fixture_events.get(league) if fixture_events else None
        ).records
        matched = _match_events(session, league, events, now, horizon)
        pending.extend((league, game, event) for game, event in matched)

    # Games with no number go first. A capped run used to re-buy the early
    # slate and leave the late window blank.
    missing_ids = _games_without_spread(session, [game.game_id for _, game, _ in pending])
    pending.sort(
        key=lambda item: (
            0 if item[1].game_id in missing_ids else 1,
            as_utc(item[1].kickoff_utc) or now,
        )
    )
    pending = _interleave_leagues(pending)
    league_ticks = {lg: 0 for lg in leagues}
    for league, game, event in pending:
        if totals["events_fetched"] >= limit:
            break
        dedupe_id = _spread_dedupe_id(str(event["id"]))
        # Dedupe against prior *spreads* fetches only — not props (bare event id).
        if fixture_odds is None and _recently_fetched(session, dedupe_id, now, timedelta(minutes=20)):
            totals["events_skipped"] += 1
            totals["skipped_dedup"] += 1
            continue
        est = adapter._estimate_credits(1)
        bucket = _market_bucket(game, now)
        spendable = spendable_for_bucket(session, bucket, allow_near_emergency=False)
        if fixture_odds is None and spendable < est:
            totals["events_skipped"] += 1
            totals["skipped_for_near_reserve"] += 1
            if bucket == "near":
                totals["status"] = "budget_stop"
                break
            continue
        reserved = 0
        if fixture_odds is None:
            if not reserve_credits(session, est):
                totals["events_skipped"] += 1
                totals["skipped_for_near_reserve"] += 1
                if bucket == "near":
                    totals["status"] = "budget_stop"
                    break
                continue
            reserved = est
        try:
            result = adapter.fetch_event_odds(
                league,
                event["id"],
                markets=[SPREAD_MARKET_KEY],
                fixture=(fixture_odds or {}).get(event["id"]),
                remaining_budget=None if fixture_odds else max(est, remaining_soft_budget(session) + est),
            )
        except Exception:
            release_reservation(session, reserved)
            raise
        if fixture_odds:
            cost = 0
        elif result.credit_cost is None:
            cost = int(est)
        else:
            cost = int(result.credit_cost)
        if not fixture_odds:
            reconcile_actual_cost(session, reserved, cost)
        append_raw(
            session,
            provider="odds_api",
            endpoint=result.endpoint,
            provider_record_id=dedupe_id,
            payload={
                "event": event,
                "n_records": len(result.records),
                "markets": [SPREAD_MARKET_KEY],
                "dataset": "spreads",
                "observation_ok": True,
                "dedupe_id": dedupe_id,
            },
            observed_at=now,
            credit_cost=0,
        )
        totals["credits_spent"] += cost
        totals["events_fetched"] += 1
        home = session.get(Team, game.home_team_id)
        away = session.get(Team, game.away_team_id)
        home_names = _name_variants(home)
        away_names = _name_variants(away)
        for rec in result.records:
            if rec.get("market_key") != SPREAD_MARKET_KEY:
                continue
            added, changed = _store_spread_tick(session, game, rec, home_names, away_names, now, cost)
            league_ticks[league] += added
            totals["ticks_upserted"] += added
            if changed:
                totals["ticks_price_changed"] += 1
            elif added:
                totals["ticks_reobserved"] += 1

    for league in leagues:
        upsert_coverage(
            session,
            provider="odds_api",
            dataset="live_spreads",
            league_id=league,
            season_year=0,
            row_count=league_ticks[league],
            status="available" if league_ticks[league] else "empty",
            notes=f"Live spreads only. Credits spent this run={totals['credits_spent']}.",
        )
        totals["leagues"][league] = {"ticks": league_ticks[league]}
    totals["credits_remaining_budget"] = remaining_soft_budget(session)
    totals["budget"] = budget_status(session)
    session.flush()
    return totals


def _fold_name(value: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in value.lower()).split())


def _name_variants(team: Team | None) -> set[str]:
    """School, nickname, and location. Matching uses the longest phrase, not the first hit."""
    if team is None:
        return set()
    raw: set[str] = set()
    if team.name:
        raw.add(team.name)
    if team.abbreviation:
        raw.add(team.abbreviation)
    for alias in team.aliases or []:
        if isinstance(alias, str):
            raw.add(alias)
        elif isinstance(alias, dict):
            location = str(alias.get("location") or "").strip()
            nick = str(alias.get("nick") or "").strip()
            if location:
                raw.add(location)
            if nick:
                raw.add(nick)
            if location and nick:
                raw.add(f"{location} {nick}")
    return {name for name in raw if name and name.strip()}


def _phrase_score(book_name: str, variants: set[str]) -> int:
    """Longest whole-word variant inside the book name. Exact names outrank a shared prefix."""
    folded = _fold_name(book_name)
    if not folded:
        return 0
    padded = f" {folded} "
    best = 0
    for variant in variants:
        token = _fold_name(variant)
        if not token:
            continue
        if folded == token:
            best = max(best, 1000 + len(token))
            continue
        # "michigan" is a word in "michigan state". Length decides, so the full school name wins.
        if len(token) < 4:
            continue
        if f" {token} " in padded:
            best = max(best, len(token))
    return best


def spread_side(book_name: str, home_names: set[str], away_names: set[str]) -> str | None:
    """Home or away. A tie, including two schools that share a first word, is left unmatched."""
    home_score = _phrase_score(book_name, home_names)
    away_score = _phrase_score(book_name, away_names)
    if home_score == away_score or (home_score <= 0 and away_score <= 0):
        return None
    if home_score <= 0:
        return "away"
    if away_score <= 0 or home_score > away_score:
        return "home"
    return "away"


def team_name_from_spread_record(record_id: str | None) -> str | None:
    """Team name embedded in an Odds API spread record id. None when the id is not a spread."""
    if not record_id:
        return None
    parts = str(record_id).split(":")
    if len(parts) < 6 or parts[2] != "spreads":
        return None
    name = ":".join(parts[3:-2]).strip()
    if not name or name.lower() == "none":
        return None
    return name


def reassign_spread_sides(session: Session) -> dict[str, int]:
    """Move spread ticks onto the school named in the book record.

    A short name used to win: "Michigan" matched inside "Michigan State", so the
    underdog's +14 was stored as the home number. The record id still has the
    book’s team name, so the side can be corrected without another API call.
    """
    outcomes = session.query(Outcome).filter(Outcome.market_id == "point_spread").all()
    games = {
        game.game_id: game
        for game in session.query(Game).filter(Game.game_id.in_({row.game_id for row in outcomes})).all()
    } if outcomes else {}
    team_ids = {game.home_team_id for game in games.values()} | {game.away_team_id for game in games.values()}
    teams = {
        team.team_id: team
        for team in session.query(Team).filter(Team.team_id.in_(team_ids)).all()
    } if team_ids else {}
    ticks_by_outcome: dict[str, list[OddsTick]] = {}
    if outcomes:
        outcome_ids = [row.outcome_id for row in outcomes]
        for tick in session.query(OddsTick).filter(OddsTick.outcome_id.in_(outcome_ids)).all():
            ticks_by_outcome.setdefault(tick.outcome_id, []).append(tick)
    by_key = {(row.game_id, row.side, row.line): row for row in outcomes}
    moved = skipped = 0
    for outcome in list(outcomes):
        game = games.get(outcome.game_id)
        ticks = ticks_by_outcome.get(outcome.outcome_id, [])
        if game is None or not ticks:
            skipped += 1
            continue
        home_names = _name_variants(teams.get(game.home_team_id))
        away_names = _name_variants(teams.get(game.away_team_id))
        for tick in list(ticks):
            name = team_name_from_spread_record(tick.provider_record_id)
            side = spread_side(name or "", home_names, away_names)
            if side is None:
                skipped += 1
                continue
            if side == outcome.side:
                continue
            key = (outcome.game_id, side, outcome.line)
            target = by_key.get(key)
            if target is None:
                target = Outcome(
                    outcome_id=new_id(),
                    market_id="point_spread",
                    game_id=outcome.game_id,
                    player_id=None,
                    side=side,
                    line=outcome.line,
                    description=f"{side} {float(outcome.line):+g}",
                )
                session.add(target)
                by_key[key] = target
                session.flush()
            tick.outcome_id = target.outcome_id
            ticks_by_outcome.setdefault(target.outcome_id, []).append(tick)
            ticks.remove(tick)
            moved += 1
    session.flush()
    for outcome in outcomes:
        if not ticks_by_outcome.get(outcome.outcome_id):
            session.delete(outcome)
    session.flush()
    return {"ticks_moved": moved, "ticks_unmatched": skipped}


def _store_spread_tick(
    session: Session,
    game: Game,
    rec: dict[str, Any],
    home_names: set[str],
    away_names: set[str],
    now: datetime,
    credit_cost: int,
) -> tuple[int, bool]:
    """Returns (added_or_reobserved, price_changed)."""
    point = as_float(rec.get("point"))
    price = as_int(rec.get("price_american"), None)
    book = as_str(rec.get("bookmaker"))
    if point is None or price is None or not book:
        return 0, False
    side = spread_side(as_str(rec.get("name")) or "", home_names, away_names)
    if side is None:
        return 0, False
    line = float(point)
    outcome = (
        session.query(Outcome)
        .filter(
            Outcome.game_id == game.game_id,
            Outcome.market_id == "point_spread",
            Outcome.side == side,
            Outcome.line == line,
        )
        .one_or_none()
    )
    if outcome is None:
        outcome = Outcome(
            outcome_id=new_id(),
            market_id="point_spread",
            game_id=game.game_id,
            player_id=None,
            side=side,
            line=line,
            description=f"{side} {line:+g}",
        )
        session.add(outcome)
        session.flush()
    source_at = _parse_ts(rec.get("source_at")) or _parse_ts(rec.get("last_update"))
    latest = (
        session.query(OddsTick)
        .filter(OddsTick.outcome_id == outcome.outcome_id, OddsTick.bookmaker_id == book)
        .order_by(OddsTick.captured_at.desc())
        .first()
    )
    if latest and latest.price_american == price:
        latest.observed_at = now
        latest.captured_at = now
        if source_at is not None:
            latest.source_at = source_at
            latest.effective_at = source_at
        latest.quality_status = "ok"
        return 1, False
    session.add(
        OddsTick(
            tick_id=new_id(),
            outcome_id=outcome.outcome_id,
            bookmaker_id=book,
            price_american=price,
            captured_at=now,
            source_at=source_at or now,
            is_live=False,
            credit_cost=credit_cost,
            provider="odds_api",
            provider_record_id=str(rec.get("provider_record_id") or f"{game.game_id}:{book}:{side}:{line}"),
            observed_at=now,
            effective_at=source_at or now,
            quality_status="ok",
        )
    )
    return 1, True


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
