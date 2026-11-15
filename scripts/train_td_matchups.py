"""One predeclared NFL challenger, using free prior-game PBP and snap counts.

All Nov 14 outcomes are excluded. No hyperparameter search on validation results.
Writes candidate weights only; the date-scoped run is an explicit separate step.
"""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from spe.analytics.nflverse_opportunities import pbp_context, snap_context
from spe.ingest.nflverse_historical import load_asset, cache_dir
from spe.integrity.assignments import canonical_team_map
from spe.models.player_td.desk_live import HISTORY_SQL
from spe.models.player_td.nflverse_context import attach_context
from train_desk_combined import run


def train(database, output):
    output.mkdir(parents=True,exist_ok=True)
    plan={'version':'desk_combined_v9','league':'nfl','cutoff_exclusive':'2026-11-14',
        'design':'One fixed regularized player logistic + team channel allocation; fitted role-by-opponent interactions and NFL scoring-area/snap features.',
        'split':'Fit before 2025-10-15; blend before 2025-11-15; calibrate before 2025-12-15; validate through 2026-02-15. Report examined 2026 separately.',
        'gate':'More validation game winners, no worse Brier/log loss than existing combined and player-only baselines. Never promote automatically.',
        'price':'Prices do not enter probabilities. Value comparison requires actual fresh quotes.',
        'limits':'Revised historical archives; observed participant bias. No prospective accuracy or ROI claim. No route or coverage data invented.'}
    (output/'plan.json').write_text(json.dumps(plan,indent=2))
    with sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        records=[dict(r) for r in db.execute(HISTORY_SQL,{'league':'nfl','start':'2023-07-01','cutoff':plan['cutoff_exclusive']})]
        for r in records:
            if not r.pop('charted'):r['gl_carries']=r['rz_targets']=None
        teams=[dict(r) for r in db.execute('select team_id, league_id, aliases from teams')]
        aliases=canonical_team_map([SimpleNamespace(**{**r,'aliases':json.loads(r['aliases'] or '[]')}) for r in teams])
        markets={}
        for row in db.execute("select game_id,market_id,home_line,source from closing_lines where market_id in ('total','point_spread')"):
            key='total' if row['market_id']=='total' else 'home_handicap'
            markets.setdefault(row['game_id'],{})[key]=-row['home_line'] if key=='home_handicap' and row['source']=='nflverse_consensus' else row['home_line']
    players=load_asset('players')
    positions=dict(zip(players.gsis_id,players.position_group))
    contexts=[]
    for year in (2023,2024,2025,2026):
        print('Preparing free NFL PBP/snap inputs '+str(year),flush=True)
        cached=output/f'context_{year}.json'
        files=[cache_dir()/f'play_by_play_{year}.parquet',cache_dir()/f'snap_counts_{year}.parquet']
        signature=[(p.name,p.stat().st_size,p.stat().st_mtime_ns) if p.exists() else (p.name,0,0) for p in files]
        digest=hashlib.sha256(json.dumps(signature).encode()).hexdigest()
        prior=json.loads(cached.read_text()) if cached.exists() else {}
        if prior.get('signature')==digest:
            rows=prior['rows']
        else:
            rows=pbp_context(load_asset('pbp',year),positions)
            snaps,unresolved=snap_context(load_asset('snap_counts',year),players)
            rows+=snaps
            cached.write_text(json.dumps({'signature':digest,'rows':rows,'unresolved_snap_ids':unresolved}))
        contexts.extend(rows)
    attach_context(records,contexts,aliases)
    archive=Path('data/cache/desk_market_archive.json')
    if archive.exists():markets.update(json.loads(archive.read_text()))
    model=run('nfl',records,output,aliases,markets,roles=True,player_matchups=True)
    body=(output/'nfl_desk_combined_v9.json').read_bytes()
    (output/'nfl_desk_combined_v9_candidate.json').write_bytes(body)
    print(json.dumps({'finished':True,'sha256':hashlib.sha256(body).hexdigest(),
        'live_enabled':model['live_enabled'],'validation':model['validation']['validation'],
        'evaluation_2026':model['validation']['evaluation_2026']},indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    train(args.database,args.output)
