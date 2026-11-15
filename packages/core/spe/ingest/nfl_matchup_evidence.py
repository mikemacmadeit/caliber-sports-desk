"""Keep rich NFL matchup features current with the same cached PBP as the simulator.

The older SQLite play export can lag the independent nflverse Desk refresh.
Only completed games and observations available before the current cutoff enter.
No network calls, new statistics or inferred coverage assignments are used.
"""
from collections import defaultdict
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math

import pandas as pd

from spe.db.base import as_utc
from spe.models.player_td.desk_evidence import weighted


def _number(value):
    return float(value) if pd.notna(value) and math.isfinite(float(value)) else None


def aggregate(frame, games, observed):
    from spe.analytics.plays import nflverse_to_canonical
    canon = nflverse_to_canonical(frame.drop_duplicates(['game_id','play_id'],keep='last'))
    if canon.empty:return [],[]
    canon['game_id'] = 'nfl:'+canon.provider_game_id.astype(str)
    valid = canon.game_id.isin(games) & canon.is_scrimmage & (canon.period<=4)
    valid &= ~canon.play_type.fillna('').str.contains('kneel|spike|penalty|two.point',case=False,regex=True)
    canon = canon[valid].copy()
    teams,players=[],[]
    for (gid,abbr),plays in canon.groupby(['game_id','offense_abbr'],sort=False):
        team='nfl:'+str(abbr);game=games[gid]
        opponent=game.away_team_id if team==game.home_team_id else game.home_team_id
        if team not in (game.home_team_id,game.away_team_id):continue
        plays=plays[plays.defense_abbr.eq(opponent.removeprefix('nfl:'))]
        if plays.empty:continue
        y=plays.yardline;rz=y.between(0,20)
        neutral=(plays.score_off-plays.score_def).abs().le(7)&plays.period.le(3)
        field=_number(y.between(0,100).mean())
        target=plays.is_pass & ~plays.play_type.fillna('').str.contains('sack',case=False)
        identities=pd.concat([plays.loc[plays.is_rush,'rusher_player_id'].notna(),plays.loc[target,'receiver_player_id'].notna()])
        ordered=plays.sort_values('game_seconds_remaining',ascending=False)
        elapsed=-ordered.groupby(['period','drive_id']).game_seconds_remaining.diff()
        row={'game_id':gid,'league_id':'nfl','team_id':team,'opponent_id':opponent,
            'kickoff_utc':as_utc(game.kickoff_utc).isoformat(),'observed_at':as_utc(observed).isoformat(),
            'plays':len(plays),'explosive_rate':_number(((plays.is_pass&plays.yards.ge(20))|(~plays.is_pass&plays.yards.ge(10))).mean()),
            'neutral_pass_rate':_number(plays.loc[neutral,'is_pass'].mean()),
            'red_zone_drives':int(plays.loc[rz,'drive_id'].nunique()) if field>=.98 else None,
            'red_zone_td_rate':None,'field_coverage':field,'identity_coverage':_number(identities.mean()),
            'pace':_number(elapsed[elapsed.between(1,60)].mean()),'source':'nflverse_cached_pbp'}
        if row['red_zone_drives']:
            row['red_zone_td_rate']=float((rz&plays.is_td&~plays.is_turnover).sum())/row['red_zone_drives']
        for kind in ('pass','rush'):
            part=plays[plays['is_'+kind]]
            row[kind+'_epa']=_number(part.epa.mean())
            row[kind+'_success']=_number(part.loc[part.epa.notna(),'epa'].gt(0).mean())
            row[kind+'_yards']=_number(part.yards.fillna(0).mean())
        teams.append(row)
        touched=set(plays.loc[plays.is_rush,'rusher_player_id'].dropna())|set(plays.loc[plays.is_pass,'receiver_player_id'].dropna())
        for pid in touched:
            rush=plays.is_rush & plays.rusher_player_id.eq(pid)
            rec=target & plays.receiver_player_id.eq(pid)
            players.append({k:row[k] for k in ('game_id','league_id','kickoff_utc','team_id','observed_at')} | {
                'player_id':pid,'targets':int(rec.sum()),
                'inside20_carries':int((rush&rz).sum()),'inside5_carries':int((rush&y.between(0,5)).sum()),
                'inside20_targets':int((rec&rz).sum()),'inside5_targets':int((rec&y.between(0,5)).sum()),
                'identity_coverage':min(row['identity_coverage'] or 0,field or 0),'source':'nflverse_cached_pbp'})
    return teams,players


def refresh(session, *, year=None, now=None):
    from spe.config import settings
    from spe.db.models import Game
    now=as_utc(now) or datetime.now(UTC);year=year or now.year-int(now.month<7)
    source=settings.parquet_dir.resolve().parent/'cache/nflverse'/f'play_by_play_{year}.parquet'
    target=settings.parquet_dir/'desk_evidence/nfl.json'
    if not source.exists() or not target.exists():return {'status':'missing_source'}
    observed=datetime.fromtimestamp(source.stat().st_mtime,UTC)
    if observed>now:return {'status':'future_source_rejected'}
    payload=json.loads(target.read_text());signature=hashlib.sha256(source.read_bytes()).hexdigest()
    if payload.get('current_pbp_sha256')==signature:return {'status':'fresh'}
    games={g.game_id:g for g in session.query(Game).filter(Game.league_id=='nfl',Game.status=='final',
        Game.season_id==f'nfl-{year}',Game.kickoff_utc<now-timedelta(hours=48),Game.data_tier.notin_(('demo','alias')))}
    teams,players=aggregate(pd.read_parquet(source),games,observed)
    if not teams:return {'status':'empty_source_retained'}
    # Preserve older seasons and unavailable games. New measurements replace
    # complete represented games, including zero/vanished player opportunities.
    refreshed={(r['game_id'],r['team_id']) for r in teams}
    payload['teams']=[r for r in payload['teams'] if (r['game_id'],r['team_id']) not in refreshed]+teams
    payload['players']=[r for r in payload['players'] if (r['game_id'],r['team_id']) not in refreshed]+players
    offense,defense=defaultdict(list),defaultdict(list)
    for r in sorted(payload['teams'],key=lambda r:as_utc(r['kickoff_utc'])):
        kick=as_utc(r['kickoff_utc']);season=kick.year-int(kick.month<7)
        if r.get('source')=='nflverse_cached_pbp':
            for kind in ('pass','rush'):
                prior=defense[(season,r['opponent_id'])][-6:]
                baseline=weighted(prior,kind+'_epa') if len(prior)>=2 else None
                r[kind+'_schedule_residual']=r[kind+'_epa']-baseline if r[kind+'_epa'] is not None and baseline is not None else None
                prior=offense[(season,r['team_id'])][-6:]
                baseline=weighted(prior,kind+'_epa') if len(prior)>=2 else None
                r[kind+'_allowed_residual']=r[kind+'_epa']-baseline if r[kind+'_epa'] is not None and baseline is not None else None
        offense[(season,r['team_id'])].append(r);defense[(season,r['opponent_id'])].append(r)
    payload['current_pbp_sha256']=signature
    payload['current_pbp_observed_at']=observed.isoformat()
    temporary=target.with_suffix('.pbp.tmp');temporary.write_text(json.dumps(payload,allow_nan=False),encoding='utf-8');temporary.replace(target)
    return {'status':'updated','team_games':len(teams),'player_games':len(players),
            'latest_game':max(r['kickoff_utc'] for r in teams),'source_sha256':signature}
