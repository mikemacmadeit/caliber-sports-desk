"""Build simulation inputs from model output and previously observed NFL plays."""
from datetime import UTC, datetime, timedelta
from functools import lru_cache
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from spe.db.base import as_utc
from spe.models.player_td.scorer_eligibility import exclusions


class InputsUnavailable(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


COLUMNS = ['game_id', 'game_date', 'play_id', 'posteam', 'defteam', 'fixed_drive',
               'play_type', 'rush_attempt', 'pass_attempt', 'qb_kneel', 'qb_spike',
               'score_differential', 'rusher_player_id', 'receiver_player_id']


@lru_cache(maxsize=2)
def _source(path, modified):
    if Path(path).suffix == '.json':
        frame = pd.DataFrame(json.loads(Path(path).read_text())['records'])[COLUMNS]
    else:
        frame = pd.read_parquet(path, columns=COLUMNS)
    frame = frame.drop_duplicates(['game_id', 'play_id'])
    return frame, hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_inputs(game, candidates, cutoff, source_path, *, historical_replay=False):
    """No current-game data, no network, and no changes to candidate inputs."""
    cutoff = as_utc(cutoff)
    if cutoff >= as_utc(game['kickoff']):
        raise InputsUnavailable('Simulation inputs must stop before kickoff')
    path = Path(source_path)
    if not path.exists():
        raise InputsUnavailable('Cached NFL play-by-play is unavailable')
    modified = path.stat().st_mtime_ns
    if not historical_replay and datetime.fromtimestamp(modified / 1e9, UTC) > cutoff:
        raise InputsUnavailable('Play-by-play cache was observed after the prediction cutoff')
    frame, source_hash = _source(str(path.resolve()), modified)
    # Retain the same conservative date-only lag used in the two experiments.
    historical = pd.to_datetime(frame.game_date, utc=True) + pd.Timedelta(days=1)
    frame = frame[historical < pd.Timestamp(cutoff - timedelta(hours=48))]
    frame = frame[(frame.game_id != game['game_id'].removeprefix('nfl:')) &
                  frame.play_type.isin(['run', 'pass']) &
                  (frame.qb_kneel.fillna(0) != 1) & (frame.qb_spike.fillna(0) != 1)].copy()
    frame['state'] = np.where(frame.score_differential >= 8, 'leading',
                              np.where(frame.score_differential <= -8, 'trailing', 'close'))
    frame['rush'] = (frame.rush_attempt == 1) & frame.rusher_player_id.notna()
    frame['rec'] = (frame.pass_attempt == 1) & frame.receiver_player_id.notna()
    frame = frame[frame.rush | frame.rec]
    candidates = [r for r in candidates if not exclusions(r)]
    teams, pace_samples = {}, []

    def last_six(column, team):
        subset = frame[frame[column] == team]
        games = subset[['game_id', 'game_date']].drop_duplicates().sort_values(
            ['game_date', 'game_id']).tail(6).game_id.tolist()
        return subset[subset.game_id.isin(games)], games

    for tid in (game['away'], game['home']):
        opponent = game['home'] if tid == game['away'] else game['away']
        rows = [r for r in candidates if r['team'] == tid]
        if not rows:
            raise InputsUnavailable('No eligible candidates for ' + tid)
        own, offense_games = last_six('posteam', tid)
        allowed, defense_games = last_six('defteam', opponent)
        if min(len(offense_games), len(defense_games)) < 2:
            raise InputsUnavailable('Insufficient historical game-flow data for ' + tid)
        drives = own.groupby('game_id').fixed_drive.nunique().to_numpy(dtype=float)
        opponent_drives = allowed.groupby('game_id').fixed_drive.nunique().to_numpy(dtype=float)
        if min(drives.min(), opponent_drives.min()) <= 0:
            raise InputsUnavailable('Historical possession counts are unavailable')
        possessions = float((drives.mean() + opponent_drives.mean()) / 2)
        pace_samples.extend(drives.tolist() + opponent_drives.tolist())
        players = [{'id':r['player_id'], 'name':r['player_name'], 'position':r['position']} for r in rows]
        totals = rows[0]['desk_analysis']['combination'].get('team_expected_tds')
        if not totals or any(not np.isfinite(totals.get(ch, 0)) or totals.get(ch, 0) <= 0 for ch in ('rush', 'rec')):
            raise InputsUnavailable('Team touchdown budgets are unavailable')
        usage = {p['id']:{} for p in players}
        usage_totals, scorer_shares = {}, {}
        for channel, field in (('rush', 'rusher_player_id'), ('rec', 'receiver_player_id')):
            plays = own[own[channel]]

            def counts(part):
                return {'total':len(part), 'latest':int((part.game_id == offense_games[-1]).sum()),
                        'states':{state:int((part.state == state).sum()) for state in ('close', 'leading', 'trailing')}}

            usage_totals[channel] = counts(plays)
            for player in players:
                usage[player['id']][channel] = counts(plays[plays[field] == player['id'].removeprefix('nfl:')])
            weights = []
            for row in rows:
                paths = row['desk_analysis']['combination'].get('scoring_paths') or {}
                if paths.get('status') != 'active' or paths.get(channel + '_lambda') is None:
                    raise InputsUnavailable('Current model has incomplete scoring paths')
                weights.append(paths[channel + '_lambda'] / totals[channel])
            if any(not np.isfinite(w) or w < 0 for w in weights) or sum(weights) > 1.00001:
                raise InputsUnavailable('Invalid allocation of team touchdowns')
            scorer_shares[channel] = weights + [max(0, 1 - sum(weights))]
        points = (rows[0].get('market_context') or {}).get(
            'away_implied_points' if tid == game['away'] else 'home_implied_points')
        if not isinstance(points, (int, float)) or not np.isfinite(points) or points <= 0:
            raise InputsUnavailable('Pregame team scoring expectations are unavailable')
        teams[tid] = {'players':players, 'expected_tds':totals, 'scorer_shares':scorer_shares,
                      'usage':usage, 'usage_totals':usage_totals, 'expected_possessions':possessions,
                      'measured_drive_count':int(drives.sum()), 'implied_points':points,
                      'offense_games':offense_games, 'defense_games':defense_games}
    return {'game':game, 'information_cutoff':cutoff.isoformat(), 'teams':teams,
            'possession_samples':pace_samples, 'source_pbp_sha256':source_hash,
            'source_observed_at':datetime.fromtimestamp(modified / 1e9, UTC).isoformat(),
            **({'reconstructed_historical_inputs':True,
                'timing_limitation':'Revised plays retrieved later; prior-game availability assumed after 48 hours.'} if historical_replay else {})}
