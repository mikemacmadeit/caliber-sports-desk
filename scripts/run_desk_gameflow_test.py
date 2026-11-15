"""Test one NFL game without application writes; explicitly label historical replays."""
import argparse
from copy import deepcopy
from datetime import UTC,datetime,timedelta
import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np
import pandas as pd
from sqlalchemy import text

from spe.config import settings
from spe.db.base import as_utc
from spe.db.models import BoardSnapshot,Game
from spe.db.session import session_factory
from spe.models.player_td.scorer_eligibility import exclusions
from spe.simulate.desk_gameflow import simulate


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).hexdigest()
def dump(path,value):path.write_text(json.dumps(value,indent=2,default=str),encoding='utf-8')


def protected(db):
    # User specifically requested that this game remain untouched.
    return {s.key:digest(s.payload) for s in db.query(BoardSnapshot).filter(
        BoardSnapshot.key.contains('nfl:2026_10_NE_DET'))}


def freeze(game_id,output,*,historical_replay=False):
    if output.exists():raise ValueError('Use a new output directory; never replace a frozen experiment')
    now=datetime.now(UTC)
    with session_factory() as db:
        if db.bind.dialect.name!='sqlite':raise ValueError('Local SQLite research only')
        db.execute(text('PRAGMA query_only=ON'))
        game=db.get(Game,game_id)
        if not game or game.league_id!='nfl':
            raise ValueError('An NFL game is required')
        if historical_replay and as_utc(game.kickoff_utc)>now:
            raise ValueError('Historical replay is only for games whose kickoff has passed')
        if not historical_replay and (as_utc(game.kickoff_utc)<=now or game.status not in ('scheduled','preview','pregame')):
            raise ValueError('A future, unstarted NFL game is required')
        source=db.get(BoardSnapshot,'td-pregame-candidates-v1:'+game_id)
        if not source:raise ValueError('No saved candidate universe')
        saved=deepcopy(source.payload)
        if as_utc(saved['captured_at'])>=as_utc(game.kickoff_utc):raise ValueError('Not a pregame snapshot')
        untouched=protected(db)
        game_meta={'game_id':game_id,'home':game.home_team_id.removeprefix('nfl:'),
            'away':game.away_team_id.removeprefix('nfl:'),'kickoff':as_utc(game.kickoff_utc).isoformat()}
        db.rollback()
    cutoff=as_utc(saved['captured_at'])
    for row in saved['candidates']:
        row_cutoff=as_utc((row.get('forecast') or {}).get('data_cutoff'))
        if row_cutoff and row_cutoff>cutoff:raise ValueError('Candidate cutoff exceeds the saved capture time')
    model_path=settings.parquet_dir/'models/nfl_desk_combined_v11_candidate.json'
    model_bytes=model_path.read_bytes();model=json.loads(model_bytes)
    if hashlib.sha256(model_bytes).hexdigest()!=saved['model_artifact_sha256']:raise ValueError('Model hash mismatch')
    pbp_path=Path('data/cache/nflverse/play_by_play_2026.parquet')
    if datetime.fromtimestamp(pbp_path.stat().st_mtime,UTC)>cutoff:raise ValueError('Source file is newer than the saved prediction cutoff')
    fields=['game_id','game_date','play_id','posteam','defteam','fixed_drive','play_type','rush_attempt','pass_attempt',
        'qb_kneel','qb_spike','score_differential','rusher_player_id','receiver_player_id']
    frame=pd.read_parquet(pbp_path,columns=fields).drop_duplicates(['game_id','play_id'])
    # Date-only raw source: require the entire date to be older than 48 hours.
    frame=frame[pd.to_datetime(frame.game_date,utc=True)+pd.Timedelta(days=1)<pd.Timestamp(cutoff-timedelta(hours=48))]
    frame=frame[frame.play_type.isin(['run','pass']) & (frame.qb_kneel.fillna(0)!=1) & (frame.qb_spike.fillna(0)!=1)].copy()
    frame['state']=np.where(frame.score_differential>=8,'leading',np.where(frame.score_differential<=-8,'trailing','close'))
    frame['rush']=(frame.rush_attempt==1)&frame.rusher_player_id.notna()
    frame['rec']=(frame.pass_attempt==1)&frame.receiver_player_id.notna()
    frame=frame[frame.rush|frame.rec]
    if game_id.removeprefix('nfl:') in set(frame.game_id):raise ValueError('Target game entered historical inputs')
    candidates=[r for r in saved['candidates'] if not exclusions(r)]
    teams={};pace_samples=[];selected_frames=[]
    for tid in (game_meta['away'],game_meta['home']):
        opp=game_meta['home'] if tid==game_meta['away'] else game_meta['away']
        rows=[r for r in candidates if r['team']==tid]
        if not rows:raise ValueError('No candidates for '+tid)
        def last_six(column,value):
            subset=frame[frame[column]==value]
            gids=subset[['game_id','game_date']].drop_duplicates().sort_values(['game_date','game_id']).tail(6).game_id.tolist()
            return subset[subset.game_id.isin(gids)],gids
        own,gids=last_six('posteam',tid);allowed,defgids=last_six('defteam',opp)
        if len(gids)<2 or len(defgids)<2:raise ValueError('Insufficient prior games')
        selected_frames.extend([own,allowed])
        drives=own.groupby('game_id').fixed_drive.nunique().to_numpy(dtype=float)
        opp_drives=allowed.groupby('game_id').fixed_drive.nunique().to_numpy(dtype=float)
        possessions=float((drives.mean()+opp_drives.mean())/2)
        pace_samples.extend(drives.tolist()+opp_drives.tolist())
        players=[{'id':r['player_id'],'name':r['player_name'],'position':r['position']} for r in rows]
        totals=rows[0]['desk_analysis']['combination']['team_expected_tds']
        usage={p['id']:{} for p in players};usage_totals={};scorer_shares={}
        for ch,field in (('rush','rusher_player_id'),('rec','receiver_player_id')):
            plays=own[own[ch]]
            counts=lambda x:{'total':len(x),'latest':int((x.game_id==gids[-1]).sum()),
                'states':{s:int((x.state==s).sum()) for s in ('close','leading','trailing')}}
            usage_totals[ch]=counts(plays)
            for p in players:usage[p['id']][ch]=counts(plays[plays[field]==p['id'].removeprefix('nfl:')])
            weights=[r['desk_analysis']['combination']['scoring_paths'][ch+'_lambda']/totals[ch] for r in rows]
            if sum(weights)>1.00001:raise ValueError('Player TD mass exceeds team budget')
            scorer_shares[ch]=weights+[max(0,1-sum(weights))]
        teams[tid]={'players':players,'expected_tds':totals,'scorer_shares':scorer_shares,
            'usage':usage,'usage_totals':usage_totals,'expected_possessions':possessions,'measured_drive_count':int(drives.sum()),
            'implied_points':saved['market_context']['away_implied_points' if tid==game_meta['away'] else 'home_implied_points'],
            'offense_games':gids,'defense_games':defgids}
    inputs={'frozen_at':now.isoformat(),'information_cutoff':saved['captured_at'],'game':game_meta,
        'experiment_type':'historical_replay' if historical_replay else 'prospective',
        'replay_note':'Run after kickoff using saved pregame inputs; not an independent prospective prediction.' if historical_replay else None,
        'teams':teams,'possession_samples':pace_samples,'saved_board':saved,'model':model,
        'source_pbp_sha256':hashlib.sha256(pbp_path.read_bytes()).hexdigest(),
        'lions_snapshots':untouched,'candidate_snapshot_sha256':digest(saved),
        'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()}
    output.mkdir(parents=True)
    dump(output/'frozen-inputs.json',inputs)
    pd.concat(selected_frames).drop_duplicates(['game_id','play_id']).to_parquet(output/'prior-game-inputs.parquet',index=False)
    return inputs


def build_plan(inputs,unavailable_players=()):
    by_name={r['player_name']:r['player_id'] for r in inputs['saved_board']['candidates']}
    plan=[('poisson_control',{'mode':'poisson_control'}),('static_drives',{'mode':'static_drives'}),
        ('gameflow',{}),('slower_game',{'pace':.85}),('faster_game',{'pace':1.15}),
        ('stronger_game_script',{'script_strength':2.}),('recent_rb_roles',{'recent_role':True})]
    for name in unavailable_players:
        if name not in by_name:raise ValueError('Unavailable scenario player is not a saved candidate: '+name)
        plan.append((name.lower().replace(' ','_')+'_unavailable',{'unavailable':(by_name[name],)}))
    return plan


def run(inputs,output,draws,*,unavailable_players=()):
    saved=inputs['saved_board'];model=inputs['model'];rows=saved['candidates']
    by_name={r['player_name']:r['player_id'] for r in rows}
    plan=build_plan(inputs,unavailable_players)
    original_names=[saved['decision'][lane]['name'] for lane in ('most_likely','value_candidate') if saved['decision'].get(lane)]
    assumptions=['Saved eligible players assumed to play; any unavailable-player scenarios are explicit sensitivities, without invented absence probabilities.',
        'Regulation possessions only: no overtime, defensive/special-teams touchdowns, two-point tries or detailed yard-by-yard play simulation.',
        'Expected team TDs and player scoring weights retained from the frozen Desk model; shared possessions sampled from prior games.',
        'Per-game TD efficiency beta concentration equals measured offense drive count plus 25; this uncertainty rule is not fitted.',
        'Run/receiving TD mix responds to observed game-state opportunity odds, shrunk by 25 plays and bounded to a 0.75 log-odds change.',
        'Player state shares shrink by 25 opportunities and ratio is capped to 0.5–2 times their overall share.',
        'Recent-RB scenario uses latest-game RB opportunity shares, with eight prior opportunities and ratio capped to 0.25–4.',
        'Unavailable-player scenario distributes half the absent work to same-position teammates and reserves half in OTHER.',
        'QB scores are rushing only in this test; rare QB receptions are not modeled. Passing TDs always go to receivers.',
        'Existing calibration is reused only to compare with the displayed Desk percentages; it was not fitted to these simulations.',
        'These runs use the same underlying data and are not independent confirmation or evidence of improved accuracy.',
        'Saved sportsbook quotes are references, not verified executable prices at report time. No betting-return claim.']
    dump(output/'run-plan.json',{'saved_before_simulation':datetime.now(UTC).isoformat(),'draws_per_variant':draws,
        'experiment_type':inputs['experiment_type'],'information_cutoff':inputs['information_cutoff'],
        'seed':20261115,'variants':plan,'assumptions':assumptions,'selection':'Keep the current main/alternative policy; no retuning after simulation output.'})
    from spe.models.player_td.scoring_paths import calibrated
    from spe.models.player_td.scoring_decision import choose
    results={};summaries={}
    for name,options in plan:
        result=simulate(inputs,draws=draws,seed=20261115,**options)
        lookup={r['id']:r for r in result['players']};prospective=[]
        for source in rows:
            if source['player_id'] not in lookup:continue
            row=deepcopy(source);out=lookup[row['player_id']];path=row['desk_analysis']['combination']['scoring_paths']
            estimate=calibrated(model,path['baseline_probability'],out['raw_probability'])
            se=out['mc_se']
            mapped_low=calibrated(model,path['baseline_probability'],max(1e-6,out['raw_probability']-1.96*se))
            mapped_high=calibrated(model,path['baseline_probability'],min(1-1e-6,out['raw_probability']+1.96*se))
            out.update(board_probability=source['forecast']['p_estimate'],comparison_probability=estimate,
                monte_carlo_only_interval=[mapped_low,mapped_high],saved_odds=row['book_price'])
            row.update(game_id=inputs['game']['game_id'],opponent=next(t for t in inputs['teams'] if t!=row['team']))
            row['forecast']['p_estimate']=estimate
            # Price qualifications are intentionally not evaluated at report time.
            path.update(probability_low=min(path['probability_low'],estimate),probability_high=max(path['probability_high'],estimate))
            if row['player_id'] in options.get('unavailable',()):
                row['desk_analysis']['current_context']['availability']={'status':'out'}
            prospective.append(row)
        pick=choose(prospective)
        result['shadow_selection']={'main':pick['player_name'],'alternative':(pick['decision'].get('value_candidate') or {}).get('name'),
            'note':'Research comparison only; no publication or value-bet designation.'}
        results[name]=result
        summaries[name]={'main':result['shadow_selection']['main'],'alternative':result['shadow_selection']['alternative'],
            'original_pick_estimates':{player:lookup[by_name[player]]['comparison_probability'] for player in original_names}}
    # An independent seed measures simulation noise, not model correctness.
    repeat=simulate(inputs,draws=draws,seed=20261116)
    dump(output/'independent-seed-check.json',repeat)
    control=results['poisson_control']
    assert all(abs(r['comparison_probability']-r['board_probability'])<.012 for r in control['players'])
    with session_factory() as db:
        db.execute(text('PRAGMA query_only=ON'))
        now_protected=protected(db)
        assert now_protected==inputs['lions_snapshots'],'A concurrent process changed Lions records; investigate before reporting'
        db.rollback()
    final={'completed_at':datetime.now(UTC).isoformat(),'input_sha256':digest(inputs),'draws_per_variant':draws,
        'experiment_type':inputs['experiment_type'],'replay_note':inputs['replay_note'],
        'total_draws':draws*(len(plan)+1),'assumptions':assumptions,'summaries':summaries,
        'variants':results,'lions_snapshots_unchanged':len(now_protected),
        'application_writes':False,'model_artifacts_changed':False,'saved_board_selection':{
            'main':saved['decision']['most_likely']['name'],'alternative':(saved['decision'].get('value_candidate') or {}).get('name')}}
    dump(output/'results.json',final)
    print(json.dumps({'total_draws':final['total_draws'],'lions_records_unchanged':len(now_protected),'summaries':summaries},indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--game-id',required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--draws',type=int,default=50000)
    p.add_argument('--historical-replay',action='store_true',help='Use a saved pregame snapshot after kickoff, labeled as a replay')
    p.add_argument('--unavailable-player',action='append',default=[],help='Exact saved name for an optional absence sensitivity')
    a=p.parse_args()
    inputs=freeze(a.game_id,a.output,historical_replay=a.historical_replay)
    run(inputs,a.output,a.draws,unavailable_players=a.unavailable_player)
