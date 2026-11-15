"""Official conference public availability feeds; read-only public endpoint.

Uses the public report endpoint embedded by the SEC, also serving ACC and
American public reports. No login, private endpoint or report submission. Unmatched names are
reported, not fuzzy-matched. Missing players are never assumed available.
"""
import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from spe.config import settings
from spe.db.base import as_utc
from spe.db.models import Game, InjuryEvent, Player, PlayerGameStat, Team
from spe.models.player_td.current_role import _person

PUBLIC_ENDPOINT = "https://app.hdintelligence.com/api/get-publish-public"
PUBLIC_SOURCES = {"SEC":"https://www.secsports.com/fbreports", "ACC":PUBLIC_ENDPOINT,
                  "American":"https://theamerican.org/sports/2026/8/6/football_player_availability.aspx"}
ZONES = {"CT":"America/Chicago","ET":"America/New_York","MT":"America/Denver","PT":"America/Los_Angeles"}


def status_code(value):
    value=str(value or '').strip().lower()
    if "half" in value:
        return "uncertain"  # Partial suspension is not a full-game inactive.
    return {"out":"out","available":"available","probable":"probable","questionable":"questionable",
            "doubtful":"doubtful","game time decision":"uncertain"}.get(value)


def normalize_reports(payload, conference, observed_at):
    normalized=[]
    for report_id,report in payload.items():
        if not isinstance(report,dict) or not report.get('games'):
            continue
        zone=ZONES.get(report.get('conferenceTimeZone'))
        if zone is None:
            continue
        try:
            stamp=datetime.fromisoformat(report['publishDate']+'T'+report['postedTime']).replace(tzinfo=ZoneInfo(zone)).astimezone(UTC)
            day=report['footer']['date']
        except (KeyError,ValueError,TypeError):
            continue
        if stamp>observed_at or observed_at-stamp>timedelta(days=7):
            continue
        teams=report['games']
        if len(teams)!=2:
            continue
        for team in teams:
            opponent=teams[1] if team is teams[0] else teams[0]
            for entry in team.get('rows',[]):
                status=status_code(entry.get('status'))
                if status is None:
                    continue
                name=re.sub(r'^\S+\s+#\S+\s+','',entry.get('name','')).strip()
                if not name:
                    continue
                normalized.append({'report_id':str(report_id),'conference':conference,'game_date':day,
                    'team':team.get('teamDisplayName') or team.get('teamName'),
                    'opponent':opponent.get('teamDisplayName') or opponent.get('teamName'),
                    'name':name,'status':status,'reported_at':stamp,'observed_at':observed_at,
                    'original_status':entry['status']})
    return normalized


def store_reports(db, rows):
    names=defaultdict(set)
    for t in db.query(Team).filter(Team.league_id=='fbs'):
        for name in [t.name,t.abbreviation,*(t.aliases or [])]:
            if isinstance(name,str):names[_person(name)].add(t.team_id)
    games=db.query(Game).filter(Game.league_id=='fbs',Game.data_tier.notin_(('demo','alias'))).all()
    # Map within each game's historical teams. The latest roster is not a past roster.
    players={p.player_id:p for p in db.query(Player)}
    members=defaultdict(set)
    earliest=min((r['reported_at'] for r in rows),default=datetime.now(UTC))
    season=earliest.year-int(earliest.month<7)
    for s in db.query(PlayerGameStat).join(Game).filter(Game.league_id=='fbs',Game.kickoff_utc>=f'{season}-07-01',
            PlayerGameStat.quality_status.notin_(('invalid','quarantined'))):
        if s.team_id and s.player_id in players:
            members[(s.team_id,_person(players[s.player_id].name))].add(s.player_id)
    existing={r.provider_record_id for r in db.query(InjuryEvent).filter(InjuryEvent.provider=='conference_public')}
    roster_path=settings.parquet_dir/'desk_evidence/fbs_rosters.json'
    rosters=json.loads(roster_path.read_text()).get('players',[]) if roster_path.exists() else []
    count=unmatched=duplicates=0
    for row in rows:
        team_ids=names[_person(row['team'])];opp_ids=names[_person(row['opponent'])]
        matches=[g for g in games if as_utc(g.kickoff_utc).astimezone(ZoneInfo('America/Chicago')).date().isoformat()==row['game_date']
                 and ((g.home_team_id in team_ids and g.away_team_id in opp_ids) or (g.away_team_id in team_ids and g.home_team_id in opp_ids))]
        # Schedule aliases may identify one real contest; use the ESPN representation.
        if not matches or len({g.game_id.split(':')[-1] for g in matches})!=1:
            unmatched+=1;continue
        game=sorted(matches,key=lambda g:g.game_id.startswith('espn:'),reverse=True)[0]
        team=game.home_team_id if game.home_team_id in team_ids else game.away_team_id
        pids=set(members[(team,_person(row['name']))])
        pids.update(p['player_id'] for p in rosters if p['team_id']==team and p['player_id'] in players
                    and _person(p['name'])==_person(row['name']) and as_utc(p['observed_at'])<=row['observed_at']
                    and row['observed_at']-as_utc(p['observed_at'])<=timedelta(days=7))
        if len(pids)!=1:
            unmatched+=1;continue
        pid=next(iter(pids))
        for matched_game in matches:
            identity='|'.join([row['conference'],row['report_id'],matched_game.game_id,pid,row['reported_at'].isoformat(),row['status']])
            record_id=hashlib.sha256(identity.encode()).hexdigest()
            if record_id in existing:
                duplicates+=1;continue
            db.add(InjuryEvent(game_id=matched_game.game_id,player_id=pid,designation=row['status'],
                reported_at=row['reported_at'],source=PUBLIC_SOURCES[row['conference']],confidence=1.,coverage_complete=False,
                provider='conference_public',provider_record_id=record_id,observed_at=row['observed_at'],
                effective_at=row['reported_at'],quality_status='official_public_report'))
            existing.add(record_id);count+=1
    return {'inserted':count,'unmatched':unmatched,'duplicates':duplicates}


def refresh_public_availability(db, *, now=None, client=None):
    now=as_utc(now) or datetime.now(UTC)
    cache=settings.parquet_dir.parent/'cache'/'conference_public';cache.mkdir(parents=True,exist_ok=True)
    result={}
    for conf in PUBLIC_SOURCES:
        path=cache/(conf+'.json')
        try:
            fresh=path.exists() and now.timestamp()-path.stat().st_mtime<900
            if fresh:
                rows=json.loads(path.read_text())
                for row in rows:
                    row['observed_at']=as_utc(row['observed_at']);row['reported_at']=as_utc(row['reported_at'])
            else:
                response=(client or httpx).post(PUBLIC_ENDPOINT,json={'sport':'Football','organization':conf,'conference':conf},timeout=25)
                response.raise_for_status()
                rows=normalize_reports(response.json(),conf,now)
            stored=store_reports(db,rows)
            # Cache normalized public facts, never embedded images or HTML.
            if not fresh:path.write_text(json.dumps(rows,default=str))
            result[conf]={'status':'fresh' if fresh else 'ok','source_rows':len(rows),**stored}
        except (httpx.HTTPError,ValueError,TypeError) as exc:
            result[conf]={'status':'unavailable','reason':type(exc).__name__}
    return result
