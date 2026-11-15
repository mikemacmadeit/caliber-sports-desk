"""Join official play-stat identities to stored plays using exact IDs only.

Receiving yardage identifies a receiver on that play, not every incomplete target.
Unresolved/ambiguous identities remain null and reduce downstream coverage.
"""
from collections import defaultdict
from datetime import UTC, datetime

from spe.db.models import Game, Play, PlayerGameStat

RUSH = {"rushing yards", "rushing attempts", "rush attempts", "rushing touchdowns", "rush yards"}
RECEIVE = {"receiving yards", "receptions", "receiving touchdowns", "receiving targets", "targets", "target"}


def ingest_play_stats(db, records, *, observed_at=None):
    observed = observed_at or datetime.now(UTC)
    game_ids = {str(r.get("gameId")) for r in records}
    games = {g.game_id:g for g in db.query(Game).filter(Game.league_id=="fbs",Game.status=="final")
             if g.game_id.split(":")[-1] in game_ids}
    plays = {(p.game_id.split(":")[-1],p.play_id):p for p in db.query(Play).filter(
        Play.game_id.in_(games),Play.provider=="cfbd")}
    members = {(r.game_id,r.player_id,r.team_id) for r in db.query(PlayerGameStat).filter(PlayerGameStat.game_id.in_(games))}
    assignments=defaultdict(set)
    skipped=0
    for r in records:
        play=plays.get((str(r.get("gameId")),str(r.get("playId"))))
        kind=str(r.get("statType") or "").strip().lower()
        field="rusher_player_id" if kind in RUSH else "receiver_player_id" if kind in RECEIVE else None
        pid="fbs:"+str(r.get("athleteId"))
        if not play or not field or (play.game_id,pid,play.offense_id) not in members:
            skipped+=1
            continue
        if (field=="rusher_player_id" and not play.is_rush) or (field=="receiver_player_id" and not play.is_pass):
            skipped+=1
            continue
        assignments[(play,field)].add(pid)
    updated=conflicts=0
    for (play,field),ids in assignments.items():
        existing=getattr(play,field)
        if len(ids)!=1 or (existing is not None and existing not in ids):
            conflicts+=1
            continue
        pid=next(iter(ids))
        if existing!=pid:
            setattr(play,field,pid)
            play.observed_at=play.ingested_at=observed
            play.revision+=1
            updated+=1
    return {"updated":updated,"unresolved_or_unsupported":skipped,"conflicts":conflicts}


def refresh_play_stats(db, *, year, week, adapter=None):
    from spe.adapters.cfbd import CfbdAdapter
    result=(adapter or CfbdAdapter()).fetch("play_stats",{"year":year,"week":week})
    return ingest_play_stats(db,result.records,observed_at=result.observed_at)
