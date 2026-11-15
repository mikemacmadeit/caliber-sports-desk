"""Fixed-design path allocator; read-only historical evaluation, no auto-promotion."""
import argparse
import copy
import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from scipy.optimize import minimize

from spe.analytics.nflverse_opportunities import pbp_context, snap_context
from spe.ingest.nflverse_historical import load_asset, cache_dir
from spe.integrity.assignments import canonical_team_map
from spe.models.player_td.desk_live import HISTORY_SQL
from spe.models.player_td.nflverse_context import attach_context
from spe.models.player_td.desk_opportunity import OpportunityHistory, prepare_history
from spe.models.player_td.desk_combined import AllocationHistory, combined_prediction, logit, sigmoid
from spe.models.player_td.desk_evidence import load_evidence
from spe.models.player_td.desk_market import market_features
from spe.models.player_td.scoring_paths import prior_rates, VERSION
from train_desk_combined import metrics, paired_brier_interval


def train(database, output, contextual=False):
    output.mkdir(parents=True, exist_ok=True)
    plan = {'version': 'desk_combined_v10', 'cutoff_exclusive': '2026-11-14',
        'fit_end': '2025-10-15', 'calibration_end': '2025-12-15', 'validation_end': '2026-02-15',
        'design': 'Fixed scoring-path shares and conversion shrinkage; inherited v9 team/player components. Fit two nonnegative component logit coefficients on calibration period only.',
        'gate': 'Strictly more held-out game hits and no worse Brier/log loss than BOTH v9 and deployed v2 on matching player-games; never auto-promote.',
        'limitations': ['Revised historical PBP, assumed prior-game availability after 48 hours.',
            'Observed participant bias; historical complete DNP and injury snapshots unavailable.',
            '2026 has been inspected before; it is not an untouched test.',
            'No historical executable prices: no ROI or value-selection performance claim.',
            'Same seasons used in earlier design work: prospective evaluation remains necessary.']}
    if contextual:
        plan['version']='desk_combined_v11'
        plan['design']='Fixed QB-specific scoring-area share posterior (eight opportunities prior, half-weight), then refit component calibration on the original calibration dates only. No tuning to today or Sharp selections.'
        plan['limitations'].append('Historical expected QB inferred from last completed game; current verified depth overrides are unavailable historically.')
    (output/'plan.json').write_text(json.dumps(plan, indent=2))
    with sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        records = [dict(r) for r in db.execute(HISTORY_SQL, {'league':'nfl','start':'2023-07-01','cutoff':plan['cutoff_exclusive']})]
        for r in records:
            if not r.pop('charted'): r['gl_carries']=r['rz_targets']=None
        aliases = canonical_team_map([SimpleNamespace(**{**dict(r),'aliases':json.loads(r['aliases'] or '[]')}) for r in db.execute('select team_id,league_id,aliases from teams')])
        markets = {}
        for r in db.execute("select game_id,market_id,home_line,source from closing_lines where market_id in ('total','point_spread')"):
            markets.setdefault(r['game_id'],{})['total' if r['market_id']=='total' else 'home_handicap'] = -r['home_line'] if r['market_id']=='point_spread' and r['source']=='nflverse_consensus' else r['home_line']
    archive = Path('data/cache/desk_market_archive.json')
    if archive.exists(): markets.update(json.loads(archive.read_text()))
    people = load_asset('players')
    positions = dict(zip(people.gsis_id, people.position_group))
    contexts = []
    for year in (2023, 2024, 2025, 2026):
        print('Preparing scoring paths '+str(year), flush=True)
        cached = output/f'context_{year}.json'
        signature = [(p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in
            (cache_dir()/f'play_by_play_{year}.parquet',cache_dir()/f'snap_counts_{year}.parquet')]
        digest = hashlib.sha256(json.dumps([signature, 'scoring_path_context_v2']).encode()).hexdigest()
        prior = json.loads(cached.read_text()) if cached.exists() else {}
        if prior.get('signature') == digest:
            rows = prior['rows']
        else:
            rows = pbp_context(load_asset('pbp',year),positions)
            snaps, unresolved = snap_context(load_asset('snap_counts',year),people)
            rows += snaps
            cached.write_text(json.dumps({'signature':digest,'rows':rows,'unresolved_snap_ids':unresolved}))
        contexts.extend(rows)
    attach_context(records, contexts, aliases)
    records = prepare_history(records, aliases)
    history = OpportunityHistory(records)
    history.evidence = load_evidence('nfl', replay=True)
    allocator = AllocationHistory(history)
    baseline = json.loads(Path('data/parquet/models/nfl_desk_combined_v9_candidate.json').read_text())
    deployed = json.loads(Path('data/parquet/models/nfl_desk_combined_v2.json').read_text())
    previous = json.loads(Path('data/parquet/models/nfl_desk_combined_v10_candidate.json').read_text())
    model = copy.deepcopy(baseline)
    model.update(version='desk_combined_v10', live_enabled=False,
        scoring_paths={'version':VERSION,'rates':prior_rates(records,plan['fit_end']), 'calibration':[0.,.5,.5]},
        decision_engine='scoring_case_v1', design=plan)
    if contextual:
        from spe.models.player_td.scoring_paths import CONTEXT_VERSION
        model['version']='desk_combined_v11'
        model['scoring_paths']['version']=CONTEXT_VERSION
        model['decision_engine']='scoring_case_v4'
    rows, caches = [], {k:{} for k in ('candidate','v9','v2','v10')}
    for i,r in enumerate(records):
        date = r['kickoff_utc'].isoformat()
        if date < plan['fit_end']: continue
        f = history.features(r['player_id'],r['team_id'],r['opponent_id'],r['position'],r['kickoff_utc'],r['team_id']==r['home_team_id'])
        market = markets.get(r['game_id'],{})
        if not f or not all(market.get(k) is not None for k in ('home_handicap','total')) or f['days_since_appearance'] > 21: continue
        f.update(market_features(market,r['team_id']==r['home_team_id'],True))
        predictions = {}
        for name, m in (('candidate',model),('v9',baseline),('v2',deployed),('v10',previous)):
            key=(r['game_id'],r['team_id'])
            if key not in caches[name]:
                caches[name][key]=allocator.allocate(m,r['team_id'],r['opponent_id'],r['kickoff_utc'],r['team_id']==r['home_team_id'],market=market)
            predictions[name]=combined_prediction(m,f,caches[name][key],r['player_id'])
        if any(v is None for v in predictions.values()): continue
        path = predictions['candidate'].get('scoring_paths') or {}
        if path.get('status') != 'active': continue
        rows.append({'game_id':r['game_id'],'player_id':r['player_id'],'kickoff_utc':r['kickoff_utc'],
            'label':int((r['rush_td'] or 0)+(r['rec_td'] or 0)>0),
            'base': predictions['v9']['probability'], 'deployed':predictions['v2']['probability'],
            'previous':predictions['v10']['probability'], 'path':path['probability']})
    parts = {'calibration':[r for r in rows if r['kickoff_utc'].isoformat()<plan['calibration_end']],
        'validation':[r for r in rows if plan['calibration_end']<=r['kickoff_utc'].isoformat()<plan['validation_end']],
        'examined_2026':[r for r in rows if r['kickoff_utc'].isoformat()>='2026-08-01']}
    if min(len(v) for v in parts.values()) < 100: raise ValueError('Insufficient matched samples')
    cal=parts['calibration']
    x=np.array([[1.,logit(r['base']),logit(r['path'])] for r in cal]); y=np.array([r['label'] for r in cal])
    def loss(w):
        z=x@w
        return float(np.sum(np.logaddexp(0,z)-y*z)+.5*np.sum((w[1:]-[.5,.5])**2))
    fitted=minimize(loss,[0.,.5,.5],bounds=[(-5,5),(0,3),(0,3)],method='L-BFGS-B')
    if not fitted.success: raise ValueError('Calibration did not converge')
    model['scoring_paths']['calibration']=fitted.x.tolist()
    report={'sample_counts':{k:len(v) for k,v in parts.items()},'limitations':plan['limitations'], 'promotion_rule':plan['gate']}
    for key, part in parts.items():
        p=sigmoid(np.array([[1.,logit(r['base']),logit(r['path'])] for r in part])@fitted.x)
        report[key]={'candidate':metrics(part,p),'v9':metrics(part,np.array([r['base'] for r in part])),
            'v10':metrics(part,np.array([r['previous'] for r in part])),
            'v2':metrics(part,np.array([r['deployed'] for r in part])),
            'paired_brier_vs_v9':paired_brier_interval(part,p,np.array([r['base'] for r in part]))}
        for r,v in zip(part,p): r['candidate']=float(v)
    v=report['validation']; candidate=v['candidate']
    report['promotion_gate_passed']=all(candidate['one_per_game']['hits']>v[k]['one_per_game']['hits'] and candidate['brier']<=v[k]['brier'] and candidate['log_loss']<=v[k]['log_loss'] for k in ('v9','v2','v10'))
    # Flag is evidence only. Publication stays an explicit, date-scoped operation.
    model['validation']=report
    model['source_sha256']=hashlib.sha256(json.dumps(rows,default=str,sort_keys=True).encode()).hexdigest()
    (output/(model['version'].replace('desk_','nfl_desk_',1)+'_candidate.json')).write_text(json.dumps(model,indent=2))
    (output/'matched_predictions.json').write_text(json.dumps(parts,default=str))
    print(json.dumps({'calibration':fitted.x.tolist(),**report},indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--contextual',action='store_true')
    args=parser.parse_args()
    train(args.database,args.output,args.contextual)
