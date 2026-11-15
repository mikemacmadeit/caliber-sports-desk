"""Dated combined-model replay, separate from the immutable pregame Desk ledger."""
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
import json
import math

from sqlalchemy import select

from spe.db.base import as_utc
from spe.config import settings
from spe.db.models import BoardSnapshot, Game, InjuryEvent, OddsTick, Outcome, Player, PlayerGameStat, PublishedStat, Team
from spe.integrity.assignments import canonical_team_map
from spe.market.odds import american_to_implied
from spe.models.player_td.desk_combined import AllocationHistory, INACTIVE, combined_prediction, load_combined_model
from spe.models.player_td.desk_live import load_history
from spe.models.player_td.desk_opportunity import OpportunityHistory, canonical_team, prepare_history
from spe.models.player_td.desk_market import context_for_game, market_features, market_pick_eligible

ZONE = ZoneInfo("America/Chicago")
SELECTION_VERSION = "market_scorer_v3"
CACHE_PREFIX = "combined-replay-v4-free:"
DAY_CARD_PREFIX = "atd-desk-card-v1:"
MIN_SCORING_CHANCE = .40
MAX_CHANCE_SACRIFICE = .10
MOST_EXPENSIVE_PREFERRED_PRICE = -200


def revised_policy_requested(date):
    # The scorer-first Desk became the standing policy on this release date.
    # A one-day research override must not silently revert future days to the
    # older price/availability gate and produce an empty board.
    if date >= '2026-11-14':
        return True
    path = settings.parquet_dir / "models" / "desk_replay_policy_dates.json"
    return date in (json.loads(path.read_text()) if path.exists() else [])


def requested_retrain(date, league):
    path = settings.parquet_dir / 'models' / 'desk_retrain_runs.json'
    config=json.loads(path.read_text()) if path.exists() else {}
    dated=config.get(date, {}).get(league)
    if dated:
        return dated
    standing=config.get('research_defaults',{}).get(league)
    if standing and date>=standing.get('effective_from','9999-01-01'):
        return {**standing,'standing':True}
    local=config.get('local_defaults',{}).get(league)
    if settings.app_env=='dev' and local and date>=local.get('effective_from','9999-01-01'):
        return {**local,'standing_local':True}
    return None


def load_replay_model(date, league):
    """Date-scoped local research choice; never promotes the production artifact."""
    path = settings.parquet_dir / "models" / "desk_replay_overrides.json"
    overrides = json.loads(path.read_text()) if path.exists() else {}
    requested = overrides.get(date, {}).get(league)
    if not requested:
        return load_combined_model(league)
    options = {"version": requested["version"]} if requested.get("version") else {}
    model = load_combined_model(league, research_candidate=True, **options)
    if not model or model.get("artifact_sha256") != requested.get("artifact_sha256"):
        return None  # Never silently substitute another model for the requested run.
    return {**model, "user_requested_experiment": True}


def grade(db, game, player):
    """A touchdown already on the live box shows green. A miss waits until the final."""
    official = db.get(PublishedStat, f"{game.game_id}|{player}|anytime_td")
    source = str(official.source or "") if official is not None else ""
    scored_live = official is not None and (official.actual or 0) > 0 and source.startswith("espn.")
    if str(game.status or "") != "final":
        if scored_live:
            return "scored"
        return "pending"
    if source.startswith("espn.summary"):
        return "scored" if (official.actual or 0) > 0 else "blank"
    if scored_live:
        return "scored"
    stats = db.scalars(select(PlayerGameStat).where(
        PlayerGameStat.game_id == game.game_id, PlayerGameStat.player_id == player,
        PlayerGameStat.quality_status.notin_(("invalid", "quarantined")),
    )).all()
    if not stats:
        return "awaiting_box"
    # Prefer full official rows, then the newest revision within that provider.
    row = max(stats, key=lambda s: (s.provider in {"nflverse", "cfbd"}, s.revision or 0,
                                   as_utc(s.ingested_at) or datetime.min.replace(tzinfo=UTC)))
    return "scored" if (row.rush_td or 0) + (row.rec_td or 0) > 0 else "blank"


def day_card_key(date, league) -> str:
    return f"{DAY_CARD_PREFIX}{date}:{league or 'all'}"


def saved_day_card(db, date, league="all", *, now=None):
    """The picks already chosen for this day. Grades update; the names do not."""
    getter = getattr(db, "get", None)
    if getter is None:
        return None
    saved = getter(BoardSnapshot, day_card_key(date, league))
    payload = saved.payload if saved is not None else None
    if not payload or not payload.get("rows"):
        return None
    card = {**payload, "rows": [{**row} for row in payload["rows"]]}
    _refresh_card_grades(db, card)
    saved.payload = card
    saved.updated_at = datetime.now(UTC)
    return _withhold_current_absences(db, card, as_utc(now) or datetime.now(UTC))


def _withhold_current_absences(db, card, now):
    """Cheap pregame safety check; never rebuild or rewrite the saved selection."""
    from copy import deepcopy
    from spe.models.player_td.availability_status import resolved_reports
    upcoming = {r['game_id'] for r in card['rows'] if as_utc(r.get('kickoff_utc'))
        and as_utc(r['kickoff_utc']) > now and r.get('game_status') not in ('in_progress','final')}
    if not upcoming:
        return card
    reports = db.scalars(select(InjuryEvent).where(InjuryEvent.game_id.in_(upcoming),
        InjuryEvent.reported_at <= now, InjuryEvent.observed_at <= now,
        InjuryEvent.quality_status.notin_(('invalid','quarantined')))).all()
    absent = {gid:{pid for pid,r in resolved_reports([r for r in reports if r.game_id==gid]).items()
        if r['status'] in INACTIVE | {'doubtful'}} for gid in upcoming}
    response = deepcopy(card)
    kept = []
    unavailable = {r['game_id']:r for r in response.get('unavailable_games',[])}
    for row in response['rows']:
        blocked = absent.get(row['game_id'],set())
        if row.get('player_id') in blocked:
            unavailable[row['game_id']] = {'game_id':row['game_id'],
                'reason':'Saved pick withheld after a reported absence; replacement update queued.'}
            continue
        decision = row.get('decision') or {}
        alternative = decision.get('value_candidate') or {}
        if alternative.get('player_id') in blocked:
            decision.update(value_candidate=None, alternative_status='availability_update_pending',
                alternative_note='The saved alternative was reported unavailable; replacement update queued.')
        if decision.get('alternatives'):
            decision['alternatives'] = [r for r in decision['alternatives'] if r.get('player_id') not in blocked]
        kept.append(row)
    response['rows'] = kept
    response['unavailable_games'] = list(unavailable.values())
    return response


def _refresh_card_grades(db, card) -> None:
    ids = [row.get("game_id") for row in card["rows"] if row.get("game_id")]
    games = {
        game.game_id: game
        for game in db.scalars(select(Game).where(Game.game_id.in_(ids or ["__none__"])))
    }
    for row in card["rows"]:
        game = games.get(row.get("game_id"))
        if game is None or not row.get("player_id"):
            continue
        row["game_status"] = game.status
        row["result"] = grade(db, game, row["player_id"])
        row["final_score"] = (
            f"{game.away_score}–{game.home_score}"
            if str(game.status or "") == "final" and game.away_score is not None and game.home_score is not None
            else None
        )
        decision = row.get("decision") or {}
        results = {** (row.get("decision_results") or {})}
        for lane in ("most_likely", "value_candidate"):
            entry = decision.get(lane) or {}
            if not entry.get("player_id"):
                continue
            if lane == "value_candidate" and not (entry.get("research_selected") or entry.get("price_supported")):
                continue
            prior = {**(results.get(lane) or {})}
            prior["result"] = grade(db, game, entry["player_id"])
            results[lane] = prior
        if results:
            row["decision_results"] = results
    card["wins"] = sum(row.get("result") == "scored" for row in card["rows"])
    card["losses"] = sum(row.get("result") == "blank" for row in card["rows"])


def _store_day_card(db, payload) -> None:
    if not payload.get("rows") and not payload.get('unavailable_games'):
        return
    key = day_card_key(payload.get("date"), payload.get("league"))
    kept = {
        field: payload.get(field)
        for field in (
            "date", "league", "rows", "unavailable_games", "decision_record",
            "selection_version", "games", "retrospective", "wins", "losses", "note",
        )
    }
    now = datetime.now(UTC)
    saved = db.get(BoardSnapshot, key)
    if saved is None:
        db.add(BoardSnapshot(key=key, payload=kept, updated_at=now))
    else:
        saved.payload, saved.updated_at = kept, now
    if payload.get('league') == 'all':
        # A background refresh computes the slate once. Its league tabs must
        # receive the same run, rather than retaining older cached selections.
        unavailable = payload.get('unavailable_games') or []
        game_leagues = {g.game_id:g.league_id for g in db.scalars(select(Game).where(
            Game.game_id.in_([r['game_id'] for r in unavailable] or ['__none__'])))}
        for league in ('nfl','fbs'):
            rows = [r for r in payload['rows'] if r.get('league_id')==league]
            missing = [r for r in unavailable if game_leagues.get(r['game_id'])==league]
            _store_day_card(db, {**payload, 'league':league, 'rows':rows, 'unavailable_games':missing,
                'games':len(rows)+len(missing), 'decision_record':_decision_record(rows),
                'wins':sum(r.get('result')=='scored' for r in rows),
                'losses':sum(r.get('result')=='blank' for r in rows)})


def _decision_record(rows):
    result = {}
    for lane in ('most_likely', 'value_candidate'):
        recorded = [r['decision_results'][lane] for r in rows if r.get('retrospective') is False and lane in r.get('decision_results', {})]
        result[lane] = {'saved_pregame':len(recorded),
            'wins':sum(r['result']=='scored' for r in recorded),
            'losses':sum(r['result']=='blank' for r in recorded),
            'pending':sum(r['result'] not in ('scored','blank') for r in recorded),
            'priced_bets':sum(bool(r.get('bet_qualified')) for r in recorded),
            'hypothetical_unit_profit':sum(r['hypothetical_unit_profit'] for r in recorded if r.get('hypothetical_unit_profit') is not None)
                if lane=='value_candidate' and any(r.get('hypothetical_unit_profit') is not None for r in recorded) else None}
    return result


def select_replay_candidate(rows):
    """Scoring-first: affordable alternatives must stay close to the best scorer.

    These are explicit experimental selection preferences, not fitted probabilities
    or proof of price value. Results never enter ranking or eligibility.
    """
    if not rows:
        return None
    eligible = [r for r in rows if r["forecast"]["p_estimate"] >= MIN_SCORING_CHANCE
                and (r.get("selection_safety") is None or r["selection_safety"].get("eligible") is True)
                and r.get("book_price") is not None and abs(r["book_price"]) >= 100
                and market_pick_eligible(r)]
    if not eligible:
        return None
    best_chance = max(r["forecast"]["p_estimate"] for r in eligible)
    if any(r.get("selection_safety") for r in eligible):
        from spe.models.player_td.selection_safety import POLICY_VERSION
        chosen=max(eligible,key=lambda r:(r['forecast']['p_estimate'],r['selection_safety']['estimated_edge'],r['player_id']))
        return {**chosen,'selection_version':POLICY_VERSION,'replay_selection':'confirmed_role_value',
                'selection_reason':'highest_probability_with_confirmed_role_and_positive_edge','best_candidate_probability':best_chance}
    affordable = [r for r in eligible if r.get("book_price") is not None
                  and abs(r["book_price"]) >= 100
                  and r["book_price"] >= MOST_EXPENSIVE_PREFERRED_PRICE
                  and best_chance - r["forecast"]["p_estimate"] <= MAX_CHANCE_SACRIFICE + 1e-12]
    candidates = affordable or eligible
    chosen = max(candidates, key=lambda r: (r["forecast"]["p_estimate"],
                 r["forecast"]["p_estimate"] - american_to_implied(r["book_price"]) if r.get("book_price") else -1,
                 r["player_id"]))
    return {**chosen, "replay_selection": "balanced_scorer", "selection_version": SELECTION_VERSION,
            "selection_reason": "competitive_affordable_scorer" if affordable else "highest_chance_no_close_affordable_alternative",
            "best_candidate_probability": best_chance,
            "selection_policy": {"minimum_probability": MIN_SCORING_CHANCE,
                "minimum_team_implied_points": 21.0, "requires_player_price": True,
                "maximum_probability_sacrifice": MAX_CHANCE_SACRIFICE,
                "preferred_price_floor": MOST_EXPENSIVE_PREFERRED_PRICE}}


def select_game_scorer(rows, *, value_aware=False):
    """Name the most likely scorer on a team the market expects to score.

    A player whose team is under 21 implied points stays off when the other
    side has an eligible player. Betting and data checks remain visible warnings.
    """
    from spe.models.player_td.desk_market import MIN_TEAM_IMPLIED_POINTS
    from spe.models.player_td.scorer_eligibility import exclusions
    eligible = [row for row in rows if not exclusions(row)]
    if not eligible:
        return None
    scoring_side = [row for row in eligible if _team_expected_to_score(row, MIN_TEAM_IMPLIED_POINTS)]
    if scoring_side and len(scoring_side) < len(eligible):
        eligible = scoring_side
    # Saturday's names under 40% scored once in nine tries. A pass beats that group.
    eligible = [row for row in eligible if _chance(row) >= MIN_SCORING_CHANCE]
    if not eligible:
        return None
    chosen = max(eligible, key=lambda r: (_chance(r), r["player_id"]))
    value_policy = None
    if value_aware:
        from spe.models.player_td.scorer_value import choose
        chosen,value_policy = choose(eligible)
    from spe.models.player_td.selection_safety import BLOCKER_LABELS
    warnings = [BLOCKER_LABELS.get(b, b) for b in (chosen.get('selection_safety') or {}).get('blockers', [])]
    availability = (((chosen.get('desk_analysis') or {}).get('current_context') or {}).get('availability') or {}).get('status')
    if availability not in ('available', 'active', 'healthy'):
        warnings.append('Playing status is not confirmed; a historical role is not a game-day active list')
    return {**chosen, 'replay_selection': 'scorer_only', 'selection_version': 'matchup_value_scorer_v3' if value_policy else 'highest_chance_scorer_v2',
            'selection_reason': 'highest_estimated_scoring_chance_on_a_team_expected_to_score',
            'selection_warnings': warnings,
            **({'value_selection':value_policy,'selection_reason':value_policy['reason']} if value_policy else {}),
            'best_candidate_probability': chosen['forecast']['p_estimate']}


def _chance(row) -> float:
    value = (row.get("forecast") or {}).get("p_estimate")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0.0
    return float(value)


def _team_expected_to_score(row, floor):
    points = row.get('team_implied_points')
    return isinstance(points, (int, float)) and math.isfinite(points) and points >= floor


def replay_day(db, date, league="all", now=None, *, cached_only=False):
    now = as_utc(now) or datetime.now(UTC)
    revised = revised_policy_requested(date)
    start = datetime.fromisoformat(date).replace(tzinfo=ZONE)
    end = start + timedelta(days=1)
    games = db.scalars(select(Game).where(Game.kickoff_utc >= start.astimezone(UTC),
        Game.kickoff_utc < end.astimezone(UTC), Game.data_tier.notin_(("demo", "alias")),
        Game.league_id.in_(("nfl", "fbs") if league == "all" else (league,)),
        Game.status.notin_(("canceled", "cancelled", "postponed")))).all()
    teams = {t.team_id: t for t in db.scalars(select(Team))}
    aliases = canonical_team_map(list(teams.values()))
    unique = {}
    for game in sorted(games, key=lambda g: (g.game_id.startswith("espn:"), g.game_id)):
        key = (game.league_id, canonical_team(game.home_team_id, aliases), canonical_team(game.away_team_id, aliases))
        unique[key] = game
    games = list(unique.values())
    if not cached_only:
        from spe.desk.day_results import refresh_live_touchdowns
        refresh_live_touchdowns(db, games)
    players = {} if cached_only else {p.player_id: p for p in db.scalars(select(Player))}
    histories, models = {}, {}
    rows, unavailable = [], []
    for game in sorted(games, key=lambda g: (g.kickoff_utc, g.game_id)):
        lg = game.league_id
        if lg not in models:
            models[lg] = load_replay_model(date, lg)
        kick = as_utc(game.kickoff_utc)
        guarded_key = ("scorer-desk-v1:" if revised else "guarded-desk-v1:") + game.game_id
        guarded_saved = db.get(BoardSnapshot, guarded_key)
        if guarded_saved is None and kick <= now:
            # Policy changes apply prospectively. Keep a previously captured
            # pregame decision under either policy's key once its game starts.
            alternate = ('guarded-desk-v1:' if revised else 'scorer-desk-v1:') + game.game_id
            previous = db.get(BoardSnapshot, alternate)
            if previous is not None and (previous.payload or {}).get('evaluated'):
                guarded_key, guarded_saved = alternate, previous
        prospective = kick > now or guarded_saved is not None
        model = load_combined_model(lg) if prospective or revised else models[lg]
        retrain = requested_retrain(date, lg)
        reconstructed = bool(retrain and retrain.get('reconstruct_public_pregame'))
        if retrain:
            model = load_combined_model(lg, research_candidate=not retrain.get('use_validated_model', False), version=retrain['version'])
            if model and model.get('artifact_sha256') != retrain['artifact_sha256']:
                model = None
            if model:
                model = {**model, 'user_requested_experiment': True}
            prospective = kick > now
        cutoff = min(now, kick - timedelta(seconds=1))
        if not model or cutoff < as_utc(model.get("validation_end", "1900-01-01")):
            unavailable.append({"game_id": game.game_id, "reason": "No combined model available for this date."})
            continue
        cache_key = guarded_key if prospective else f"{CACHE_PREFIX}{game.game_id}:{model['artifact_sha256']}"
        if revised and not prospective:
            cache_key = f"scorer-replay-v1:{game.game_id}:{model['artifact_sha256']}"
        if retrain:
            cache_key = guarded_key if retrain.get('standing_local') or retrain.get('standing') else f"retrained-desk-v1:{game.game_id}:{model['artifact_sha256']}"
        if reconstructed:
            cache_key = f"corrected-inputs-v1:{game.game_id}:{model['artifact_sha256']}:{retrain.get('input_revision', 1)}"
        saved = db.get(BoardSnapshot, cache_key)
        chosen = (saved.payload or {}).get("row") if saved and (kick <= now or cached_only) else None
        if cached_only and not (saved and (saved.payload or {}).get('evaluated')):
            unavailable.append({'game_id':game.game_id, 'reason':'No saved pick for this model yet; update queued.'})
            continue
        if cached_only and chosen and kick > now:
            # Saved probabilities can load without a full model run. A new out
            # report or documented backup role still hides an unsafe old pick.
            from spe.models.player_td.desk_evidence import current_context
            from spe.models.player_td.scorer_eligibility import exclusions
            tid = game.home_team_id if chosen.get('team') == chosen.get('home_abbr') else game.away_team_id
            latest = current_context(db, game.game_id, chosen['player_id'], now,
                team_id=canonical_team(tid, aliases))
            stored_analysis = chosen.get('desk_analysis') or {}
            stored_context = stored_analysis.get('current_context') or {}
            check = {**chosen, 'desk_analysis':{**stored_analysis, 'current_context':{
                **stored_context, **{k:v for k,v in latest.items() if k in ('availability','depth') and v is not None}}}}
            blocked = exclusions(check)
            if blocked:
                unavailable.append({'game_id':game.game_id, 'reason':'Saved pick withheld pending replacement: '+'; '.join(blocked)})
                continue
            alternative = (chosen.get('decision') or {}).get('value_candidate')
            if alternative:
                alt_tid = game.home_team_id if alternative.get('team') == chosen.get('home_abbr') else game.away_team_id
                alt_latest = current_context(db, game.game_id, alternative['player_id'], now,
                    team_id=canonical_team(alt_tid, aliases))
                inputs = alternative.get('eligibility_inputs') or {}
                alt_context = {**inputs.get('current_context', {}),
                    **{k:v for k,v in alt_latest.items() if k in ('availability','depth') and v is not None}}
                alt_blocked = exclusions({'position':alternative.get('position'),
                    'forecast':{'p_estimate':alternative['probability']},
                    'desk_analysis':{'features':inputs.get('features',{}),'current_context':alt_context}})
                if alt_blocked:
                    chosen = {**chosen, 'decision':{**chosen['decision'], 'value_candidate':None,
                        'alternative_status':'withheld_availability',
                        'alternative_note':'The saved alternative no longer passes the playing-status checks; a replacement is being reviewed.'}}
            if latest.get('availability'):
                chosen = {**chosen, 'selection_warnings':list(dict.fromkeys([
                    *chosen.get('selection_warnings', []),
                    f"Latest availability check: {latest['availability']['status']} ({now.isoformat()})"]))}
        if not cached_only and not (saved and kick <= now and (saved.payload or {}).get("evaluated")):
            # Features always stop before this game's kickoff, even when grading finals.
            season = cutoff.year if cutoff.month >= 7 else cutoff.year - 1
            history_key = (lg, season, prospective, None if reconstructed else cutoff)
            if history_key not in histories:
                season_end = cutoff.replace(year=season+1, month=6, day=30)
                loaded = load_history(db, lg, min(now, season_end), reconstructed=True) if reconstructed else load_history(db, lg, min(cutoff, season_end))
                history = OpportunityHistory(prepare_history(loaded, aliases))
                from spe.models.player_td.desk_evidence import load_evidence
                history.evidence = load_evidence(lg, replay=not prospective)
                histories[history_key] = (history, AllocationHistory(history))
            history, allocator = histories[history_key]
            context = context_for_game(db, game.game_id, cutoff, replay=kick <= now)
            from sqlalchemy import or_
            reports = db.scalars(select(InjuryEvent).where(InjuryEvent.game_id == game.game_id,
                InjuryEvent.reported_at <= cutoff, or_(InjuryEvent.observed_at <= cutoff, InjuryEvent.provider == 'conference_public') if reconstructed else InjuryEvent.observed_at <= cutoff,
                InjuryEvent.quality_status.notin_(("invalid", "quarantined"))).order_by(InjuryEvent.reported_at)).all()
            from spe.models.player_td.availability_status import resolved_reports
            status = {p: r['status'] for p,r in resolved_reports(reports).items()}
            excluded = {p for p, s in status.items() if s in INACTIVE}
            offers = {}
            ticks = db.execute(select(Outcome.player_id, OddsTick).join(OddsTick, OddsTick.outcome_id == Outcome.outcome_id).where(
                Outcome.game_id == game.game_id, Outcome.market_id == "anytime_td", OddsTick.is_live.is_(False),
                OddsTick.captured_at <= cutoff, OddsTick.observed_at <= cutoff,
                OddsTick.captured_at >= cutoff - timedelta(days=7),
                OddsTick.quality_status.notin_(("invalid", "quarantined"))).order_by(OddsTick.captured_at)).all()
            per_book = {}
            for pid, tick in ticks:
                if abs(tick.price_american) >= 100 and (not tick.source_at or as_utc(tick.source_at) <= cutoff):
                    per_book[(pid, tick.bookmaker_id)] = tick
            for (pid, _), tick in per_book.items():
                if pid not in offers or american_to_implied(tick.price_american) < american_to_implied(offers[pid].price_american):
                    offers[pid] = tick
            candidates = []
            personnel = None
            if prospective and lg == 'nfl' and retrain and retrain.get('simulation', {}).get('weekly_context'):
                from spe.models.player_td.weekly_context import capture_personnel
                personnel = capture_personnel(db, game, cutoff)
            from spe.models.player_td.scouting_report import capture_matchup, capture_personnel, model_fields, build_report
            fitted_fields = model_fields(model)
            for team_id, opponent_id, home in ((game.home_team_id, game.away_team_id, True), (game.away_team_id, game.home_team_id, False)):
                team, opponent = canonical_team(team_id, aliases), canonical_team(opponent_id, aliases)
                from spe.models.player_td.candidate_universe import team_candidates
                from spe.models.player_td.desk_evidence import current_context
                from spe.models.player_td.scorer_eligibility import exclusions
                team_rows=list(team_candidates(history, allocator, team, opponent, cutoff, home))
                supplements = {}
                if lg == 'nfl':
                    from spe.models.player_td.nflverse_context import supplemental_context
                    supplements = supplemental_context(db, [r['player_id'] for r in team_rows], cutoff, team_id=team)
                current_by_player={}
                team_excluded=set(excluded)
                for candidate in team_rows:
                    pid=candidate['player_id']
                    current_by_player[pid]=current_context(db,game.game_id,pid,cutoff,reconstructed=reconstructed,
                        team_id=team,nfl_supplement=supplements.get(pid,{}))
                    if exclusions({'position':candidate['position'],'forecast':{'p_estimate':.5},
                        'desk_analysis':{'features':candidate['features'],'current_context':current_by_player[pid]}}):
                        team_excluded.add(pid)
                starters = [r['player_id'] for r in team_rows if r['position']=='QB' and r['player_id'] not in team_excluded
                    and (current_by_player[r['player_id']].get('depth') or {}).get('rank')==1]
                allocation_context = {**context, **({'starting_qb_id':starters[0]} if len(starters)==1 else {})}
                research_model = None
                if lg=='nfl' and prospective and not model.get('scoring_paths'):
                    research_model=load_combined_model('nfl',research_candidate=True,version='desk_combined_v11')
                allocation_model = {**model,'scoring_paths':research_model['scoring_paths']} if research_model and research_model.get('scoring_paths') else model
                allocation = allocator.allocate(allocation_model, team, opponent, cutoff, home, team_excluded, market=allocation_context)
                if not allocation:
                    continue
                scouting_inputs = capture_matchup(history, team, opponent, cutoff)
                vacated_workload = capture_personnel(history, team, cutoff, status, players)
                for candidate in team_rows:
                    pid = candidate['player_id']
                    if pid not in allocation['players']:
                        continue
                    player = players.get(pid)
                    if not player or status.get(pid) in INACTIVE | ({"doubtful"} if revised else {"doubtful", "questionable", "uncertain"}):
                        continue
                    f = candidate['features']
                    f.update(market_features(context, home, model.get("market_total_trained", True)))
                    combined = combined_prediction(model, f, allocation, pid)
                    if research_model and research_model.get('scoring_paths'):
                        from spe.models.player_td.matchup_research import attach_unfitted_research
                        combined=attach_unfitted_research(combined,allocation,pid,research_model['artifact_sha256'])
                    qb = ((combined.get('scoring_paths') or {}).get('research') or {}).get('quarterback')
                    if qb and players.get(qb.get('player_id')):
                        qb['name'] = players[qb['player_id']].name
                    from spe.models.player_td.desk_evidence import explanations, current_context
                    evidence_reasons, evidence_risks = explanations(f)
                    if reconstructed:
                        evidence_risks.append('Reconstructed pregame research: uses prior-game boxes and officially published pre-kickoff reports collected later. Not information our system demonstrably held before kickoff.')
                    if model.get('role_features'):
                        evidence_reasons.append('Retrained role model uses the last two observed team games, workload shares and favored-team role interactions.')
                    if model.get("user_requested_experiment") and not reconstructed:
                        evidence_risks.append('User-requested retrained replay; original pregame record is separate.' if model.get('live_enabled') else "Experimental expanded model selected for this day's replay; it did not pass the historical validation gate.")
                    quote = offers.get(pid)
                    candidates.append({"game_id": game.game_id, "league_id": lg, "player_id": pid,
                        "player_name": player.name, "position": candidate['position'],
                        "outcome_id": f"replay:{game.game_id}:{pid}", "market_id": "anytime_td", "side": "yes", "line": None,
                        "team": teams[team_id].abbreviation, "opponent": teams[opponent_id].abbreviation,
                        "home_abbr": teams[game.home_team_id].abbreviation, "away_abbr": teams[game.away_team_id].abbreviation,
                        "kickoff_utc": kick.isoformat(), "book_price": quote.price_american if quote else None,
                        "bookmaker": quote.bookmaker_id if quote else None,
                        "source_timestamp": as_utc(quote.captured_at).isoformat() if quote else None,
                        "price_source_at": as_utc(quote.source_at).isoformat() if quote and quote.source_at else None,
                        "market_context": context, "team_implied_points": context.get("home_implied_points" if home else "away_implied_points"),
                        "forecast": {"p_estimate": combined["probability"], "model_version": model["version"] + "-" + lg + "-" + model["artifact_sha256"][:10],
                            "research_only": True, "play_enabled": False, "prediction_as_of": now.isoformat(), "data_cutoff": cutoff.isoformat()},
                        "desk_analysis": {"combination": combined, "combination_status": "active",
                            "scouting_inputs": scouting_inputs, "vacated_workload": vacated_workload,
                            "fitted_feature_names": fitted_fields,
                            "market_total_active": bool(model.get('market_total_trained') or model.get('scorer_model',{}).get('uses_market_totals')),
                            "features": f, "matchup_features_active": bool(model.get("rich_features")),
                            "free_opportunities_active": bool(model.get("free_opportunities") or model.get('scorer_model') or model.get('player_matchups')),
                            "player_matchups_active": bool(model.get('player_matchups')),
                            "situational_features_active": bool(model.get("situational_features") or model.get('scorer_model')),
                            "reasons": evidence_reasons, "risks": evidence_risks,
                            "current_context": current_by_player[pid],
                            "sources": ["Stored prior-game play-by-play", "Prior-game player box scores"] +
                                (["SportsDataverse ESPN play-by-play"] if lg == 'fbs' and (f.get("rush_pbp_known") or f.get("target_pbp_known")) else []) +
                                (["nflverse play-by-play"] if lg == 'nfl' and (f.get('pbp_games') or f.get('offense_evidence_known')) else []) +
                                (["PFR snap counts via nflverse"] if lg == 'nfl' and f.get('snap_games') else []),
                            "evidence_timing": "Revised historical plays; availability assumed 48 hours after prior games."},
                        "retrospective": not prospective, "generated_at": now.isoformat()})
            simulation_run = None
            if personnel is not None:
                for candidate in candidates:
                    candidate['desk_analysis']['current_context']['personnel'] = personnel
            if prospective and lg == 'nfl' and retrain and retrain.get('simulation'):
                from spe.models.player_td.gameflow_stage import apply
                candidates, simulation_run = apply(candidates, model, game, cutoff, retrain['simulation'])
            if prospective or revised:
                from spe.models.player_td.selection_safety import assess
                for candidate in candidates:
                    candidate['selection_safety']=assess(candidate,cutoff,bool(model.get('live_enabled')))
            if prospective or model.get('decision_engine', '').startswith('scoring_case_'):
                from spe.models.player_td.scoring_decision import choose
                chosen = choose(candidates)
            else:
                chosen = select_game_scorer(candidates, value_aware=bool(model.get('player_matchups'))) if revised else select_replay_candidate(candidates)
            if chosen:
                chosen = {**chosen, 'desk_analysis': {**chosen['desk_analysis'],
                    'scouting_report': build_report(chosen, candidates)}}
            challenger = None
            if now < kick:
                from spe.models.player_td.scorer_shadow import compare
                challenger = compare(candidates, chosen, model, cutoff)
                if chosen and challenger:
                    chosen = {**chosen, 'challenger_comparison':challenger}
            from spe.models.player_td.pregame_candidates import save_candidates
            save_candidates(db, game, candidates, chosen, now, model['artifact_sha256'], challenger,
                            simulation=simulation_run)
            from spe.models.player_td.selection_safety import POLICY_VERSION
            payload = {"row": chosen, "evaluated": True, "selection_version": SELECTION_VERSION,
                       "model_version": model['version'], "model_artifact_sha256": model['artifact_sha256'],
                       "selection_blockers": sorted({b for c in candidates for b in (c.get('selection_safety') or {}).get('blockers',[])}),
                       "game_id": game.game_id, "kickoff_utc": kick.isoformat(), "generated_at": now.isoformat()}
            if prospective or revised:
                payload['selection_version']=POLICY_VERSION
            if revised:
                payload['selection_version']=(chosen or {}).get('selection_version','highest_chance_scorer_v2')
            if saved is None:
                saved = BoardSnapshot(key=cache_key, payload=payload, updated_at=now)
                db.add(saved)
            else:
                saved.payload, saved.updated_at = payload, now
        if not chosen:
            reason = "Pass: requires a priced scorer at 40%+, usable spread and total, team implied points of 21+, and sufficient role history."
            if prospective or revised:
                detail=(saved.payload or {}).get('selection_blockers',[]) if saved else payload.get('selection_blockers',[])
                from spe.models.player_td.selection_safety import BLOCKER_LABELS
                reason="Pass: no candidate meets the probability, price, availability and role requirements. " + '; '.join(BLOCKER_LABELS.get(b,b.replace('_',' ')) for b in detail)
            unavailable.append({"game_id": game.game_id, "reason": reason})
            continue
        result = grade(db, game, chosen["player_id"])
        from spe.models.player_td.scorer_readiness import readiness
        analysis = chosen.get('desk_analysis') or {}
        if not analysis.get('scouting_report'):
            from spe.models.player_td.scouting_report import build_report
            # Response-only explanation. Never alter a started game's snapshot
            # or query later observations to fill gaps in its saved prediction.
            analysis = {**analysis, 'scouting_report': build_report(chosen, frozen_inputs_only=True)}
        else:
            from spe.models.player_td.scouting_report import selection_context
            analysis = {**analysis, 'scouting_report': selection_context(analysis['scouting_report'], chosen)}
        if 'market_total_active' not in analysis:
            version = chosen.get('forecast',{}).get('model_version','')
            # These saved FBS artifacts were fitted before historical totals existed.
            if lg == 'fbs' and any(version.startswith('desk_combined_v'+v+'-') for v in ('2','3','4','5','6')):
                analysis = {**analysis, 'market_total_active':False}
        decision_results = {}
        if chosen.get('decision'):
            for lane in ('most_likely', 'value_candidate'):
                entry = chosen['decision'].get(lane)
                # A merely interesting point estimate is not a recorded value bet.
                if entry and (lane == 'most_likely' or entry.get('research_selected') or entry.get('price_supported')):
                    lane_result = grade(db, game, entry['player_id'])
                    price = entry.get('american')
                    profit = None
                    if lane == 'value_candidate' and entry.get('price_supported') and price and lane_result in ('scored', 'blank'):
                        profit = (price/100 if price > 0 else 100/abs(price)) if lane_result == 'scored' else -1.
                    decision_results[lane] = {'result': lane_result, 'hypothetical_unit_profit': profit,
                        'bet_qualified':bool(lane=='value_candidate' and entry.get('price_supported'))}
        rows.append({**chosen, "desk_analysis":analysis, "result": result, "game_status": game.status,
                     **({'decision_results':decision_results} if decision_results else {}),
                     "selection_readiness": readiness(chosen, as_utc(chosen.get('forecast',{}).get('data_cutoff')) or cutoff),
                     "final_score": f"{game.away_score}–{game.home_score}" if game.status == "final" and game.away_score is not None and game.home_score is not None else None})
    decision_record = _decision_record(rows)
    payload = {"date": date, "league": league, "rows": rows, "unavailable_games": unavailable,
            "decision_record": decision_record,
            "selection_version": "highest_chance_scorer_v2" if revised else SELECTION_VERSION,
            "games": len(games), "retrospective": True,
            "wins": sum(r["result"] == "scored" for r in rows), "losses": sum(r["result"] == "blank" for r in rows),
            "note": "Combined-model replay using prior-game inputs and recorded pregame prices. Recomputed picks are not the original pregame record."}
    _store_day_card(db, payload)
    return payload
