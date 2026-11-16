"""Derive score-state, goal-line play calling and QB continuity from free PBP."""
import json
from pathlib import Path
import pandas as pd
from spe.config import settings
from spe.ingest.free_college import external_id
from spe.integrity.assignments import canonical_team_map
from spe.models.player_td.desk_opportunity import canonical_team
from spe.db.models import Team

COLUMNS = ['game_id','id','pos_team_id','homeTeamId','awayTeamId','status_type_completed',
           'penalty_no_play','penalty_negated_play','kneel_down','type.text','rush','pass','sack',
           'start.pos_score_diff','start.down','start.distance','start.yardsToEndzone','period',
           'statYardage','passer_player_id','pass_attempt']


def summarize(frame, games, aliases):
    f = frame.drop_duplicates(['game_id','id'],keep='last').copy()
    valid = f['status_type_completed'].fillna(False).astype(bool)
    for k in ('penalty_no_play','penalty_negated_play','kneel_down'):
        valid &= ~f[k].fillna(False).astype(bool)
    valid &= ~f['type.text'].fillna('').str.contains('spike|two.point|extra.point|conversion',case=False,regex=True)
    valid &= f['rush'].fillna(False).astype(bool) | f['pass'].fillna(False).astype(bool)
    f = f.loc[valid].copy()
    passing = f['pass'].fillna(False).astype(bool)
    score = f['start.pos_score_diff']
    field = f['start.yardsToEndzone'].between(0,100)
    masks = {'leading_pass_rate':score>=8, 'trailing_pass_rate':score<=-8,
             'close_pass_rate':score.between(-7,7), 'redzone_pass_rate':field & (f['start.yardsToEndzone']<=20),
             'goal_line_pass_rate':field & (f['start.yardsToEndzone']<=5),
             'early_down_pass_rate':f['start.down'].isin([1,2]),
             'late_lead_pass_rate':(score>=15)&(f['period']==4), 'sack_rate':passing,
             'short_yardage_success':f['start.distance'].between(1,2) & f['statYardage'].notna()}
    for key,mask in masks.items():
        numerator = f['sack'].fillna(False).astype(bool) if key=='sack_rate' else (
            f['statYardage']>=f['start.distance'] if key=='short_yardage_success' else passing)
        f[key+'_d'] = mask.astype(int)
        f[key+'_n'] = (mask & numerator).astype(int)
    numeric = [k+s for k in masks for s in ('_n','_d')]
    result=[]
    for (gid,tid), group in f.groupby(['game_id','pos_team_id']):
        team=canonical_team('fbs:'+str(external_id(tid)),aliases)
        game=games.get((external_id(gid),team))
        if not game:
            continue
        # Verify the actual game sides, rather than trusting a coincident ID.
        sides={canonical_team('fbs:'+str(external_id(group.iloc[0][k])),aliases) for k in ('homeTeamId','awayTeamId')}
        if sides!={game['team_id'],game['opponent_id']}:
            continue
        row={k:game[k] for k in ('game_id','team_id','opponent_id','kickoff_utc','observed_at')}
        row.update({k:int(group[k].sum()) for k in numeric})
        attempts=group.loc[group['pass_attempt'].fillna(False).astype(bool)]
        ids=attempts['passer_player_id'].map(external_id).dropna().value_counts()
        row.update(passer_coverage=len(attempts.dropna(subset=['passer_player_id']))/len(attempts) if len(attempts) else 0,
                   primary_passer=str(ids.index[0]) if len(ids) else None,
                   primary_passer_share=float(ids.iloc[0]/len(attempts)) if len(ids) else None)
        result.append(row)
    return result


def refresh_college_situations(db, *, years=None):
    path=settings.parquet_dir/'desk_evidence/fbs_free_opportunities.json'
    source=json.loads(path.read_text())
    # Only team-game identities and source metadata are used here.
    source.pop('players', None)
    from spe.ops.memory import release_processing_memory
    release_processing_memory()
    games={(r['game_id'].split(':')[-1],r['team_id']):r for r in source['opportunity_games']}
    aliases=canonical_team_map(db.query(Team).all())
    output=settings.parquet_dir/'desk_evidence/fbs_situations.json'
    previous=json.loads(output.read_text()) if output.exists() else {}
    from spe.db.base import as_utc
    def season(row):
        kickoff=as_utc(row['kickoff_utc'])
        return kickoff.year-int(kickoff.month<7)
    rows=[r for r in previous.get('teams',[]) if years is not None and season(r) not in years]
    for entry in source['sources']:
        name=entry['source'].rsplit('/',1)[-1]
        if years is not None and not any(name == f'play_by_play_{year}.parquet' for year in years):
            continue
        frame=pd.read_parquet(settings.parquet_dir.parent/'cache/sportsdataverse'/name,columns=COLUMNS)
        rows.extend(summarize(frame,games,aliases))
    body={'teams':rows,'sources':source['sources'],'note':'Historical score-state tendencies, not future play calls or individual line grades.'}
    tmp=output.with_suffix('.tmp');tmp.write_text(json.dumps(body,allow_nan=False));tmp.replace(output)
    return {'team_games':len(rows),'source':'cached SportsDataverse PBP','new_network_requests':0}
