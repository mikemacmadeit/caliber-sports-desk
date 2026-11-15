"""Complete free prior-game boxes, with honest collection timestamps."""
import json
import math
from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select
from spe.config import settings
from spe.db.base import as_utc
from spe.db.models import Game, Player, Team
from spe.ingest.espn_box import _fetch_summary
from spe.integrity.assignments import canonical_team_map
from spe.models.player_td.desk_opportunity import canonical_team


def parse_box(payload, game, players, aliases, observed):
    comps=(payload or {}).get('header',{}).get('competitions',[])
    if not any(str(c.get('id'))==game.game_id.split(':')[-1] and c.get('status',{}).get('type',{}).get('completed') for c in comps):
        return []
    rows=[];seen=set()
    expected={canonical_team(game.home_team_id,aliases),canonical_team(game.away_team_id,aliases)}
    for block in (payload.get('boxscore') or {}).get('players',[]):
        team=canonical_team('fbs:'+str(block.get('team',{}).get('id')),aliases)
        if team not in expected:return []
        groups={g.get('name'):g for g in block.get('statistics',[])}
        if not {'rushing','receiving'}.issubset(groups):return []
        seen.add(team);people={}
        for kind in ('passing','rushing','receiving'):
            group=groups.get(kind,{})
            for item in group.get('athletes',[]):
                athlete=item.get('athlete',{});pid='fbs:'+str(athlete.get('id'))
                player=players.get(pid)
                if not player or player.position_group not in ('QB','RB','FB','WR','TE'):continue
                values=dict(zip(group.get('labels',[]),item.get('stats',[])))
                if kind=='passing':
                    try:
                        if int(values.get('C/ATT','').split('/')[1])<=0:continue
                    except (ValueError,IndexError):continue
                row=people.setdefault(pid,dict(game_id=game.game_id,player_id=pid,team_id=team,
                    home_team_id=game.home_team_id,away_team_id=game.away_team_id,
                    kickoff_utc=as_utc(game.kickoff_utc).isoformat(),position=player.position_group,
                    provider='espn.complete_box',player_name=player.name,carries=0,receptions=0,
                    rush_td=0,rec_td=0,rush_yards=0,rec_yards=0,targets=None,gl_carries=None,rz_targets=None,
                    observed_at=observed.isoformat()))
                mapping={'CAR':'carries','YDS':'rush_yards','TD':'rush_td'} if kind=='rushing' else {'REC':'receptions','YDS':'rec_yards','TD':'rec_td'} if kind=='receiving' else {}
                for field,key in mapping.items():
                    try:value=float(values[field])
                    except (ValueError,KeyError,TypeError):return []
                    if not math.isfinite(value):return []
                    row[key]=value
        rows.extend(people.values())
    return rows if seen==expected else []


def refresh_recent_boxes(db, date, fetch=_fetch_summary):
    from zoneinfo import ZoneInfo
    start=datetime.fromisoformat(date).replace(tzinfo=ZoneInfo('America/Chicago')).astimezone(UTC)
    games=db.scalars(select(Game).where(Game.league_id=='fbs',Game.data_tier.notin_(('demo','alias')))).all()
    aliases=canonical_team_map(db.scalars(select(Team)).all())
    players={p.player_id:p for p in db.scalars(select(Player))}
    teams={canonical_team(t,aliases) for g in games if start<=as_utc(g.kickoff_utc)<start+timedelta(days=1) for t in (g.home_team_id,g.away_team_id)}
    selected={}
    for team in teams:
        prior=sorted([g for g in games if g.status=='final' and start-timedelta(days=30)<=as_utc(g.kickoff_utc)<start and g.game_id.startswith('espn:') and team in {canonical_team(g.home_team_id,aliases),canonical_team(g.away_team_id,aliases)}],key=lambda g:as_utc(g.kickoff_utc),reverse=True)
        for g in prior[:2]:selected[g.game_id]=g
    observed=datetime.now(UTC)
    def one(g):return g.game_id,parse_box(fetch('fbs',g.game_id.split(':')[-1]),g,players,aliases,observed)
    with ThreadPoolExecutor(max_workers=4) as pool:results=dict(pool.map(one,selected.values()))
    path=settings.parquet_dir/'desk_evidence'/'recent_complete_boxes.json';path.parent.mkdir(parents=True,exist_ok=True)
    prior=json.loads(path.read_text()) if path.exists() else {'games':{}}
    for gid,rows in results.items():
        if rows:
            entry={'rows':rows,'observed_at':observed.isoformat(),'source':f'https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary?event={gid.split(":")[-1]}'}
            prior['games'][gid]=retain_revisions(prior['games'].get(gid),entry)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(prior));temp.replace(path)
    return {'requested_games':len(selected),'complete_boxes':sum(bool(r) for r in results.values()),'player_rows':sum(map(len,results.values())),'failed_games':[g for g,r in results.items() if not r]}


def retain_revisions(previous, current):
    """Keep the first observation of unchanged data and older revised boxes."""
    if not previous:
        return current
    values=lambda entry: sorted(({k:v for k,v in row.items() if k!='observed_at'} for row in entry['rows']),key=lambda row:row['player_id'])
    if values(previous)==values(current):
        return previous
    old={k:v for k,v in previous.items() if k!='revisions'}
    return {**current,'revisions':[*previous.get('revisions',[]),old]}


def overlay_history(rows, cutoff, *, reconstructed=False):
    path=settings.parquet_dir/'desk_evidence'/'recent_complete_boxes.json'
    if not path.exists():return rows
    additions=[];replaced=set()
    for gid,entry in json.loads(path.read_text()).get('games',{}).items():
        versions=[*entry.get('revisions',[]),entry]
        eligible=[v for v in versions if reconstructed or as_utc(v['observed_at'])<=cutoff]
        if not eligible:continue
        entry=max(eligible,key=lambda v:as_utc(v['observed_at']))
        valid=[r for r in entry['rows'] if as_utc(r['kickoff_utc'])<cutoff-timedelta(hours=6)]
        if valid:replaced.add(gid);additions.extend(valid)
    return [r for r in rows if r['game_id'] not in replaced]+additions
