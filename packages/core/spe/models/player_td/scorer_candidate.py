"""Portable nonlinear scorer model. No player names, IDs, prices or outcomes as inputs.

Training exports small regression trees as JSON; serving requires only NumPy.
Missing observations have explicit indicators and training-only median fills.
"""
import math

import numpy as np

from spe.models.player_td.current_opportunities import FEATURES as CURRENT, transform
from spe.models.player_td.desk_evidence import FREE_FEATURES, RICH_FEATURES
from spe.models.player_td.desk_market import MARKET_FEATURES
from spe.models.player_td.desk_opportunity import FEATURES as BASE
from spe.models.player_td.role_features import ROLE_FEATURES, interactions
from spe.models.player_td.situational import SITUATIONAL_FEATURES

FEATURES = list(dict.fromkeys(BASE + MARKET_FEATURES + RICH_FEATURES + FREE_FEATURES
    + ROLE_FEATURES + CURRENT + SITUATIONAL_FEATURES + ['team_implied_points',
    'days_since_appearance', 'recent_role_observation_games']))


def vector(features):
    f = {**transform(features, 'channels'), **interactions(features)}
    # The embedded original model keeps its fitted total convention. The new
    # scorer receives actual market context via separate, explicitly named keys.
    total = f.get('context_game_total')
    spread = f.get('market_team_spread')
    f['market_game_total'] = total
    f['market_total_known'] = float(total is not None)
    f['team_implied_points'] = ((total-spread)/2 if total is not None
        and f.get('market_spread_known') else None)
    return np.array([float(f[k]) if f.get(k) is not None else np.nan for k in FEATURES])


def matrix(x, medians):
    x = np.asarray(x, dtype=float)
    missing = ~np.isfinite(x)
    return np.concatenate([np.where(missing, medians, x), missing.astype(float)], axis=-1).astype(np.float32)


def tree_logit(model, features):
    x = matrix(vector(features), np.array(model['medians']))
    value = model['intercept']
    for tree in model['trees']:
        node = 0
        while tree['left'][node] != -1:
            node = tree['left'][node] if x[tree['feature'][node]] <= tree['threshold'][node] else tree['right'][node]
        value += model['learning_rate'] * tree['value'][node]
    return value


def predict(model, features, baseline_raw_logit):
    score = tree_logit(model, features)
    blend = model['tree_weight']*score + (1-model['tree_weight'])*baseline_raw_logit
    z = model['calibration_intercept'] + model['calibration_slope']*blend
    return 1/(1+math.exp(-max(-25., min(25., z))))


def validate(model):
    """Fail closed for malformed, cyclic, nonfinite or unapproved artifacts."""
    if model['features'] != FEATURES or len(model['medians']) != len(FEATURES):
        return False
    values = list(model['medians']) + [model[k] for k in ('intercept', 'learning_rate',
        'tree_weight', 'calibration_intercept', 'calibration_slope')]
    if not (0 < model['tree_weight'] <= 1 and 0 < model['learning_rate'] <= 1
            and model['calibration_slope'] > 0 and 0 < len(model['trees']) <= 1000):
        return False
    for tree in model['trees']:
        n = len(tree['left'])
        if not n or n > 2047 or any(len(tree[k]) != n for k in ('right','feature','threshold','value')):
            return False
        for i, (left, right, feature) in enumerate(zip(tree['left'],tree['right'],tree['feature'])):
            if left == right == -1:
                continue
            if not (isinstance(left,int) and isinstance(right,int) and isinstance(feature,int)
                    and i < left < n and i < right < n and 0 <= feature < 2*len(FEATURES)):
                return False
        values += tree['threshold'] + tree['value']
    return all(isinstance(v,(int,float)) and math.isfinite(v) for v in values)
