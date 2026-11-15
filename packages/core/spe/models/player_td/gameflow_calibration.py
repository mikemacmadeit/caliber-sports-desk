"""Pinned, separately fitted calibration for the actual game-flow simulator."""
import hashlib
import json
import math
from pathlib import Path

from spe.config import settings
from spe.db.base import as_utc
from spe.models.player_td.desk_combined import logit, sigmoid
from spe.models.player_td.scoring_paths import calibrated

VERSION = 'td_gameflow_calibration_v1'


def load_calibration(config, model_sha, cutoff):
    fallback = {'status':'inherited_path_calibration', 'simulation_specific':False}
    if not config:
        return None, fallback
    try:
        name = config['file']
        if Path(name).name != name:
            raise ValueError('Calibration must be a model filename')
        body = (settings.parquet_dir/'models'/name).read_bytes()
        artifact = json.loads(body)
        if hashlib.sha256(body).hexdigest() != config['sha256']:
            raise ValueError('Calibration artifact hash mismatch')
        if artifact['version'] != VERSION or artifact['base_model_sha256'] != model_sha:
            raise ValueError('Calibration belongs to another model')
        if not artifact.get('promotion_gate_passed'):
            raise ValueError('Simulation calibration did not pass its validation gate')
        if as_utc(artifact['trained_at']) > as_utc(cutoff) or as_utc(artifact['evaluation_end']) >= as_utc(cutoff):
            raise ValueError('Calibration contains information after the cutoff')
        weights = artifact['coefficients']
        if len(weights)!=3 or not all(math.isfinite(x) for x in weights) or min(weights[1:])<0:
            raise ValueError('Invalid calibration coefficients')
        return artifact, {'status':'simulation_specific_retrospective', 'simulation_specific':True,
            'sha256':config['sha256'], 'validation':artifact['validation'],
            'note':'Fitted on reconstructed historical simulations; prospective validation remains necessary.'}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, {**fallback, 'requested_calibration_unavailable':str(exc)}


def probability(artifact, model, baseline, simulated):
    if artifact is None:
        return calibrated(model,baseline,simulated)
    w = artifact['coefficients']
    return float(sigmoid(w[0]+w[1]*logit(baseline)+w[2]*logit(simulated)))
