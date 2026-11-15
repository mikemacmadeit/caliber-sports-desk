"""Predeclared college opportunity experiments; no current-day outcomes.

Select channel/current variants in the blend period, calibrate independently,
then apply the fixed validation gate. Never automatically publish a model.
"""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from spe.db.base import as_utc
from spe.integrity.assignments import canonical_team_map
from spe.ingest.college_market_history import refresh
from spe.ingest.recent_td_boxes import overlay_history
from spe.models.player_td.desk_live import HISTORY_SQL
from train_desk_combined import run


def study(database, output, market_dir, cutoff='2026-11-14'):
    output.mkdir(parents=True, exist_ok=True)
    plan={'variants':['channels','current'], 'training_cutoff_exclusive':cutoff,
          'selection':'Most one-per-game blend-period winners, then lowest blend log loss; no validation tuning.',
          'promotion':'Selected variant must pass fixed validation probability and scorer gates. No automatic publication.',
          'limits':'Historical data and earlier evaluations have been examined. No prospective improvement claim.'}
    (output/'plan.json').write_text(json.dumps(plan,indent=2))
    archive=refresh(range(2023,2027),market_dir)
    with sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        teams=[dict(r) for r in db.execute('SELECT team_id,league_id,aliases FROM teams')]
        aliases=canonical_team_map([SimpleNamespace(**{**r,'aliases':json.loads(r['aliases'] or '[]')}) for r in teams])
        records=[dict(r) for r in db.execute(HISTORY_SQL,{'league':'fbs','start':'2023-07-01','cutoff':cutoff})]
        for r in records:
            if not r.pop('charted'):r['gl_carries']=r['rz_targets']=None
        records=overlay_history(records,as_utc(cutoff),reconstructed=True)
        markets={}
        for row in db.execute("SELECT game_id,market_id,home_line,source FROM closing_lines WHERE market_id IN ('point_spread','total')"):
            key='total' if row['market_id']=='total' else 'home_handicap'
            markets.setdefault(row['game_id'],{})[key]=-row['home_line'] if row['source']=='nflverse_consensus' and key=='home_handicap' else row['home_line']
        for row in db.execute("SELECT game_id FROM games WHERE league_id='fbs'"):
            entry=archive['markets'].get(row['game_id'].split(':')[-1])
            if entry:markets[row['game_id']]=entry
    models={}
    for profile in plan['variants']:
        print('Fitting predeclared variant: '+profile,flush=True)
        models[profile]=run('fbs',records,output/profile,aliases,markets,
                            input_profile=profile,with_totals=True)
    def key(profile):
        m=models[profile]['validation']['blend_selection']
        return (-m['one_per_game']['hits'],m['log_loss'],profile)
    selected=min(models,key=key)
    model=models[selected]
    report={'plan':plan,'selected_profile':selected,'promotion_gate_passed':model['live_enabled'],
            'sources':archive['sources'],'models':{p:m['validation'] for p,m in models.items()}}
    body=(json.dumps(model,indent=2)+'\n').encode()
    (output/'fbs_desk_combined_v7_candidate.json').write_bytes(body)
    report['artifact_sha256']=hashlib.sha256(body).hexdigest()
    (output/'study.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({'selected_profile':selected,'gate':model['live_enabled'],
        'validation':model['validation']['validation'],
        'evaluation_2026':model['validation']['evaluation_2026']},indent=2),flush=True)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--market-dir',type=Path,required=True)
    args=parser.parse_args()
    study(args.database,args.output,args.market_dir)
