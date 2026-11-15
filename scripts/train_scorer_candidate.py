"""Predeclared nonlinear FBS experiment with a pregame candidate universe.

No downloads, production writes or automatic promotion. Existing cached sources
are used read-only. All current-day outcomes are excluded. Historical source
revisions and absence of archived active lists are reported, never hidden.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import warnings

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier

from spe.db.base import as_utc
from spe.integrity.assignments import canonical_team_map
from spe.ingest.recent_td_boxes import overlay_history
from spe.models.player_td import scorer_candidate as scorer
from spe.models.player_td.calibration import stable_platt
from spe.models.player_td.candidate_universe import team_candidates
from spe.models.player_td.desk_combined import AllocationHistory, combined_prediction, logit, sigmoid
from spe.models.player_td.desk_evidence import load_evidence
from spe.models.player_td.desk_live import HISTORY_SQL
from spe.models.player_td.desk_market import market_features
from spe.models.player_td.desk_opportunity import OpportunityHistory, prepare_history
from spe.research.td_scoring_labels import verified_labels
from train_desk_combined import metrics as probability_metrics, paired_brier_interval

PLAN = {'variants':[{'name':'shallow','max_depth':2},{'name':'interactions','max_depth':3}],
    'n_estimators':180,'learning_rate':.035,'min_samples_leaf':120,
    'tree_weights':[.25,.5,.75,1.], 'random_state':20261115,
    'fit_before':'2025-01-01','selection_before':'2025-10-15',
    'calibration_before':'2025-11-01','validation_before':'2026-02-15',
    'data_cutoff_exclusive':'2026-11-14',
    'selection':'Highest one-per-game hits in blend period, then lowest log loss; calibrate selected model only.',
    'promotion':'At least 5 additional validation hits; Brier and log loss no worse; positive calibration; '
        '2026 examined replay must also have no fewer hits and no worse Brier/log loss. Local research only.',
    'limits':['All historical periods have been examined; this is retrospective research, not prospective proof.',
        'Player pool is built before target game; historical game-day injury/active lists are unavailable.',
        'Only games with matching complete rushing/receiving scorer identities from both sources are evaluated.',
        'Rookies and replacements without two prior same-team appearances are still outside this model.',
        'Historical market observation timestamps are unavailable. No executable-price ROI claim.',
        'Sources are revised archives, with prior PBP assumed available 48 hours after each game.']}


def metrics(rows, probabilities):
    result = probability_metrics(rows, probabilities)
    selected = {}
    for row, chance in zip(rows, probabilities):
        key = (float(chance), row['player_id'])
        if row['game_id'] not in selected or key > selected[row['game_id']][0]:
            selected[row['game_id']] = (key, row['label'])
    hits = sum(v[1] for v in selected.values())
    result['one_per_game'] = {'games':len(selected),'hits':int(hits),'hit_rate':hits/max(1,len(selected))}
    return result


def paired_hits(rows, candidate, baseline):
    def picks(p):
        result={}
        for r,q in zip(rows,p):
            key=(float(q),r['player_id'])
            if r['game_id'] not in result or key>result[r['game_id']][0]:
                result[r['game_id']]=(key,r['label'])
        return {k:v[1] for k,v in result.items()}
    a,b=picks(candidate),picks(baseline)
    d=np.array([a[g]-b[g] for g in sorted(a)])
    boot=np.random.default_rng(20261115).choice(d,(5000,len(d))).mean(axis=1)
    return {'additional_winners':int(d.sum()),'changed_to_winner':int((d>0).sum()),
        'changed_to_loser':int((d<0).sum()),'hit_rate_difference':float(d.mean()),
        'game_bootstrap_95_percent':np.quantile(boot,[.025,.975]).tolist()}


def export_trees(estimator, medians):
    prior=float(estimator.init_.class_prior_[1])
    return {'features':scorer.FEATURES,'medians':medians.tolist(),
        'intercept':float(logit(prior)),'learning_rate':estimator.learning_rate,
        'trees':[{'left':e[0].tree_.children_left.tolist(),'right':e[0].tree_.children_right.tolist(),
            'feature':e[0].tree_.feature.tolist(),'threshold':e[0].tree_.threshold.tolist(),
            'value':e[0].tree_.value[:,0,0].tolist()} for e in estimator.estimators_]}


def build_dataset(database, output, baseline):
    with sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        teams=[SimpleNamespace(**{**dict(r),'aliases':json.loads(r['aliases'] or '[]')})
            for r in db.execute('SELECT team_id,league_id,aliases FROM teams')]
        aliases=canonical_team_map(teams)
        records=[dict(r) for r in db.execute(HISTORY_SQL,{'league':'fbs','start':'2023-07-01',
            'cutoff':PLAN['data_cutoff_exclusive']})]
        for r in records:
            if not r.pop('charted'):r['gl_carries']=r['rz_targets']=None
        records=prepare_history(overlay_history(records,as_utc(PLAN['data_cutoff_exclusive']),reconstructed=True),aliases)
        known={r['player_id'] for r in records}
        player_ids={p.removeprefix('fbs:'):p for p in known if p.startswith('fbs:')}
        for r in db.execute("SELECT external_id,canonical_id FROM provider_id_map WHERE provider='espn' AND entity_type='player' AND confidence>=.99"):
            if r['canonical_id'] in known:player_ids[r['external_id']]=r['canonical_id']
    archive=json.loads(Path('data/cache/college_markets/markets.json').read_text())
    markets=archive['markets']
    history=OpportunityHistory(records);history.evidence=load_evidence('fbs',replay=True)
    allocator=AllocationHistory(history)
    labels={};source_hashes={}
    for year in range(2023,2027):
        path=Path(f'data/cache/sportsdataverse/play_by_play_{year}.parquet')
        columns=['game_id','id','pos_team_id','homeTeamId','awayTeamId','rush_td','pass_td',
            'rusher_player_id','receiver_player_id','type.text','status_type_completed',
            'penalty_no_play','penalty_negated_play']
        frame=pd.read_parquet(path,columns=columns)
        labels.update(verified_labels(frame,[r for r in records if r['kickoff_utc'].year==year],aliases,player_ids))
        source_hashes[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
        print(f'Label reconciliation {year}: {len(labels)} cumulative verified games',flush=True)
    actual={(r['game_id'],r['player_id']):r for r in records}
    games={r['game_id']:r for r in records}
    rows=[];vectors=[];counts=Counter()
    for number,(gid,g) in enumerate(sorted(games.items(),key=lambda x:x[1]['kickoff_utc'])):
        if gid not in labels:counts['unverified_outcome_games']+=1;continue
        market=markets.get(gid.split(':')[-1])
        if market is None:counts['missing_market_games']+=1;continue
        cutoff=g['kickoff_utc']-timedelta(seconds=1)
        new=[];new_x=[]
        for team,opp,home in ((g['home_team_id'],g['away_team_id'],True),(g['away_team_id'],g['home_team_id'],False)):
            allocation=allocator.allocate(baseline,team,opp,cutoff,home,market=market)
            if not allocation:continue
            for candidate in team_candidates(history,allocator,team,opp,cutoff,home):
                f=candidate['features'];f.update(market_features(market,home,baseline.get('market_total_trained',True)))
                prediction=combined_prediction(baseline,f,allocation,candidate['player_id'])
                if prediction is None:raise ValueError('Pregame candidate absent from allocation')
                pid=candidate['player_id'];present=(gid,pid) in actual
                new.append({'game_id':gid,'player_id':pid,'player_name':candidate['player_name'],
                    'team_id':team,'position':candidate['position'],'kickoff_utc':g['kickoff_utc'].isoformat(),
                    'label':int(pid in labels[gid]['scorers']),'in_target_box':present,
                    'baseline_probability':prediction['probability'],
                    'baseline_raw_logit':float((logit(prediction['probability'])-baseline['calibration_intercept'])/baseline['calibration_slope']),
                    'prior_games':int(f['games']),'team_implied_points':(market['total']-(market['home_handicap'] if home else -market['home_handicap']))/2})
                new_x.append(scorer.vector(f))
        # A game requires pregame candidates from both teams for this comparison.
        if len({r['team_id'] for r in new})!=2:counts['incomplete_candidate_teams']+=1;continue
        rows.extend(new);vectors.extend(new_x)
        if number%100==0:print(f'Features: {number}/{len(games)} games, {len(rows)} candidates',flush=True)
    x=np.array(vectors)
    np.savez_compressed(output/'dataset.npz',x=x)
    (output/'rows.json').write_text(json.dumps(rows))
    metadata={'source_hashes':source_hashes,'label_verified_games':len(labels),
        'evaluated_games':len({r['game_id'] for r in rows}),'player_games':len(rows),
        'candidates_missing_target_box':sum(not r['in_target_box'] for r in rows),
        'excluded':dict(counts),'baseline_sha256':hashlib.sha256(json.dumps(baseline,sort_keys=True).encode()).hexdigest()}
    (output/'dataset-audit.json').write_text(json.dumps(metadata,indent=2))
    return rows,x,metadata


def run(database, output, reuse=False):
    output.mkdir(parents=True,exist_ok=True)
    # Write the fixed design before fitting or reading validation results.
    plan_path=output/'plan.json'
    if plan_path.exists() and json.loads(plan_path.read_text()) != PLAN:
        raise ValueError('Do not change the design of an already evaluated study')
    if not plan_path.exists():
        plan_path.write_text(json.dumps(PLAN,indent=2))
    baseline=json.loads(Path('data/parquet/models/fbs_desk_combined_v3.json').read_text())
    if reuse:
        rows=json.loads((output/'rows.json').read_text());x=np.load(output/'dataset.npz')['x']
        audit=json.loads((output/'dataset-audit.json').read_text())
    else:rows,x,audit=build_dataset(database,output,baseline)
    times=np.array([r['kickoff_utc'] for r in rows]);y=np.array([r['label'] for r in rows])
    masks={'fit':times<PLAN['fit_before'],
        'blend':(times>=PLAN['fit_before'])&(times<PLAN['selection_before']),
        'calibrate':(times>=PLAN['selection_before'])&(times<PLAN['calibration_before']),
        'validation':(times>=PLAN['calibration_before'])&(times<PLAN['validation_before']),
        'evaluation_2026':times>='2026-08-01'}
    if min(int(m.sum()) for m in masks.values())<100:raise ValueError('Insufficient independent periods')
    with warnings.catch_warnings():
        warnings.simplefilter('ignore',RuntimeWarning)
        medians=np.nanmedian(x[masks['fit']],axis=0)
    medians=np.where(np.isfinite(medians),medians,0.)
    z=scorer.matrix(x,medians)
    raw=np.array([r['baseline_raw_logit'] for r in rows]);old=np.array([r['baseline_probability'] for r in rows])
    part=lambda key:[r for r,keep in zip(rows,masks[key]) if keep]
    choices=[];estimators={}
    for variant in PLAN['variants']:
        print('Fitting '+variant['name'],flush=True)
        estimator=GradientBoostingClassifier(n_estimators=PLAN['n_estimators'],learning_rate=PLAN['learning_rate'],
            min_samples_leaf=PLAN['min_samples_leaf'],max_depth=variant['max_depth'],random_state=PLAN['random_state'])
        estimator.fit(z[masks['fit']],y[masks['fit']])
        estimators[variant['name']]=estimator
        score=estimator.decision_function(z[masks['blend']])
        for weight in PLAN['tree_weights']:
            p=sigmoid(weight*score+(1-weight)*raw[masks['blend']])
            result=metrics(part('blend'),p)
            choices.append({'variant':variant['name'],'tree_weight':weight,'metrics':result})
    selected=min(choices,key=lambda c:(-c['metrics']['one_per_game']['hits'],c['metrics']['log_loss'],c['variant'],c['tree_weight']))
    estimator=estimators[selected['variant']];weight=selected['tree_weight']
    score=estimator.decision_function(z)
    blended=weight*score+(1-weight)*raw
    a,b=stable_platt(sigmoid(blended[masks['calibrate']]),y[masks['calibrate']])
    p=sigmoid(a+b*blended)
    exported=export_trees(estimator,medians)
    exported.update(tree_weight=weight,calibration_intercept=a,calibration_slope=b,uses_market_totals=True)
    if not scorer.validate(exported):raise ValueError('Invalid exported tree model')
    # Compare portable inference with the fitting library across every cached row.
    # Reconstruct feature maps from the exact input vector; no outcomes are used.
    parity=[]
    for i in np.linspace(0,len(rows)-1,min(500,len(rows)),dtype=int):
        f={k:(float(v) if np.isfinite(v) else None) for k,v in zip(scorer.FEATURES,x[i])}
        f['context_game_total']=f['market_game_total']
        parity.append(abs(scorer.predict(exported,f,raw[i])-p[i]))
    if max(parity)>1e-6:raise ValueError(f'Training/serving mismatch: {max(parity)}')
    report={'plan':PLAN,'dataset':audit,'selection':selected,'blend_trials':choices,
        'samples':{k:int(m.sum()) for k,m in masks.items()},'maximum_export_error':max(parity),
        'calibration':{'intercept':a,'slope':b},'evaluation':{}}
    for name in ('validation','evaluation_2026'):
        mask=masks[name];rr=part(name)
        candidate=metrics(rr,p[mask]);previous=metrics(rr,old[mask])
        result={'candidate':candidate,'baseline':previous,
            'paired_brier':paired_brier_interval(rr,p[mask],old[mask]),
            'paired_hits':paired_hits(rr,p[mask],old[mask])}
        changed=[]
        groups=defaultdict(list)
        for r,new,prev in zip(rr,p[mask],old[mask]):groups[r['game_id']].append({**r,'new_probability':float(new),'old_probability':float(prev)})
        for gid,values in groups.items():
            first=max(values,key=lambda r:(r['old_probability'],r['player_id']))
            second=max(values,key=lambda r:(r['new_probability'],r['player_id']))
            if first['player_id']!=second['player_id']:changed.append({'game_id':gid,'old':first,'new':second})
        (output/(name+'-changed-picks.json')).write_text(json.dumps(changed,indent=2))
        result['changed_picks']=len(changed)
        result['candidate_position_results']={pos: {'picks':sum(max(v,key=lambda r:(r['new_probability'],r['player_id']))['position']==pos for v in groups.values()),
            'wins':sum(max(v,key=lambda r:(r['new_probability'],r['player_id']))['label'] for v in groups.values() if max(v,key=lambda r:(r['new_probability'],r['player_id']))['position']==pos)} for pos in ('QB','RB','WR','TE')}
        report['evaluation'][name]=result
    v=report['evaluation']['validation'];recent=report['evaluation']['evaluation_2026']
    gate=bool(b>0 and v['paired_hits']['additional_winners']>=5 and recent['paired_hits']['additional_winners']>=0
        and all(s['candidate'][metric]<=s['baseline'][metric] for s in (v,recent) for metric in ('brier','log_loss')))
    report['promotion_gate_passed']=gate
    artifact={**deepcopy(baseline),'version':'desk_combined_v8','scorer_model':exported,
        'baseline_artifact_sha256':hashlib.sha256(Path('data/parquet/models/fbs_desk_combined_v3.json').read_bytes()).hexdigest(),
        'live_enabled':gate,'research_only':True,'validation':report}
    (output/'fbs_desk_combined_v8_candidate.json').write_text(json.dumps(artifact,indent=2))
    (output/'study.json').write_text(json.dumps(report,indent=2))
    (output/'predictions.json').write_text(json.dumps([{**r,'candidate_probability':float(pp)} for r,pp in zip(rows,p) if r['kickoff_utc']>=PLAN['calibration_before']]))
    print(json.dumps({'selected':selected,'gate':gate,'evaluation':report['evaluation']},indent=2),flush=True)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--database',type=Path,default=Path('data/caliber.db'))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--reuse-dataset',action='store_true')
    args=parser.parse_args()
    run(args.database,args.output,args.reuse_dataset)
