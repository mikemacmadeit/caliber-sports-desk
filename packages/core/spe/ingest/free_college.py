"""Free ESPN-derived SportsDataverse opportunities, separate from source box scores.

No season aggregates enter a prediction. Exact game/team/player IDs only; final
games only; nullified plays, kneels, spikes and conversions are excluded. Downloads
are cached with observation timestamps and hashes. Missing identities stay missing.
"""
import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pandas as pd

from spe.config import settings
from spe.db.base import as_utc
from spe.db.models import Game, Player, PlayerGameStat, ProviderIdMap, Team
from spe.integrity.assignments import canonical_team_map
from spe.models.player_td.desk_opportunity import canonical_team

BASE = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download"
PBP_COLUMNS = ["game_id", "id", "pos_team_id", "homeTeamId", "awayTeamId", "rush", "target",
    "penalty_no_play", "penalty_negated_play", "kneel_down", "sack", "type.text", "rusher_player_id",
    "receiver_player_id", "start.yardsToEndzone", "status_type_completed", "period"]


def external_id(value):
    if value is None or pd.isna(value):
        return None
    try:
        number = float(value)
        return str(int(number)) if number.is_integer() else None
    except (ValueError, TypeError):
        return None


def load_asset(year, *, refresh=False, client=None):
    root = settings.parquet_dir.parent / "cache" / "sportsdataverse"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"play_by_play_{year}.parquet"
    meta = path.with_suffix(".source.json")
    now = datetime.now(UTC)
    if path.exists() and refresh and now.timestamp()-path.stat().st_mtime < 21600:
        refresh = False
    if refresh or not path.exists():
        url = f"{BASE}/espn_cfb_pbp/{path.name}"
        response = (client or httpx).get(url, follow_redirects=True, timeout=90)
        response.raise_for_status()  # No automatic retry or paid fallback.
        tmp = path.with_suffix(".download")
        tmp.write_bytes(response.content)
        # Verify schema before replacing the last good file.
        pd.read_parquet(tmp, columns=PBP_COLUMNS)
        tmp.replace(path)
        meta.write_text(json.dumps({"source":url,"observed_at":now.isoformat(),
            "sha256":hashlib.sha256(response.content).hexdigest()}))
    if not meta.exists():
        meta.write_text(json.dumps({"source":f"{BASE}/espn_cfb_pbp/{path.name}",
            "observed_at":datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
            "sha256":hashlib.sha256(path.read_bytes()).hexdigest()}))
    return pd.read_parquet(path, columns=PBP_COLUMNS), json.loads(meta.read_text())


def summarize(frame, games, members, player_ids, aliases, observed_at):
    """Pure normalization boundary, shared by tests and the actual importer."""
    df = frame.drop_duplicates(["game_id", "id"], keep="last").copy()
    valid = df["status_type_completed"].fillna(False).astype(bool)
    for field in ("penalty_no_play", "penalty_negated_play", "kneel_down"):
        valid &= ~df[field].fillna(False).astype(bool)
    if 'sack' in df:
        valid &= ~df['sack'].fillna(False).astype(bool)
    valid &= ~df["type.text"].fillna("").str.contains("sack|kneel|spike|two.point|extra.point|conversion",case=False,regex=True)
    valid &= df["rush"].fillna(False).astype(bool) | df["target"].fillna(False).astype(bool)
    df = df.loc[valid]
    team_stats, player_stats = {}, {}
    unmatched_games = set()
    conflicts = 0
    for row in df.to_dict("records"):
        gid = external_id(row["game_id"])
        game = games.get(gid)
        if game is None:
            unmatched_games.add(gid)
            continue
        team = canonical_team("fbs:"+str(external_id(row["pos_team_id"])),aliases)
        home = canonical_team("fbs:"+str(external_id(row["homeTeamId"])),aliases)
        away = canonical_team("fbs:"+str(external_id(row["awayTeamId"])),aliases)
        if {home,away}!={game["home"],game["away"]} or team not in {home,away}:
            conflicts += 1
            continue
        key = (gid,team)
        t = team_stats.setdefault(key,{"game_id":game["game_id"],"team_id":team,
            "opponent_id":away if team==home else home,"kickoff_utc":game["kickoff_utc"],
            "observed_at":observed_at,"source":"sportsdataverse_espn", "touches":0,"identified":0,
            "field_known":0, "rushes":0,"targets":0,"identified_rushes":0,"identified_targets":0})
        kind = "rush" if pd.notna(row.get("rush")) and row["rush"] == True else "target"
        t["touches"] += 1
        t["rushes" if kind=="rush" else "targets"] += 1
        yard = row.get("start.yardsToEndzone")
        field_known = pd.notna(yard) and 0 <= float(yard) <= 100
        t["field_known"] += int(field_known)
        external = external_id(row["rusher_player_id" if kind=="rush" else "receiver_player_id"])
        player = player_ids.get(external)
        if not player or (gid,player,team) not in members:
            continue
        t["identified"] += 1
        t["identified_rushes" if kind=="rush" else "identified_targets"] += 1
        pk = (gid,team,player)
        p = player_stats.setdefault(pk,{k:v for k,v in t.items() if k in (
            "game_id","team_id","kickoff_utc","observed_at","source")})
        p["player_id"] = player
        for metric in ("targets","carries","inside20_carries","inside5_carries","inside20_targets","inside5_targets"):
            p.setdefault(metric,0)
        p["carries" if kind=="rush" else "targets"] += 1
        if field_known:
            for distance in (5,20):
                if float(yard)<=distance:
                    p[f"inside{distance}_"+("carries" if kind=="rush" else "targets")] += 1
    for key,t in team_stats.items():
        t["identity_coverage"] = t["identified"]/t["touches"]
        t["field_coverage"] = t["field_known"]/t["touches"]
        t["rush_coverage"] = t["identified_rushes"]/t["rushes"] if t["rushes"] else 1.
        t["target_coverage"] = t["identified_targets"]/t["targets"] if t["targets"] else 1.
    for (gid,team,_),p in player_stats.items():
        t=team_stats[(gid,team)]
        p["identity_coverage"]=min(t["identity_coverage"],t["field_coverage"])
        p["rush_coverage"]=min(t["rush_coverage"],t["field_coverage"])
        p["target_coverage"]=min(t["target_coverage"],t["field_coverage"])
    return {"opportunity_games":list(team_stats.values()),"players":list(player_stats.values())}, {
        "matched_team_games":len(team_stats),"player_games":len(player_stats),
        "unmatched_games":len(unmatched_games),"team_conflict_plays":conflicts,
        "identified_opportunities":sum(t["identified"] for t in team_stats.values()),
        "total_opportunities":sum(t["touches"] for t in team_stats.values()),
        "complete_team_games":sum(min(t['identity_coverage'],t['field_coverage'])>=.95 for t in team_stats.values())}


def refresh_free_college(db, *, years=(2023,2024,2025,2026), refresh=False):
    aliases=canonical_team_map(db.query(Team).all())
    candidates=defaultdict(list)
    for g in db.query(Game).filter(Game.league_id=="fbs",Game.status=="final",Game.data_tier.notin_(("demo","alias"))):
        candidates[g.game_id.split(":")[-1]].append(g)
    games={gid:{"game_id":sorted(gs,key=lambda g:g.game_id.startswith('espn:'),reverse=True)[0].game_id,
        "home":canonical_team(gs[0].home_team_id,aliases),"away":canonical_team(gs[0].away_team_id,aliases),
        "kickoff_utc":as_utc(gs[0].kickoff_utc).isoformat()} for gid,gs in candidates.items()}
    members={(r.game_id.split(":")[-1],r.player_id,canonical_team(r.team_id,aliases))
        for r in db.query(PlayerGameStat).join(Game).filter(Game.league_id=="fbs",Game.status=="final",
            PlayerGameStat.quality_status.notin_(("invalid","quarantined"))) if r.team_id}
    known={p.player_id for p in db.query(Player)}
    player_ids={p.removeprefix('fbs:'):p for p in known if p.startswith('fbs:')}
    for r in db.query(ProviderIdMap).filter(ProviderIdMap.provider=='espn',ProviderIdMap.entity_type=='player'):
        if r.canonical_id in known and r.confidence>=.99:
            player_ids[r.external_id]=r.canonical_id
    path=settings.parquet_dir/'desk_evidence'/'fbs_free_opportunities.json'
    prior=json.loads(path.read_text()) if path.exists() else {}
    def previous_season(row):
        kick=as_utc(row['kickoff_utc'])
        return kick.year-int(kick.month<7) not in years
    result={"opportunity_games":[r for r in prior.get('opportunity_games',[]) if previous_season(r)],
            "players":[r for r in prior.get('players',[]) if previous_season(r)],
            "sources":[r for r in prior.get('sources',[]) if not any(str(y) in str(r.get('source','')) for y in years)],
            "reports":{k:v for k,v in prior.get('reports',{}).items() if int(k) not in years}}
    for year in years:
        current=datetime.now(UTC)
        season=current.year-int(current.month<7)
        frame,source=load_asset(year,refresh=refresh and year>=season)
        payload,report=summarize(frame,games,members,player_ids,aliases,source['observed_at'])
        if not payload['opportunity_games'] or not payload['players']:
            raise ValueError("No matched college opportunities; retain previous evidence")
        for key in ('opportunity_games','players'):result[key].extend(payload[key])
        result['sources'].append(source);result['reports'][str(year)]=report
    path=settings.parquet_dir/'desk_evidence'/'fbs_free_opportunities.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(result,allow_nan=False));tmp.replace(path)
    return result['reports']
