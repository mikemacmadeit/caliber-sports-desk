"""Live Odds API player-prop ingest. Never fabricates historical ticks."""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from spe.adapters.odds_api import MARKET_KEYS, OddsApiAdapter
from spe.config import settings
from spe.db.base import as_utc
from spe.db.models import Bookmaker, Game, Market, OddsTick, Outcome, Roster, Team
from spe.identity.match import match_name
from spe.ingest.canonical import NFL_TEAM_NAMES, nfl_team_id
from spe.ingest.odds_budget import (
    budget_status,
    effective_soft_budget,
    reconcile_actual_cost,
    release_reservation,
    remaining_soft_budget,
    reserve_credits,
    spendable_for_bucket,
)
from spe.ingest.snapshots import append_raw
from spe.ingest.store import upsert_coverage
from spe.ingest.values import as_str

def _note_progress(message: str) -> None:
    """Append one line so a killed download still shows the last game that saved."""
    try:
        path = Path(settings.parquet_dir).parent / "cache" / "odds_live_progress.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(UTC).isoformat()} {message}\n")
    except OSError:
        return


MARKET_BY_ODDS_KEY = {v: k for k, v in MARKET_KEYS.items()}
NFL_NAME_TO_ABBR = {name.lower(): abbr for abbr, name in NFL_TEAM_NAMES.items()}
NFL_NAME_TO_ABBR.update(
    {
        "washington commanders": "WAS",
        "washington football team": "WAS",
        "washington redskins": "WAS",
        "st. louis rams": "STL",
        "los angeles rams": "LAR",
        "los angeles chargers": "LAC",
        "oakland raiders": "OAK",
        "san diego chargers": "SD",
    }
)


def ingest_odds_live(
    session: Session,
    *,
    adapter: OddsApiAdapter | None = None,
    now: datetime | None = None,
    leagues: tuple[str, ...] = ("nfl", "fbs"),
    fixture_events: dict[str, list[dict[str, Any]]] | None = None,
    fixture_odds: dict[str, dict[str, Any]] | None = None,
    horizon_hours: int | None = None,
    publish_forecasts: bool = True,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    adapter = adapter or OddsApiAdapter()
    if fixture_odds is None:
        _note_progress(f"start leagues={','.join(leagues)} hours={horizon_hours}")
    if not adapter.api_key and not fixture_events:
        for league in leagues:
            upsert_coverage(
                session,
                provider="odds_api",
                dataset="live_player_props",
                league_id=league,
                season_year=0,
                row_count=0,
                status="needs_key",
                notes="Set ODDS_API_KEY to ingest current player props. Historical ticks remain paid-plan.",
            )
        return {"status": "needs_key", "ticks_upserted": 0, "credits_spent": 0}

    configured_budget = effective_soft_budget()
    near_reserve = max(0, int(settings.odds_near_reserve_credits))
    totals = {
        "status": "ok",
        "ticks_upserted": 0,
        "ticks_price_changed": 0,
        "ticks_reobserved": 0,
        "events_fetched": 0,
        "events_skipped": 0,
        "skipped_for_near_reserve": 0,
        "skipped_dedup": 0,
        "unmatched_players": 0,
        "unlinked_outcomes": 0,
        "credits_spent": 0,
        "credits_reserved": 0,
        "credits_remaining_budget": remaining_soft_budget(session),
        "configured_daily_budget": configured_budget,
        "standard_daily_budget": int(settings.odds_credit_budget_per_day),
        "near_reserve_credits": near_reserve,
        "api_remaining": None,
        "budget": budget_status(session),
        "leagues": {},
        "markets_by_bucket": {"near": 0, "mid": 0, "far": 0},
    }
    _ensure_books(session)
    # Do not hold the database write lock while matching the slate or calling the odds API.
    session.commit()
    horizon = now + timedelta(hours=horizon_hours if horizon_hours is not None else settings.odds_upcoming_hours)
    min_age = timedelta(minutes=settings.odds_min_refresh_minutes)
    pending: list[tuple[str, Game, dict[str, Any]]] = []
    matched_counts = {league: 0 for league in leagues}
    for league in leagues:
        events = adapter.fetch_events(
            league, fixture=fixture_events.get(league) if fixture_events else None
        ).records
        matched = _match_events(session, league, events, now, horizon)
        matched_counts[league] = len(matched)
        pending.extend((league, game, event) for game, event in matched)
    pending.sort(key=lambda item: _refresh_priority(session, item[1], now))
    # One league's live slate must not spend the whole run before the other league is priced.
    pending = _interleave_leagues(pending)
    league_ticks = {league: 0 for league in leagues}
    fetched_by = {league: 0 for league in leagues}
    totals["events_unavailable_cooldown"] = 0
    for league, game, event in pending:
        if totals["events_fetched"] >= settings.odds_max_events_per_run:
            break
        event_min_age = _adaptive_min_age(game, now, min_age)
        if _unavailable_cooldown(session, event["id"], now):
            totals["events_skipped"] += 1
            totals["events_unavailable_cooldown"] += 1
            continue
        if _recently_fetched(session, event["id"], now, event_min_age) and not _needs_urgent_refresh(
            session, game, now
        ):
            totals["events_skipped"] += 1
            totals["skipped_dedup"] += 1
            continue
        # Skip far-out games that already have a fresh-enough price unless urgent.
        if _skip_far_fresh_price(session, game, now):
            totals["events_skipped"] += 1
            continue
        markets = _markets_for_game(game, now)
        bucket = _market_bucket(game, now)
        totals["markets_by_bucket"][bucket] = int(totals["markets_by_bucket"].get(bucket, 0)) + 1
        est = adapter._estimate_credits(len(markets))
        urgent = _needs_urgent_refresh(session, game, now)
        spendable = spendable_for_bucket(session, bucket, allow_near_emergency=urgent)
        if fixture_odds is None and spendable < est:
            # Soft cap is hard. Near reserve is within-cap priority, not overspend.
            if bucket == "near":
                totals["status"] = "budget_stop"
                break
            totals["events_skipped"] += 1
            totals["skipped_for_near_reserve"] += 1
            continue
        elif fixture_odds is None:
            if not reserve_credits(session, est):
                if bucket == "near":
                    totals["status"] = "budget_stop"
                    break
                totals["events_skipped"] += 1
                totals["skipped_for_near_reserve"] += 1
                continue
            reserved = est
        else:
            reserved = 0
        totals["credits_reserved"] += reserved
        # The reservation is saved. Do not hold the database during the price call.
        session.commit()
        try:
            result = adapter.fetch_event_odds(
                league,
                event["id"],
                markets=markets,
                fixture=(fixture_odds or {}).get(event["id"]),
                remaining_budget=None if fixture_odds else max(est, remaining_soft_budget(session) + est),
            )
        except Exception:
            release_reservation(session, reserved)
            raise
        # credit_cost 0 is valid (e.g. empty/unavailable); do not coerce via `or est`.
        if fixture_odds:
            cost = 0
        elif result.credit_cost is None:
            cost = int(est)
        else:
            cost = int(result.credit_cost)
        if not fixture_odds:
            reconcile_actual_cost(session, reserved, cost)
        empty = len(result.records) == 0
        # Raw snapshot records the observation; credit already reserved/reconciled.
        append_raw(
            session,
            provider="odds_api",
            endpoint=result.endpoint,
            provider_record_id=str(event["id"]),
            payload={
                "event": event,
                "n_records": len(result.records),
                "markets_unavailable": empty,
                "markets_requested": markets,
                "market_bucket": bucket,
                "kickoff_utc": (as_utc(game.kickoff_utc) or now).isoformat(),
                "observation_ok": True,
                "price_rows": len(result.records),
            },
            observed_at=now,
            credit_cost=0,
        )
        if adapter.last_credits_remaining is not None:
            totals["api_remaining"] = adapter.last_credits_remaining
            totals["budget"] = budget_status(session, provider_remaining=adapter.last_credits_remaining)
        totals["credits_spent"] += cost
        fetched_by[league] += 1
        totals["events_fetched"] += 1
        # Audit current membership once per event, then reuse it for every book quote.
        catalog = _roster_catalog(session, game, apply_eligibility_audit=True)
        eligible_ids = {player_id for player_id, _ in catalog}
        for rec in result.records:
            added, unmatched, unlinked, changed = _store_tick(
                session, game, rec, catalog, now, cost, eligible_ids=eligible_ids
            )
            league_ticks[league] += added
            totals["ticks_upserted"] += added
            totals["unmatched_players"] += unmatched
            totals["unlinked_outcomes"] += unlinked
            if changed:
                totals["ticks_price_changed"] += 1
            elif added:
                totals["ticks_reobserved"] += 1
        # Save each game before the next one. A later game or the forecast pass
        # must not throw away prices that were already paid for.
        session.commit()
        if fixture_odds is None:
            line = f"priced {league} {game.game_id} events={totals['events_fetched']} credits={totals['credits_spent']}"
            print(line, flush=True)
            _note_progress(line)
    for league in leagues:
        league_ticks_n = league_ticks[league]
        totals["leagues"][league] = {
            "events": fetched_by[league],
            "ticks": league_ticks_n,
            "matched_upcoming": matched_counts[league],
        }
        upsert_coverage(
            session,
            provider="odds_api",
            dataset="live_player_props",
            league_id=league,
            season_year=0,
            row_count=league_ticks_n,
            status="available" if league_ticks_n else "empty",
            notes=(
                "Live ticks only. Historical player-prop snapshots remain paid-plan. "
                f"Credits spent this run={totals['credits_spent']}."
            ),
        )
    totals["credits_remaining_budget"] = remaining_soft_budget(session)
    totals["budget"] = budget_status(session, provider_remaining=totals.get("api_remaining"))
    session.commit()
    if publish_forecasts:
        from spe.ingest.model_forecasts import publish_model_forecasts

        totals["forecasts"] = publish_model_forecasts(session, now)
    else:
        totals["forecasts"] = {"skipped": True}
    session.flush()
    return totals


def _ensure_books(session: Session) -> None:
    for book_id in settings.odds_bookmaker_list:
        if session.get(Bookmaker, book_id) is None:
            session.add(Bookmaker(bookmaker_id=book_id, name=book_id))
    session.flush()


def _match_events(
    session: Session,
    league: str,
    events: list[dict[str, Any]],
    now: datetime,
    horizon: datetime,
) -> list[tuple[Game, dict[str, Any]]]:
    games = (
        session.query(Game)
        .filter(
            Game.league_id == league,
            Game.data_tier != "demo",
            Game.kickoff_utc >= now - timedelta(hours=3),
            Game.kickoff_utc <= horizon,
        )
        .all()
    )
    by_teams: dict[tuple[str, str], Game] = {}
    teams = {t.team_id: t for t in session.query(Team).filter(Team.league_id == league)}
    for game in games:
        home = teams.get(game.home_team_id)
        away = teams.get(game.away_team_id)
        if not home or not away:
            continue
        by_teams[(_norm(home.name), _norm(away.name))] = game
        if league == "nfl":
            by_teams[(_norm(NFL_TEAM_NAMES.get(home.abbreviation, home.name)), _norm(NFL_TEAM_NAMES.get(away.abbreviation, away.name)))] = game
    matched: list[tuple[Game, dict[str, Any]]] = []
    for event in events:
        commence = _parse_ts(event.get("commence_time"))
        if commence is None or commence < now - timedelta(hours=3) or commence > horizon:
            continue
        home = _norm(event.get("home_team"))
        away = _norm(event.get("away_team"))
        game = by_teams.get((home, away))
        other = _fuzzy_game(games, teams, event, league)
        if other is not None and (game is None or _prefer_slate_game(other, game) is other):
            game = other
        if game is None:
            continue
        matched.append((game, event))
    return matched


_NFL_ABBR_ALIAS = {"LA": "LAR", "LAR": "LA", "WAS": "WSH", "WSH": "WAS", "JAC": "JAX", "JAX": "JAC"}


def _prefer_slate_game(candidate: Game, current: Game) -> Game:
    """The board hides an ESPN copy when the nflverse game is on the same field."""
    if current.game_id.startswith("espn:") and not candidate.game_id.startswith("espn:"):
        return candidate
    return current


def _same_nfl_club(team_id: str, wanted_id: str) -> bool:
    if team_id == wanted_id:
        return True
    left, right = team_id.rsplit(":", 1)[-1], wanted_id.rsplit(":", 1)[-1]
    return _NFL_ABBR_ALIAS.get(left) == right


def _fuzzy_game(games: list[Game], teams: dict[str, Team], event: dict[str, Any], league: str) -> Game | None:
    home = str(event.get("home_team") or "")
    away = str(event.get("away_team") or "")
    if league == "nfl":
        home_id = _nfl_id(home)
        away_id = _nfl_id(away)
        if not home_id or not away_id:
            return None
        hits = [
            game
            for game in games
            if _same_nfl_club(game.home_team_id, home_id) and _same_nfl_club(game.away_team_id, away_id)
        ]
        if not hits:
            return None
        chosen = hits[0]
        for game in hits[1:]:
            chosen = _prefer_slate_game(game, chosen)
        return chosen
    home_n, away_n = _norm(home), _norm(away)
    for game in games:
        ht = teams.get(game.home_team_id)
        at = teams.get(game.away_team_id)
        if ht and at and _names_compatible(ht.name, home_n) and _names_compatible(at.name, away_n):
            return game
    return None


def _nfl_id(name: str) -> str | None:
    abbr = NFL_NAME_TO_ABBR.get(name.lower().strip())
    return nfl_team_id(abbr) if abbr else None


def _roster_catalog(
    session: Session, game: Game, *, apply_eligibility_audit: bool = False
) -> list[tuple[str, str]]:
    """Current eligible players only — never the multi-season open roster dump."""
    from spe.identity.roster import eligible_players_for_game

    # The ingest loop requests the full audit once and reuses these validated IDs.
    # Other latency-sensitive callers can retain the status-filtered catalog.
    return [
        (row.player_id, row.name)
        for row in eligible_players_for_game(
            session,
            game,
            include_inactive_for_props=True,
            apply_eligibility_audit=apply_eligibility_audit,
        )
    ]


def _store_tick(
    session: Session,
    game: Game,
    rec: dict[str, Any],
    catalog: list[tuple[str, str]],
    observed_at: datetime,
    credit_cost: int,
    *,
    eligible_ids: set[str] | None = None,
) -> tuple[int, int, int, bool]:
    """Returns (added, unmatched, unlinked, price_changed).

    Unchanged quotes still update last_observed_at so freshness reflects a
    successful reobservation, not only a price-change tick.
    """
    market_id = MARKET_BY_ODDS_KEY.get(str(rec.get("market_key") or ""))
    if not market_id or session.get(Market, market_id) is None:
        return 0, 0, 0, False
    book_id = as_str(rec.get("bookmaker"))
    if not book_id:
        return 0, 0, 0, False
    if session.get(Bookmaker, book_id) is None:
        session.add(Bookmaker(bookmaker_id=book_id, name=book_id))
        session.flush()
    description = as_str(rec.get("description")) or ""
    player_id = None
    unmatched = 0
    unlinked = 0
    if description and catalog:
        decision = match_name(description, catalog)
        if decision.review_status == "auto":
            player_id = decision.canonical_id
            # Never accept a fuzzy/auto match that fails roster membership for this game.
            on_roster = (
                player_id in eligible_ids if eligible_ids is not None
                else _player_on_game_roster(session, game, player_id)
            )
            if not on_roster:
                player_id = None
                unlinked = 1
                unmatched = 1
        else:
            unmatched = 1
            unlinked = 1
    elif description:
        unmatched = 1
        unlinked = 1
    side = _side(rec.get("name"))
    line = rec.get("point")
    line_f = float(line) if line is not None else None
    # Ambiguous / unmatched names stay unlinked — never force a player_id.
    outcome = _get_or_create_outcome(session, market_id, game.game_id, player_id, side, line_f, description)
    if player_id is None and description:
        unlinked = max(unlinked, 1)
    price = rec.get("price_american")
    if price is None:
        return 0, unmatched, unlinked, False
    # Provider bookmaker timestamp when present; never invent one after a failure.
    source_at = _parse_ts(rec.get("source_at")) or _parse_ts(rec.get("last_update"))
    last = (
        session.query(OddsTick)
        .filter_by(outcome_id=outcome.outcome_id, bookmaker_id=book_id)
        .order_by(OddsTick.captured_at.desc())
        .first()
    )
    price_i = int(price)
    if last and last.price_american == price_i:
        # Successful reobservation of an unchanged quote.
        last.observed_at = observed_at
        last.captured_at = observed_at
        if source_at is not None:
            last.source_at = source_at
            last.effective_at = source_at
        last.quality_status = "ok"
        return 1, unmatched, unlinked, False
    session.add(
        OddsTick(
            outcome_id=outcome.outcome_id,
            bookmaker_id=book_id,
            price_american=price_i,
            captured_at=observed_at,
            source_at=source_at or observed_at,
            is_live=False,
            credit_cost=credit_cost,
            provider="odds_api",
            provider_record_id=str(rec.get("provider_record_id") or "")[:160],
            observed_at=observed_at,
            effective_at=source_at or observed_at,
            quality_status="ok",
        )
    )
    return 1, unmatched, unlinked, True


def _player_on_game_roster(session: Session, game: Game, player_id: str) -> bool:
    from spe.identity.roster import eligible_players_for_game

    return any(row.player_id == player_id for row in eligible_players_for_game(session, game, include_inactive_for_props=True))


def _get_or_create_outcome(
    session: Session,
    market_id: str,
    game_id: str,
    player_id: str | None,
    side: str,
    line: float | None,
    description: str,
) -> Outcome:
    desc = (description or "")[:256]
    row = _find_outcome(session, market_id, game_id, player_id, side, line, desc)
    if row:
        # Never attach a player_id to an already-linked different player, and never
        # promote an unlinked row with an unverified id (caller must validate roster).
        if player_id and row.player_id is None:
            row.player_id = player_id
        if desc and not row.description:
            row.description = desc
        return row
    outcome_id = _deterministic_outcome_id(market_id, game_id, player_id, side, line)
    if outcome_id:
        existing = session.get(Outcome, outcome_id)
        if existing:
            return existing
    fields: dict[str, Any] = {
        "market_id": market_id,
        "game_id": game_id,
        "player_id": player_id,
        "side": side,
        "line": line,
        "description": desc,
    }
    if outcome_id:
        fields["outcome_id"] = outcome_id
    row = Outcome(**fields)
    session.add(row)
    session.flush()
    return row


def _find_outcome(
    session: Session,
    market_id: str,
    game_id: str,
    player_id: str | None,
    side: str,
    line: float | None,
    description: str,
) -> Outcome | None:
    q = session.query(Outcome).filter_by(market_id=market_id, game_id=game_id, side=side)
    if line is None:
        q = q.filter(Outcome.line.is_(None))
    else:
        q = q.filter_by(line=line)
    if player_id:
        by_player = q.filter_by(player_id=player_id).order_by(Outcome.outcome_id).first()
        if by_player:
            return by_player
        # Reuse an unmatched description row created before roster match succeeded.
        if description:
            orphan = q.filter(Outcome.player_id.is_(None), Outcome.description == description).first()
            if orphan:
                return orphan
        return None
    if description:
        return q.filter_by(description=description).order_by(Outcome.outcome_id).first()
    return q.filter(Outcome.player_id.is_(None), Outcome.description == "").first()


def _deterministic_outcome_id(
    market_id: str,
    game_id: str,
    player_id: str | None,
    side: str,
    line: float | None,
) -> str | None:
    """Stable ids for player yes-lines so odds + slate ingest share one outcome."""
    if not player_id or side != "yes" or line is not None:
        return None
    if market_id != "anytime_td":
        return None
    digest = hashlib.sha1(f"{game_id}:{player_id}".encode("utf-8")).hexdigest()[:40]
    return f"atd:{digest}"


def _recently_fetched(session: Session, event_id: str, now: datetime, min_age: timedelta) -> bool:
    from spe.db.models import RawSnapshot

    row = (
        session.query(RawSnapshot)
        .filter_by(provider="odds_api", provider_record_id=str(event_id))
        .order_by(RawSnapshot.observed_at.desc())
        .first()
    )
    if row is None:
        return False
    observed = as_utc(row.observed_at)
    if observed is None:
        return False
    return (now - observed) < min_age


def _unavailable_cooldown(session: Session, event_id: str, now: datetime) -> bool:
    """Avoid re-spending credits on events that returned no player-prop markets."""
    from spe.db.models import RawSnapshot

    row = (
        session.query(RawSnapshot)
        .filter_by(provider="odds_api", provider_record_id=str(event_id))
        .order_by(RawSnapshot.observed_at.desc())
        .first()
    )
    if row is None:
        return False
    payload = row.payload if isinstance(row.payload, dict) else {}
    if not payload.get("markets_unavailable"):
        return False
    observed = as_utc(row.observed_at)
    if observed is None:
        return False
    return (now - observed) < timedelta(hours=4)


def _adaptive_min_age(game: Game, now: datetime, default: timedelta) -> timedelta:
    """Polling cadence: aggressive near kickoff, sparse for distant games."""
    kickoff = as_utc(game.kickoff_utc) or now
    hours = (kickoff - now).total_seconds() / 3600.0
    if hours <= 2:
        return timedelta(minutes=max(1, int(settings.odds_near_poll_minutes)))
    if hours <= 6:
        return timedelta(minutes=15)
    if hours <= 24:
        return timedelta(minutes=90)
    if hours <= 72:
        return timedelta(minutes=180)
    return max(default, timedelta(minutes=360))


def _latest_price_age_minutes(session: Session, game_id: str, now: datetime) -> float | None:
    """Age from last successful observation (reobserve counts), not only price ticks."""
    row = (
        session.query(OddsTick.observed_at, OddsTick.captured_at)
        .join(Outcome, Outcome.outcome_id == OddsTick.outcome_id)
        .filter(Outcome.game_id == game_id)
        .order_by(OddsTick.observed_at.desc(), OddsTick.captured_at.desc())
        .first()
    )
    if row is None:
        return None
    stamp = as_utc(row[0]) or as_utc(row[1])
    if stamp is None:
        return None
    return (now - stamp).total_seconds() / 60.0


def _needs_urgent_refresh(session: Session, game: Game, now: datetime) -> bool:
    kickoff = as_utc(game.kickoff_utc) or now
    hours = (kickoff - now).total_seconds() / 3600.0
    if hours > 6:
        return False
    age = _latest_price_age_minutes(session, game.game_id, now)
    if age is None:
        return True
    from spe.market.price_age import max_fresh_minutes

    ceiling = max_fresh_minutes(game.kickoff_utc, now) if hours <= 2 else float(settings.max_price_age_minutes)
    return age > ceiling


def _interleave_leagues(pending: list[tuple[str, Game, dict[str, Any]]]) -> list[tuple[str, Game, dict[str, Any]]]:
    """Round-robin leagues while keeping each league's existing order."""
    buckets: dict[str, list[tuple[str, Game, dict[str, Any]]]] = {}
    for item in pending:
        buckets.setdefault(item[0], []).append(item)
    lists = list(buckets.values())
    mixed: list[tuple[str, Game, dict[str, Any]]] = []
    while any(lists):
        for rows in lists:
            if rows:
                mixed.append(rows.pop(0))
    return mixed


def _refresh_priority(session: Session, game: Game, now: datetime) -> tuple[int, float, int]:
    """Near-kickoff and stale/missing prices first. Lower tuple sorts earlier."""
    kickoff = as_utc(game.kickoff_utc) or now
    hours = max(0.0, (kickoff - now).total_seconds() / 3600.0)
    age = _latest_price_age_minutes(session, game.game_id, now)
    missing = age is None
    stale = age is not None and age > float(settings.max_price_age_minutes)
    if hours <= 6 and (missing or stale):
        bucket = 0
    elif hours <= 24:
        bucket = 1
    elif missing:
        bucket = 2
    elif stale:
        bucket = 3
    else:
        bucket = 4
    return (bucket, hours, 0 if missing else 1)


def _market_bucket(game: Game, now: datetime) -> str:
    kickoff = as_utc(game.kickoff_utc) or now
    hours = (kickoff - now).total_seconds() / 3600.0
    if hours <= settings.odds_near_hours:
        return "near"
    if hours <= settings.odds_mid_hours:
        return "mid"
    return "far"


def _markets_for_game(game: Game, now: datetime) -> list[str]:
    """Full market set near kickoff; thinner set farther out to save credits."""
    bucket = _market_bucket(game, now)
    if bucket == "far":
        return list(settings.odds_far_market_list or settings.odds_market_list)
    return list(settings.odds_market_list)


def _skip_far_fresh_price(session: Session, game: Game, now: datetime) -> bool:
    """Do not spend credits on far-out games whose prices are still within max age."""
    if _market_bucket(game, now) != "far":
        return False
    age = _latest_price_age_minutes(session, game.game_id, now)
    if age is None:
        return False
    return age <= float(settings.max_price_age_minutes)


def _side(name: Any) -> str:
    raw = str(name or "").strip().lower()
    if raw in {"yes", "over", "under", "no"}:
        return raw
    return raw[:16] or "yes"


# Stored school names that omit a word the odds feed still uses.
_SCHOOL_ALIASES = {
    "appstate": "appalachianstate",
    "samhouston": "samhoustonstate",
}


def _game_has_price(session: Session, game_id: str) -> bool:
    return (
        session.query(OddsTick.tick_id)
        .join(Outcome, Outcome.outcome_id == OddsTick.outcome_id)
        .filter(Outcome.game_id == game_id)
        .first()
        is not None
    )


def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _names_compatible(left: str, right: str) -> bool:
    """School names from the feed include the mascot. Stored names often do not."""
    a, b = _SCHOOL_ALIASES.get(_norm(left), _norm(left)), _SCHOOL_ALIASES.get(_norm(right), _norm(right))
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < 3 or not long.startswith(short):
        return False
    rest = long[len(short) :]
    # Keep Ohio State, Texas A&M, and Miami (OH) off the shorter school.
    if rest.startswith(("state", "am", "oh")):
        return False
    return True


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
