"""Read-only, chronological simulation calibration with a predeclared gate.

Candidates come from prior games, including players who subsequently did not
score or play. Never publish a replay or backdate a reconstructed forecast.
"""
import argparse
from collections import defaultdict
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from spe.db.base import as_utc
from spe.integrity.assignments import canonical_team_map
from spe.models.player_td.candidate_universe import team_candidates
from spe.models.player_td.desk_live import HISTORY_SQL
from spe.models.player_td.desk_opportunity import OpportunityHistory, prepare_history
from spe.models.player_td.desk_combined import AllocationHistory, combined_prediction, logit, sigmoid
from spe.models.player_td.desk_evidence import load_evidence
from spe.models.player_td.desk_market import market_features
from spe.models.player_td.nflverse_context import attach_context
from spe.models.player_td.gameflow_calibration import VERSION, probability
from spe.simulate.desk_gameflow_inputs import build_inputs, InputsUnavailable, digest
from spe.simulate.desk_gameflow import simulate
from train_desk_combined import metrics, paired_brier_interval


def dump(path,value):
    path.write_text(json.dumps(value,indent=2,default=str),encoding='utf-8')


def reliability(rows, probabilities):
    bins=[]
    for lo in np.arange(0,1,.1):
        pairs=[(r,p) for r,p in zip(rows,probabilities) if lo<=p<lo+.1]
        if pairs:
            bins.append({'from':float(lo),'to':float(lo+.1),'count':len(pairs),
                'mean_probability':float(np.mean([p for _,p in pairs])),
                'scoring_rate':float(np.mean([r['label'] for r,_ in pairs]))})
    return bins


def run(database, output, draws=10000):
    output.mkdir(parents=True,exist_ok=True)
    plan={'calibration_start':'2025-12-16', 'calibration_end':'2026-02-15',
        'evaluation_start':'2026-08-01', 'evaluation_end':'2026-11-14', 'draws':draws, 'seed':20261115,
        'gate':'At least 30 calibration games and 50 evaluation games; Brier/log loss no worse and more main-pick hits than inherited simulation calibration. No automatic publication.',
        'limitations':['Historical replay with revised data, not saved pregame predictions.',
            '2026 and earlier validation seasons have been examined during prior development; not an untouched test.',
            'Final weekly injury archive used as assumed pregame designation; original capture timestamps unavailable.',
            'No historical current-week weather forecast or verified snap-limit coverage: context stress effects are not fitted.',
            'Prior-appearance candidate pool excludes players without history; DNPs in that pool count as zero TDs.',
            'Historical closing market context is reconstructed; no executable odds or ROI claim.']}
    dump(output/'plan.json',plan)
    body=Path('data/parquet/models/nfl_desk_combined_v11_candidate.json').read_bytes()
    model=json.loads(body);model['artifact_sha256']=hashlib.sha256(body).hexdigest()
    if as_utc(model['calibration_end'])>=as_utc(plan['calibration_start']):
        raise ValueError('Calibration samples must follow component fitting')
    cache=Path('data/cache/nflverse')
    with sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        records=[dict(r) for r in db.execute(HISTORY_SQL,{'league':'nfl','start':'2024-07-01','cutoff':plan['evaluation_end']})]
        for r in records:
            if not r.pop('charted'):r['gl_carries']=r['rz_targets']=None
        teams=[SimpleNamespace(**{**dict(r),'aliases':json.loads(r['aliases'] or '[]')}) for r in db.execute('select team_id,league_id,aliases from teams')]
        aliases=canonical_team_map(teams)
        markets={}
        for r in db.execute("select game_id,market_id,home_line,source from closing_lines where market_id in ('total','point_spread')"):
            markets.setdefault(r['game_id'],{})['total' if r['market_id']=='total' else 'home_handicap']=-r['home_line'] if r['market_id']=='point_spread' and r['source']=='nflverse_consensus' else r['home_line']
    archive=Path('data/cache/desk_market_archive.json')
    if archive.exists():markets.update(json.loads(archive.read_text()))
    contexts=[]
    from spe.analytics.nflverse_opportunities import pbp_context,snap_context
    people=pd.read_parquet(cache/'players.parquet')
    positions=dict(zip(people.gsis_id,people.position_group))
    for year in (2024,2025,2026):
        print('Preparing historical contexts',year,flush=True)
        prior=Path(f'data/research/td_paths_v11/context_{year}.json')
        signature=[(p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in (cache/f'play_by_play_{year}.parquet',cache/f'snap_counts_{year}.parquet')]
        expected=hashlib.sha256(json.dumps([signature,'scoring_path_context_v2']).encode()).hexdigest()
        stored=json.loads(prior.read_text()) if prior.exists() else {}
        if stored.get('signature')==expected:
            rows=stored['rows']
        else:
            rows=pbp_context(pd.read_parquet(cache/f'play_by_play_{year}.parquet'),positions)
            snaps,_=snap_context(pd.read_parquet(cache/f'snap_counts_{year}.parquet'),people)
            rows+=snaps
        contexts.extend(rows)
    attach_context(records,contexts,aliases)
    records=prepare_history(records,aliases)
    history=OpportunityHistory(records);history.evidence=load_evidence('nfl',replay=True)
    allocator=AllocationHistory(history)
    games={r['game_id']:r for r in records}
    labels={(r['game_id'],r['player_id']):int((r['rush_td'] or 0)+(r['rec_td'] or 0)>0) for r in records}
    injury={}
    for year in (2025,2026):
        for r in pd.read_parquet(cache/f'injuries_{year}.parquet').to_dict('records'):
            if str(r.get('report_status','')).lower() in ('out','doubtful','ir','injured reserve'):
                injury.setdefault((year,int(r['week']),r['team']),set()).add('nfl:'+str(r['gsis_id']))
    predictions=[];skipped=[];game_count=0
    for gid,g in sorted(games.items(),key=lambda pair:pair[1]['kickoff_utc']):
        date=g['kickoff_utc'].isoformat()
        part='calibration' if plan['calibration_start']<=date<plan['calibration_end'] else 'evaluation' if plan['evaluation_start']<=date<plan['evaluation_end'] else None
        if part is None:continue
        market=markets.get(gid,{})
        if any(market.get(k) is None for k in ('total','home_handicap')):
            skipped.append({'game_id':gid,'reason':'missing market'});continue
        cutoff=g['kickoff_utc']-timedelta(minutes=1)
        metadata={'game_id':gid,'home':g['home_team_id'].removeprefix('nfl:'),'away':g['away_team_id'].removeprefix('nfl:'),'kickoff':g['kickoff_utc'].isoformat()}
        market={**market,'home_implied_points':(market['total']-market['home_handicap'])/2,
                'away_implied_points':(market['total']+market['home_handicap'])/2}
        season=int(gid.split(':')[-1].split('_')[0]);week=int(gid.split(':')[-1].split('_')[1])
        candidates=[]
        for tid,opp,home in ((g['home_team_id'],g['away_team_id'],True),(g['away_team_id'],g['home_team_id'],False)):
            excluded=injury.get((season,week,tid.removeprefix('nfl:')),set())
            allocation=allocator.allocate(model,tid,opp,cutoff,home,excluded,market=market)
            for candidate in team_candidates(history,allocator,tid,opp,cutoff,home):
                if candidate['player_id'] in excluded:continue
                f=candidate['features'];f.update(market_features(market,home,True))
                combined=combined_prediction(model,f,allocation,candidate['player_id'])
                if not combined:continue
                candidates.append({**candidate,'team':tid.removeprefix('nfl:'),'market_context':market,
                    'forecast':{'p_estimate':combined['probability']},
                    'desk_analysis':{'features':f,'combination':combined,'current_context':{}}})
        try:
            inputs=build_inputs(metadata,candidates,cutoff,cache/f'play_by_play_{season}.parquet',historical_replay=True)
            result=simulate(inputs,draws=draws,seed=plan['seed'])
        except (InputsUnavailable,ValueError) as exc:
            skipped.append({'game_id':gid,'reason':str(exc)});continue
        players={r['id']:r for r in result['players']}
        for r in candidates:
            if r['player_id'] not in players:continue
            paths=r['desk_analysis']['combination']['scoring_paths']
            raw=players[r['player_id']]['raw_probability'];base=paths['baseline_probability']
            predictions.append({'game_id':gid,'player_id':r['player_id'],'player_name':r['player_name'],
                'kickoff_utc':g['kickoff_utc'].isoformat(),'part':part,'label':labels.get((gid,r['player_id']),0),
                'baseline':base,'raw_simulation':raw,'inherited':probability(None,model,base,raw),
                'model_without_simulation':r['forecast']['p_estimate']})
        game_count+=1
        if game_count%10==0:print('Simulated',game_count,'games',flush=True)
    dump(output/'predictions.json',predictions);dump(output/'skipped.json',skipped)
    parts={part:[r for r in predictions if r['part']==part] for part in ('calibration','evaluation')}
    cal=parts['calibration'];y=np.array([r['label'] for r in cal])
    x=np.array([[1.,logit(r['baseline']),logit(r['raw_simulation'])] for r in cal])
    if len(cal)<100 or len(set(y))<2:raise ValueError('Insufficient calibration inputs')
    prior=np.array(model['scoring_paths']['calibration'])
    def loss(w):
        z=x@w
        return float(np.sum(np.logaddexp(0,z)-y*z)+5*np.sum((w-prior)**2))
    fit=minimize(loss,prior,bounds=[(-3,3),(0,3),(0,3)],method='L-BFGS-B')
    if not fit.success:raise ValueError('Calibration fit failed')
    report={}
    for name,part in parts.items():
        p=sigmoid(np.array([[1.,logit(r['baseline']),logit(r['raw_simulation'])] for r in part])@fit.x)
        report[name]={'candidate':metrics(part,p),'inherited':metrics(part,np.array([r['inherited'] for r in part])),
            'model_without_simulation':metrics(part,np.array([r['model_without_simulation'] for r in part])),
            'paired_brier':paired_brier_interval(part,p,np.array([r['inherited'] for r in part])),
            'reliability_bins':reliability(part,p)}
        for r,prob in zip(part,p):r['candidate']=float(prob)
    v=report['evaluation'];new=v['candidate'];old=v['inherited']
    passed=bool(report['calibration']['candidate']['one_per_game']['games']>=30 and new['one_per_game']['games']>=50 and
        new['brier']<=old['brier'] and new['log_loss']<=old['log_loss'] and new['one_per_game']['hits']>old['one_per_game']['hits'])
    artifact={'version':VERSION,'base_model_sha256':model['artifact_sha256'],'coefficients':fit.x.tolist(),
        'trained_at':datetime.now(UTC).isoformat(),'evaluation_end':plan['evaluation_end'],
        'promotion_gate_passed':passed,'validation':report,'plan':plan,'input_sha256':digest(predictions)}
    dump(output/'calibration.json',artifact);dump(output/'predictions.json',predictions)
    print(json.dumps({'coefficients':fit.x.tolist(),'promotion_gate_passed':passed,'report':report,'skipped_games':len(skipped)},indent=2),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--database',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--draws',type=int,default=10000)
    a=p.parse_args();run(a.database,a.output,a.draws)
