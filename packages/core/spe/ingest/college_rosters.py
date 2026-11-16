"""Observed ESPN college rosters for identity and personnel context, not depth ranks."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import httpx
from spe.config import settings
from spe.db.base import as_utc
from spe.db.models import Game, Player


def normalize(payload, team, observed):
    if str(payload.get('team',{}).get('id')) != team.split(':')[-1]:
        raise ValueError('Roster team mismatch')
    season = payload.get('season',{}).get('year')
    if season != observed.year-int(observed.month<7):
        raise ValueError('Stale roster season')
    rows=[]
    for group in payload.get('athletes',[]):
        for p in group.get('items',[]):
            pid=str(p.get('id',''))
            if not pid.isdigit() or not p.get('displayName'):
                continue
            position=p.get('position',{}).get('abbreviation')
            if not position:
                continue
            rows.append({'player_id':'fbs:'+pid,'name':p['displayName'],'position':position,
                         'team_id':team,'season':season,'observed_at':observed.isoformat()})
    if not rows:
        raise ValueError('Empty roster')
    return rows


def refresh_college_rosters(db, now=None):
    now=as_utc(now) or datetime.now(UTC)
    games=db.query(Game).filter(Game.league_id=='fbs',Game.data_tier.notin_(('demo','alias')),
        Game.kickoff_utc>=now-timedelta(hours=24),Game.kickoff_utc<=now+timedelta(days=3)).all()
    teams=sorted({t for g in games for t in (g.home_team_id,g.away_team_id) if t.startswith('fbs:') and t.split(':')[-1].isdigit()})
    root=settings.parquet_dir.parent/'cache/college_rosters';root.mkdir(parents=True,exist_ok=True)
    def fetch(team):
        path=root/(team.split(':')[-1]+'.json')
        try:
            if path.exists() and now.timestamp()-path.stat().st_mtime<86400:
                body=json.loads(path.read_text())
            else:
                url=f'https://site.api.espn.com/apis/site/v2/sports/football/college-football/teams/{team.split(":")[-1]}/roster'
                response=httpx.get(url,timeout=20);response.raise_for_status()
                body={'source':url,'players':normalize(response.json(),team,now),'observed_at':now.isoformat()}
                tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(body));tmp.replace(path)
            return team,body,None
        except (httpx.HTTPError,ValueError,TypeError) as exc:
            return team,None,type(exc).__name__
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses=list(pool.map(fetch,teams))
    all_players=[];errors={};sources=[]
    known={p.player_id for p in db.query(Player.player_id)}
    added=0
    for team,body,error in responses:
        if error:
            errors[team]=error;continue
        sources.append(body['source']);all_players.extend(body['players'])
        for p in body['players']:
            if p['player_id'] not in known:
                pos=p['position'];group='OL' if pos in ('OT','OG','C','G','T','OL') else pos
                db.add(Player(player_id=p['player_id'],name=p['name'],position=pos,position_group=group))
                known.add(p['player_id']);added+=1
    output=settings.parquet_dir/'desk_evidence/fbs_rosters.json';output.parent.mkdir(parents=True,exist_ok=True)
    tmp=output.with_suffix('.tmp');tmp.write_text(json.dumps({'players':all_players,'sources':sources}));tmp.replace(output)
    db.flush()
    return {'teams':len(responses)-len(errors),'roster_memberships':len(all_players),'new_player_identities':added,'errors':errors,
            'limitation':'Roster membership is not starter rank, snap count, availability or injury severity.'}
