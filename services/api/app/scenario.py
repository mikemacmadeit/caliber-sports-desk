"""Research scenarios start with the saved TD model, then vary team scoring."""
from datetime import UTC, datetime
import math
import threading

import numpy as np
from fastapi import HTTPException

from spe.db.base import as_utc
from spe.db.models import BoardSnapshot, InjuryEvent
from spe.models.player_td.scorer_eligibility import exclusions
from spe.models.player_td.availability_status import INACTIVE, resolved_reports
from spe.simulate.engine import SimulationResult

_slot = threading.BoundedSemaphore(1)


def simulate_saved(payload, home_abbr, away_abbr, league, draws, seed, overrides):
    context = payload.get("market_context") or {}
    home_base, away_base = context.get("home_implied_points"), context.get("away_implied_points")
    if not all(isinstance(n, (float, int)) and math.isfinite(n) and 0 < n < 100 for n in (home_base, away_base)):
        raise HTTPException(409, "Saved team scoring expectations are unavailable for this game.")
    candidates = [r for r in payload.get("candidates", []) if not exclusions(r)
                  and r.get("team") in {home_abbr, away_abbr}]
    if not candidates or len(candidates) > 80:
        raise HTTPException(409, "A usable saved candidate list is not available yet.")
    probs, names, events, scores = {}, {}, {}, {}
    # Shared team scoring uncertainty creates dependence. The rate transformation
    # preserves each saved marginal probability before the user's score adjustment.
    # This is a sensitivity experiment, not a newly calibrated production model.
    shape = 6.0
    for index, (side, team, baseline) in enumerate((("home", home_abbr, home_base), ("away", away_abbr, away_base))):
        rng = np.random.default_rng(np.random.SeedSequence([seed, index]))
        target = overrides.get(side + "_points", baseline)
        script = rng.gamma(shape, 1 / shape, draws)
        scores[side] = [float(v) for v in np.quantile(script * target, [.1, .25, .5, .75, .9])]
        for row in sorted((r for r in candidates if r['team'] == team), key=lambda r:r['player_id']):
            p = min(1 - 1e-9, row['forecast']['p_estimate'])
            rate = shape * ((1-p)**(-1/shape)-1)
            hit = rng.poisson(rate * (target / baseline) * script) > 0
            pid = row['player_id']
            events[pid], names[pid], probs[pid] = hit, row['player_name'], float(hit.mean())
    leaders = sorted(probs, key=probs.get, reverse=True)[:12]
    joint = {f"{a}|{b}":float(np.mean(events[a] & events[b]))
             for i,a in enumerate(leaders) for b in leaders[i+1:]}
    result = SimulationResult(seed=seed, draw_count=draws, league_id=league,
        research_only=True, overrides=overrides, player_td_prob=probs,
        player_rec_over={}, player_yards_over={}, score_quantiles=scores, joint_td=joint,
        convergence={"max_td_mc_se":max(math.sqrt(p*(1-p)/draws) for p in probs.values()),
                     "draw_count":float(draws), "target_se":.005})
    return result, names


def run_scenario(db, game, body):
    snapshot = db.get(BoardSnapshot, "td-pregame-candidates-v1:" + game.game_id)
    if not snapshot or not snapshot.payload:
        raise HTTPException(409, "This game needs a saved TD analysis before running a scenario.")
    payload = dict(snapshot.payload)
    capture, kick = as_utc(payload.get("captured_at")), as_utc(game.kickoff_utc)
    if capture is None or capture >= kick:
        raise HTTPException(409, "A verified pregame analysis is unavailable for this game.")
    if body.as_of and as_utc(body.as_of) != capture:
        raise HTTPException(422, "Scenarios use the saved analysis timestamp; arbitrary historical cutoffs are unsupported.")
    now = datetime.now(UTC)
    if now < kick:
        reports = db.query(InjuryEvent).filter(InjuryEvent.game_id == game.game_id,
            InjuryEvent.observed_at <= now, InjuryEvent.reported_at <= now,
            InjuryEvent.quality_status.notin_(("invalid", "quarantined"))).all()
        absent = {pid for pid, r in resolved_reports(reports).items() if r['status'] in INACTIVE | {'doubtful'}}
        payload['candidates'] = [r for r in payload.get('candidates', []) if r['player_id'] not in absent]
    if not _slot.acquire(blocking=False):
        raise HTTPException(429, "Another scenario is running. Try again shortly.", headers={"Retry-After":"5"})
    try:
        result, names = simulate_saved(payload, game.home_team.abbreviation, game.away_team.abbreviation,
            game.league_id, body.draw_count, body.seed, body.scenario_overrides)
        return result, names, {"engine":"saved_td_sensitivity_v1", "model_artifact_sha256":payload.get('model_artifact_sha256'),
            "captured_at":capture.isoformat(), "market_context":payload.get('market_context'),
            "limitation":"Research sensitivity to team scoring. Weather is not a fitted adjustment; no improvement in accuracy is claimed."}
    finally:
        _slot.release()
