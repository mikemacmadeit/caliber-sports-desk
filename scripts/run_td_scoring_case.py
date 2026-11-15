"""Install a local date-scoped research artifact and rerun FUTURE games only.

Archives the previous picks/candidate sets before mutation. Does not deploy,
buy data, change production model defaults, or rewrite begun-game forecasts.
"""
import argparse
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from spe.config import settings
from spe.db.base import as_utc
from spe.db.models import BoardSnapshot, Game
from spe.db.session import session_factory
from spe.models.player_td.desk_combined import load_combined_model
from spe.models.player_td.desk_replay import replay_day
from spe.models.player_td.scorer_eligibility import exclusions


def dump(path, data):
    path.write_text(json.dumps(data,indent=2,default=str),encoding='utf-8')


def run(day, source, output, context_file=None):
    output.mkdir(parents=True,exist_ok=True)
    now=datetime.now(UTC)
    with session_factory() as db:
        games=[g for g in db.query(Game).filter(Game.league_id.in_(('nfl','fbs')))
            if as_utc(g.kickoff_utc).astimezone(ZoneInfo('America/Chicago')).date().isoformat()==day
            and g.data_tier not in ('demo','alias') and g.status not in ('canceled','cancelled','postponed')]
        if not games or any(as_utc(g.kickoff_utc)<=now or g.status in ('final','in_progress') for g in games):
            raise ValueError('This command requires an entirely future slate; begun games are protected')
        before=replay_day(db,day,'all',now,cached_only=True)
        dump(output/'scoring-case-before.json',before)
        gids={g.game_id for g in games}
        archives=[]
        for saved in db.query(BoardSnapshot):
            if ':preserved:' in saved.key: continue
            if saved.key.startswith(('td-pregame-candidates-v1:','retrained-desk-v1:','scorer-desk-v1:')) and any(gid in saved.key for gid in gids):
                digest=hashlib.sha256(json.dumps(saved.payload,sort_keys=True).encode()).hexdigest()
                key=saved.key+':preserved:'+digest[:16]
                if db.get(BoardSnapshot,key) is None:
                    db.add(BoardSnapshot(key=key,payload=deepcopy(saved.payload),updated_at=saved.updated_at))
                archives.append({'original_key':saved.key,'archive_key':key,'sha256':digest})
        db.commit()
    dump(output/'scoring-case-preserved.json',archives)
    body=source.read_bytes()
    artifact=json.loads(body)
    if artifact.get('version')!='desk_combined_v10' or artifact.get('league')!='nfl':
        raise ValueError('Expected an NFL scoring-path candidate artifact')
    target=settings.parquet_dir/'models/nfl_desk_combined_v10_candidate.json'
    target.write_bytes(body)
    model=load_combined_model('nfl',research_candidate=True,version='desk_combined_v10')
    if not model: raise ValueError('Candidate artifact failed schema validation')
    if context_file:
        from spe.ingest.nflverse_desk import store_context
        contexts=json.loads(context_file.read_text())['rows']
        with session_factory() as db:
            stored=store_context(db,contexts,year=now.year-int(now.month<7),observed_at=now)
            db.commit()
        print(json.dumps({'new_context_rows':stored}),flush=True)
    config_path=settings.parquet_dir/'models/desk_retrain_runs.json'
    prior=config_path.read_bytes() if config_path.exists() else b'{}'
    dump(output/'scoring-case-prior-config.json',json.loads(prior))
    config=json.loads(prior)
    config.setdefault(day,{})['nfl']={'version':model['version'],'artifact_sha256':model['artifact_sha256'],
        'use_validated_model':False,'reconstruct_public_pregame':False,'requested_at':now.isoformat(),
        'reason':'User-requested local scoring-case research run. Not a production promotion; validation results stay visible.'}
    dump(config_path,config)
    try:
        with session_factory() as db:
            board=replay_day(db,day,'all')
            if board['unavailable_games'] or len(board['rows'])!=board['games']:
                raise ValueError('Incomplete board; prior date configuration will be restored')
            if any(exclusions(r) for r in board['rows']): raise ValueError('Ineligible player on board')
            nfl=[r for r in board['rows'] if r['league_id']=='nfl']
            if not all((r.get('decision') or {}).get('version','').startswith('scoring_case_') and r['desk_analysis']['combination'].get('scoring_paths_active') for r in nfl):
                raise ValueError('Detailed NFL inputs were not available for every selected player')
            db.commit()
    except Exception:
        config_path.write_bytes(prior)
        raise
    dump(output/'scoring-case-picks.json',board)
    old={r['game_id']:r for r in before['rows']}
    comparison=[{'game':r['away_abbr']+' at '+r['home_abbr'],'league':r['league_id'],
        'before':old.get(r['game_id'],{}).get('player_name'),'after':r['player_name'],
        'changed':old.get(r['game_id'],{}).get('player_id')!=r['player_id'],
        'position':r['position'],'probability':r['forecast']['p_estimate'], 'price':r.get('book_price'),
        'decision':r.get('decision'), 'case':r['desk_analysis']['scouting_report'].get('case')} for r in board['rows']]
    report={'date':day,'generated_at':datetime.now(UTC).isoformat(),'games':len(comparison),
        'changed':sum(r['changed'] for r in comparison), 'validation':model['validation'],
        'artifact_sha256':model['artifact_sha256'],'global_promotion':False,'comparisons':comparison}
    dump(output/'scoring-case-comparison.json',report)
    print(json.dumps({'games':report['games'],'changed':report['changed'],
        'picks':[{k:v for k,v in r.items() if k not in ('decision','case')} for r in comparison]},indent=2),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--date',required=True)
    p.add_argument('--model',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--context-file',type=Path)
    a=p.parse_args()
    run(a.date,a.model,a.output,a.context_file)
