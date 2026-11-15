"""Activate the NFL simulation stage and rebuild only unstarted scorer picks."""
import argparse
from copy import deepcopy
from datetime import UTC, datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from spe.config import settings
from spe.db.base import as_utc
from spe.db.models import BoardSnapshot, Game
from spe.db.session import session_factory
from spe.models.player_td.desk_replay import replay_day, requested_retrain
from spe.models.player_td.gameflow_stage import VERSION
from spe.models.player_td.scorer_eligibility import exclusions
from spe.simulate.desk_gameflow_inputs import digest


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, default=str), encoding='utf-8')


def run(day, output, *, weekly_context=False, calibration=None):
    if output.exists():raise ValueError('Use a new output directory to preserve prior runs')
    output.mkdir(parents=True)
    now = datetime.now(UTC)
    config_path = settings.parquet_dir / 'models/desk_retrain_runs.json'
    original_config = config_path.read_bytes()
    config = json.loads(original_config)
    current = requested_retrain(day, 'nfl')
    if not current or current['version'] != 'desk_combined_v11':
        raise ValueError('Expected the current pinned NFL v11 model')
    with session_factory() as db:
        from spe.ingest.nfl_matchup_evidence import refresh as refresh_matchups
        refresh_matchups(db, now=now)
        games = [g for g in db.query(Game).filter(Game.league_id == 'nfl')
                 if as_utc(g.kickoff_utc).astimezone(ZoneInfo('America/Chicago')).date().isoformat() == day
                 and g.data_tier not in ('demo', 'alias')]
        future = {g.game_id for g in games if as_utc(g.kickoff_utc) > now and g.status in ('scheduled', 'preview', 'pregame')}
        begun = {g.game_id for g in games if as_utc(g.kickoff_utc) <= now or g.status in ('in_progress', 'final')}
        if not future:raise ValueError('No unstarted NFL games to update')
        snapshots = list(db.query(BoardSnapshot))
        protected = {s.key:digest(s.payload) for s in snapshots if any(gid in s.key for gid in begun)}
        originals = [{'key':s.key, 'payload':deepcopy(s.payload), 'updated_at':s.updated_at}
                     for s in snapshots if ':preserved:' not in s.key and
                     s.key.startswith(('td-pregame-candidates-v1:', 'retrained-desk-v1:', 'scorer-desk-v1:')) and
                     any(gid in s.key for gid in future)]
        before = replay_day(db, day, 'nfl', now, cached_only=True)
        dump(output/'before.json', before)
        dump(output/'original-snapshots.json', originals)
        dump(output/'protected-hashes.json', protected)
        dump(output/'prior-config.json', config)
        for item in originals:
            key = item['key'] + ':preserved:' + digest(item['payload'])[:16]
            if db.get(BoardSnapshot, key) is None:
                db.add(BoardSnapshot(key=key, payload=item['payload'], updated_at=item['updated_at']))
        db.commit()
    simulation = {'version':VERSION, 'draws':50000, 'seed':20261115}
    if weekly_context:
        from spe.models.player_td.weekly_context import VERSION as CONTEXT_VERSION
        simulation['weekly_context'] = CONTEXT_VERSION
    if calibration:
        from spe.models.player_td.gameflow_calibration import load_calibration
        pinned = json.loads(calibration.read_text())
        fitted, status = load_calibration(pinned, current['artifact_sha256'], now)
        if fitted is None:raise ValueError(status)
        simulation['calibration'] = pinned
    config.setdefault(day, {})['nfl'] = {**current, 'simulation':simulation}
    config.setdefault('local_defaults', {})['nfl'] = {**current, 'effective_from':day, 'simulation':simulation}
    config.setdefault('research_defaults', {})['nfl'] = {**current, 'effective_from':day, 'simulation':simulation}
    dump(config_path, config)
    try:
        with session_factory() as db:
            # Fresh model run, then simulations, then ranking and saved explanations.
            board = replay_day(db, day, 'nfl')
            updated = [r for r in board['rows'] if r['game_id'] in future]
            if {r['game_id'] for r in updated} != future:raise ValueError('Incomplete future NFL board')
            if any(exclusions(r) for r in updated):raise ValueError('Ineligible main pick')
            for row in updated:
                if row['desk_analysis'].get('simulation', {}).get('status') != 'active':
                    raise ValueError('Simulation did not run for ' + row['game_id'])
                if as_utc(row['kickoff_utc']) <= datetime.now(UTC):
                    raise ValueError('A game started during the rebuild; retry without rewriting it')
            db.flush()
            after_protected = {s.key:digest(s.payload) for s in db.query(BoardSnapshot) if any(gid in s.key for gid in begun)}
            if after_protected != protected:raise ValueError('A started-game snapshot changed; do not commit')
            db.commit()
            dump(output/'after.json', board)
            snapshots = [db.get(BoardSnapshot, 'td-pregame-candidates-v1:' + gid) for gid in sorted(future)]
            dump(output/'candidate-runs.json', [{'key':s.key, 'payload':s.payload} for s in snapshots])
        old = {r['game_id']:r for r in before['rows']}
        comparison = []
        for row in updated:
            previous = old.get(row['game_id'], {})
            alt = (row.get('decision') or {}).get('value_candidate') or {}
            old_alt = (previous.get('decision') or {}).get('value_candidate') or {}
            comparison.append({'game_id':row['game_id'], 'game':row['away_abbr']+' at '+row['home_abbr'],
                'main_before':previous.get('player_name'), 'main_after':row['player_name'],
                'alternative_before':old_alt.get('name'), 'alternative_after':alt.get('name'),
                'probability_before':previous.get('forecast', {}).get('p_estimate'),
                'probability_after':row['forecast']['p_estimate'], 'alternative_probability':alt.get('probability'),
                'simulation':row['desk_analysis']['simulation']})
        report = {'date':day, 'generated_at':datetime.now(UTC).isoformat(), 'games_updated':len(updated),
                  'started_snapshots_unchanged':len(protected), 'comparisons':comparison,
                  'changed_main':sum(x['main_before'] != x['main_after'] for x in comparison),
                  'changed_alternative':sum(x['alternative_before'] != x['alternative_after'] for x in comparison)}
        dump(output/'comparison.json', report)
        print(json.dumps({**report, 'comparisons':[{k:v for k,v in r.items() if k != 'simulation'} for r in comparison]}, indent=2))
    except Exception:
        config_path.write_bytes(original_config)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--weekly-context', action='store_true')
    parser.add_argument('--calibration-config', type=Path)
    args = parser.parse_args()
    run(args.date, args.output, weekly_context=args.weekly_context, calibration=args.calibration_config)
