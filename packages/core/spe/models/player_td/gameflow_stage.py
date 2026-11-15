"""Prospective NFL model -> simulation -> existing calibration -> selection.

The simulation changes probabilities, not the published model's validation status.
No DB writes or source refresh occurs in this stage. Callers preserve the run record.
"""
from copy import deepcopy
from datetime import UTC, datetime
import logging

from spe.config import settings
from spe.db.base import as_utc
from spe.simulate.desk_gameflow import simulate
from spe.simulate.desk_gameflow_inputs import InputsUnavailable, build_inputs, digest

VERSION = 'desk_gameflow_v1'
VARIANTS = (('gameflow', {}), ('slower_game', {'pace':.85}), ('faster_game', {'pace':1.15}),
            ('stronger_game_script', {'script_strength':2.}), ('recent_rb_roles', {'recent_role':True}))
NOTE = ('Experimental model plus simulation. The existing probability calibration is reused; '
        'higher accuracy has not been established. Scenario ranges are not confidence intervals.')


def apply(candidates, model, game, cutoff, config):
    if not config or game.league_id != 'nfl' or as_utc(game.kickoff_utc) <= as_utc(cutoff) or game.status not in ('scheduled', 'preview', 'pregame'):
        return candidates, None
    if config.get('version') != VERSION:
        raise ValueError('Unsupported simulation configuration')
    # A model refresh always supplies unsimulated rows. Reject accidental repeats.
    if any(((r.get('desk_analysis') or {}).get('simulation') or {}).get('status') == 'active' for r in candidates):
        raise ValueError('Simulation was already applied to this candidate list')
    rows = deepcopy(candidates)
    draws = int(config.get('draws', 50000))
    seed = int(config.get('seed', 20261115))
    if not 1000 <= draws <= 100000:
        raise ValueError('Simulation draws must be between 1,000 and 100,000')
    base = {'version':VERSION, 'draws_per_scenario':draws, 'seed':seed,
            'information_cutoff':as_utc(cutoff).isoformat(), 'note':NOTE}
    try:
        if not model.get('scoring_paths'):
            raise InputsUnavailable('This model has no fitted scoring-path calibration')
        metadata = {'game_id':game.game_id, 'home':game.home_team_id.removeprefix('nfl:'),
                    'away':game.away_team_id.removeprefix('nfl:'), 'kickoff':as_utc(game.kickoff_utc).isoformat()}
        season = as_utc(cutoff).year - int(as_utc(cutoff).month < 7)
        source = settings.parquet_dir.resolve().parent / 'cache/nflverse' / f'play_by_play_{season}.parquet'
        inputs = build_inputs(metadata, rows, cutoff, source)
        # Same engine and fixed parameters as the two experiments. Controls are
        # covered by tests; only gameflow supplies the published point estimate.
        results = {name:simulate(inputs, draws=draws, seed=seed, **options) for name, options in VARIANTS}
        context_assessment = None
        if config.get('weekly_context'):
            from spe.models.player_td.weekly_context import scenarios, VERSION as CONTEXT_VERSION
            if config['weekly_context'] != CONTEXT_VERSION:
                raise ValueError('Unsupported weekly context configuration')
            extra, context_assessment = scenarios(inputs, rows)
            for name, scenario_inputs, options in extra:
                results[name] = simulate(scenario_inputs, draws=draws, seed=seed, **options)
        lookup = {name:{p['id']:p for p in run['players']} for name, run in results.items()}
        input_hash = digest({'inputs':inputs, 'model_sha256':model['artifact_sha256'],
                             'baseline':{r['player_id']:r['forecast']['p_estimate'] for r in rows},
                             'weekly_context':context_assessment})
        from spe.models.player_td.gameflow_calibration import load_calibration, probability as calibrate_simulation
        calibration, calibration_status = load_calibration(config.get('calibration'), model['artifact_sha256'], cutoff)
        for row in rows:
            pid = row['player_id']
            if pid not in lookup['gameflow']:
                row['desk_analysis']['simulation'] = {**base, 'status':'excluded', 'input_sha256':input_hash}
                continue
            analysis = row['desk_analysis']
            combined = analysis['combination']
            paths = combined['scoring_paths']
            baseline = row['forecast']['p_estimate']
            scenarios = {name:calibrate_simulation(calibration, model, paths['baseline_probability'], players[pid]['raw_probability'])
                         for name, players in lookup.items()}
            probability = scenarios['gameflow']
            raw = lookup['gameflow'][pid]
            low = min(paths.get('probability_low', baseline), baseline, *scenarios.values())
            high = max(paths.get('probability_high', baseline), baseline, *scenarios.values())
            analysis['simulation'] = {**base, 'status':'active', 'input_sha256':input_hash,
                'baseline_probability':baseline, 'raw_probability':raw['raw_probability'],
                'adjusted_probability':probability, 'scenario_probabilities':scenarios,
                'raw_monte_carlo_standard_error':raw['mc_se'], 'probability_low':low, 'probability_high':high,
                'total_simulated_games':draws * len(results), 'source_pbp_sha256':inputs['source_pbp_sha256'],
                'calibration':calibration_status,
                **({'weekly_context':{**context_assessment['players'][pid],
                    'weather_status':context_assessment['weather_status'],
                    'scenario_assumptions':context_assessment['scenario_assumptions'],
                    'point_estimate_effect':context_assessment['point_estimate_effect']}} if context_assessment else {})}
            combined.update(pre_simulation_probability=baseline, probability=probability, combined_probability=probability)
            paths.update(probability_low=low, probability_high=high)
            row['forecast'].update(p_estimate=probability, simulation_version=VERSION,
                                   baseline_probability=baseline, research_only=True, play_enabled=False)
            analysis['risks'] = list(dict.fromkeys([*analysis.get('risks', []), NOTE]))
        return rows, {**base, 'status':'active', 'input_sha256':input_hash, 'inputs':inputs,
                      'results':results, 'model_artifact_sha256':model['artifact_sha256'],
                      'completed_at':datetime.now(UTC).isoformat(), 'total_simulated_games':draws * len(results),
                      'weekly_context':context_assessment, 'calibration':calibration_status}
    except (InputsUnavailable, OSError, ValueError, KeyError) as exc:
        # Preserve the complete original forecast on missing data; never publish
        # half-updated rows or disguise a fallback as a simulated pick.
        logging.getLogger(__name__).warning('Simulation unavailable for %s: %s', game.game_id, exc)
        rows = deepcopy(candidates)
        failed = {**base, 'status':'unavailable', 'reason':str(exc), 'baseline_retained':True}
        for row in rows:
            row['desk_analysis']['simulation'] = deepcopy(failed)
            row['desk_analysis']['risks'] = [*row['desk_analysis'].get('risks', []),
                'Game-flow simulation unavailable; retained the original model estimate.']
        return rows, failed
