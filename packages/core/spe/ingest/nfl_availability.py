"""Public ESPN NFL game designations, including IR; no active-list inference."""
import hashlib
import json
import re
import unicodedata
import urllib.request
from datetime import UTC, datetime, timedelta

from spe.config import settings
from spe.db.base import as_utc, new_id
from spe.db.models import Game, InjuryEvent
from spe.identity.roster import eligible_players_for_game
from spe.models.player_td.availability_status import normalize, GAME_STATUS
from spe.models.player_td.desk_opportunity import canonical_team

URL = 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries'


def name_key(name):
    from spe.identity.match import normalize_name
    ascii_name = unicodedata.normalize('NFKD', name).encode('ascii','ignore').decode()
    return re.sub(r'[^a-z0-9]', '', normalize_name(ascii_name))


def parse(payload):
    rows = []
    for group in payload.get('injuries', []):
        for item in group.get('injuries', []):
            athlete = item.get('athlete') or {}
            team = (athlete.get('team') or {}).get('abbreviation')
            status = normalize(item.get('status'))
            if not team or not athlete.get('displayName') or status not in GAME_STATUS:
                continue
            rows.append({'team':canonical_team('nfl:'+team, {}),
                'name':athlete['displayName'], 'status':status,
                'reported_at':as_utc(item.get('date')), 'source_id':str(item.get('id',''))})
    return rows


def refresh_nfl_availability(db, *, now=None, force=False, payload=None):
    now = as_utc(now) or datetime.now(UTC)
    path = settings.parquet_dir / 'desk_evidence/nfl_availability_refresh.json'
    if payload is None and not force and path.exists():
        last = json.loads(path.read_text())
        if 0 <= (now-as_utc(last['observed_at'])).total_seconds() < 300:
            return {**last, 'status':'fresh'}
    if payload is None:
        request = urllib.request.Request(URL, headers={'User-Agent':'CaliberSports/1.0','Accept':'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
        except Exception as exc:
            return {'status':'unavailable', 'reason':type(exc).__name__, 'source':URL}
    rows = parse(payload)
    games = db.query(Game).filter(Game.league_id=='nfl', Game.data_tier.notin_(('demo','alias')),
        Game.kickoff_utc>now, Game.kickoff_utc<=now+timedelta(hours=72),
        Game.status.in_(('scheduled','preview','pregame'))).all()
    report = {'status':'ok' if rows else 'empty', 'source':URL, 'observed_at':now.isoformat(),
        'games':len(games), 'source_rows':len(rows), 'inserted':0, 'matched':0, 'unmatched':[],
        'coverage_complete':False, 'payload_sha256':hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()}
    for game in games:
        catalog = {}
        for p in eligible_players_for_game(db,game,include_inactive_for_props=True,apply_eligibility_audit=False):
            # Injury ingestion must also resolve the inactive players that a
            # scoring candidate audit correctly excludes. Match within the
            # reported team; suffix normalization never permits ambiguous IDs.
            catalog.setdefault((canonical_team(p.team_id,{}),name_key(p.name)), []).append(p.player_id)
        teams = {canonical_team(game.home_team_id,{}),canonical_team(game.away_team_id,{})}
        for row in rows:
            if row['team'] not in teams or not row['reported_at'] or row['reported_at'] > now:
                continue
            # Old explicit Active news is not proof of this week's availability.
            if row['status'] in {'active','healthy','available','probable'} and now-row['reported_at'] > timedelta(days=7):
                continue
            ids = set(catalog.get((row['team'],name_key(row['name'])), []))
            if len(ids) != 1:
                report['unmatched'].append({'game_id':game.game_id, 'name':row['name'], 'status':row['status']})
                continue
            pid = ids.pop()
            report['matched'] += 1
            key = hashlib.sha256(f"{game.game_id}|{pid}|{row['status']}|{row['reported_at'].isoformat()}".encode()).hexdigest()
            if db.query(InjuryEvent).filter_by(provider='espn',provider_record_id='nfl-status:'+key).first():
                continue
            db.add(InjuryEvent(injury_event_id=new_id(), game_id=game.game_id,player_id=pid,
                designation=row['status'],reported_at=row['reported_at'],observed_at=now,effective_at=now,
                source=URL,provider='espn',provider_record_id='nfl-status:'+key,confidence=.8,
                coverage_complete=False,quality_status='ok'))
            report['inserted'] += 1
    db.flush()
    # Only persist freshness after the observations are durably committed.
    db.commit()
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,indent=2),encoding='utf-8')
    return report
