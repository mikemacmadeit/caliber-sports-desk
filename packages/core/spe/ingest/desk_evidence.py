"""Export measured local plays for the Desk. No network or paid data access.

Only final games, valid plays and same-game identities are used. Stored CFBD
plays lack player IDs: that gap is reported, never filled with catches-as-targets.
"""
import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

from spe.integrity.assignments import canonical_team_map
from spe.models.player_td.desk_opportunity import canonical_team
from spe.models.player_td.desk_evidence import weighted


def export(db, output):
    aliases = canonical_team_map([SimpleNamespace(**{**dict(r), "aliases": json.loads(r["aliases"] or "[]")})
        for r in db.execute("SELECT team_id,league_id,aliases FROM teams")])
    db.execute("""CREATE TEMP VIEW valid_plays AS
      SELECT p.*,g.league_id,g.kickoff_utc FROM plays p JOIN games g USING(game_id)
      WHERE g.status='final' AND g.data_tier NOT IN ('alias','demo')
      AND g.kickoff_utc >= '2023-07-01' AND p.quality_status NOT IN ('invalid','quarantined')
      AND p.offense_id IN (g.home_team_id,g.away_team_id)
      AND p.defense_id IN (g.home_team_id,g.away_team_id) AND p.offense_id != p.defense_id
      AND p.is_scrimmage=1 AND p.play_type NOT LIKE '%kneel%'
      AND p.play_type NOT LIKE '%spike%' AND p.play_type NOT LIKE '%penalty%'
      AND p.play_type NOT LIKE '%two%point%' AND p.period <= 4
    """)
    # Aggregated source provenance remains attached to every game.
    expressions = []
    for kind in ("pass", "rush"):
        expressions.extend([
            f"avg(case when is_{kind}=1 then epa end) as {kind}_epa",
            f"avg(case when is_{kind}=1 and epa is not null then epa>0 end) as {kind}_success",
            f"avg(case when is_{kind}=1 then yards end) as {kind}_yards"])
    sql = """SELECT game_id,league_id,kickoff_utc,offense_id as team_id,defense_id as opponent_id,
        max(observed_at) as observed_at,count(*) as plays,
        avg(case when is_pass=1 then yards>=20 else yards>=10 end) as explosive_rate,
        avg(case when abs(score_before_off-score_before_def)<=7 and period<=3 then is_pass end) as neutral_pass_rate,
        count(distinct case when yardline between 0 and 20 then drive_id end) as red_zone_drives,
        1.0*sum(case when yardline between 0 and 20 and is_td=1 and is_turnover=0 then 1 else 0 end)/
          nullif(count(distinct case when yardline between 0 and 20 then drive_id end),0) as red_zone_td_rate,
        avg(yardline between 0 and 100) as field_coverage,
        avg(case when is_rush=1 then rusher_player_id is not null
             when is_pass=1 and play_type not like '%sack%' then receiver_player_id is not null end) as identity_coverage,
        """ + ",".join(expressions) + " FROM valid_plays GROUP BY game_id,offense_id ORDER BY kickoff_utc"
    raw_teams = [dict(r) for r in db.execute(sql)]
    unique = {}
    for r in raw_teams:
        key = (r["kickoff_utc"][:10], canonical_team(r["team_id"], aliases), canonical_team(r["opponent_id"], aliases))
        if key not in unique or r["plays"] > unique[key]["plays"]:
            unique[key] = r
    teams = sorted(unique.values(), key=lambda r:r["kickoff_utc"])
    # Pace uses consecutive offensive snaps in the same drive and quarter only.
    pace = {(r["game_id"],r["team_id"]):r["pace"] for r in db.execute("""
      WITH ordered AS (SELECT game_id,offense_id as team_id,period,drive_id,game_seconds_remaining,
        lag(game_seconds_remaining) OVER(PARTITION BY game_id,offense_id,period,drive_id ORDER BY game_seconds_remaining DESC) as previous
        FROM valid_plays WHERE game_seconds_remaining IS NOT NULL AND drive_id IS NOT NULL)
      SELECT game_id,team_id,avg(previous-game_seconds_remaining) as pace FROM ordered
      WHERE previous-game_seconds_remaining between 1 and 60 GROUP BY game_id,team_id
    """)}
    by_offense, by_defense = defaultdict(list), defaultdict(list)
    team_index = {}
    for r in teams:
        r["pace"] = pace.get((r["game_id"],r["team_id"]))
        for k in ("team_id", "opponent_id"):
            r[k] = canonical_team(r[k], aliases)
        season = r["kickoff_utc"][:4] if r["kickoff_utc"][5:7] >= "07" else str(int(r["kickoff_utc"][:4])-1)
        # Opponent baseline excludes the current game and all later games.
        prior_def = by_defense[(season,r["opponent_id"])][-6:]
        for kind in ("pass", "rush"):
            baseline = weighted(prior_def,kind+"_epa") if len(prior_def)>=2 else None
            value = r[kind+"_epa"]
            r[kind+"_schedule_residual"] = value-baseline if value is not None and baseline is not None else None
            prior_off = by_offense[(season,r["team_id"])][-6:]
            off_baseline = weighted(prior_off,kind+"_epa") if len(prior_off)>=2 else None
            r[kind+"_allowed_residual"] = value-off_baseline if value is not None and off_baseline is not None else None
        if r["field_coverage"] is None or r["field_coverage"]<.98:
            r["red_zone_drives"] = r["red_zone_td_rate"] = None
        by_offense[(season,r["team_id"])].append(r)
        by_defense[(season,r["opponent_id"])].append(r)
        team_index[(r["game_id"],r["team_id"])] = r
    players = []
    for r0 in db.execute("""WITH touches AS (
       SELECT game_id,league_id,kickoff_utc,offense_id as team_id,rusher_player_id as player_id,
       observed_at,yardline,1 as carry,0 as target FROM valid_plays WHERE is_rush=1 AND rusher_player_id IS NOT NULL
       UNION ALL
       SELECT game_id,league_id,kickoff_utc,offense_id,receiver_player_id,observed_at,yardline,0,1
       FROM valid_plays WHERE is_pass=1 AND receiver_player_id IS NOT NULL AND play_type NOT LIKE '%sack%')
       SELECT game_id,league_id,kickoff_utc,team_id,player_id,max(observed_at) as observed_at,
       sum(target) as targets,sum(carry*(yardline between 0 and 20)) as inside20_carries,
       sum(carry*(yardline between 0 and 5)) as inside5_carries,
       sum(target*(yardline between 0 and 20)) as inside20_targets,
       sum(target*(yardline between 0 and 5)) as inside5_targets
       FROM touches GROUP BY game_id,team_id,player_id"""):
        r = dict(r0)
        r["team_id"] = canonical_team(r["team_id"],aliases)
        source = team_index.get((r["game_id"],r["team_id"]),{})
        if not source:
            continue
        r["identity_coverage"] = min(source.get("identity_coverage") or 0,source.get("field_coverage") or 0)
        players.append(r)
    output.mkdir(parents=True,exist_ok=True)
    report={}
    for league in ("nfl","fbs"):
        payload={"teams":[r for r in teams if r["league_id"]==league],
                 "players":[r for r in players if r["league_id"]==league],
                 "availability":"Revised historical sources; replay assumes availability 48 hours after kickoff."}
        free_path = output / "fbs_free_opportunities.json"
        if league == "fbs" and free_path.exists():
            free = json.loads(free_path.read_text())
            payload["players"] = free["players"]
            payload["opportunity_games"] = free["opportunity_games"]
            payload["sources"] = free["sources"]
        situation_path = output / "fbs_situations.json"
        if league == "fbs" and situation_path.exists():
            payload["situations"] = json.loads(situation_path.read_text())["teams"]
        path=output/(league+".json")
        tmp=path.with_suffix('.tmp')
        tmp.write_text(json.dumps(payload,allow_nan=False),encoding='utf-8')
        tmp.replace(path)
        report[league]={"team_games":len(payload['teams']),"player_games":len(payload['players']),
                        "latest_game":max((r['kickoff_utc'] for r in payload['teams']),default=None)}
    return report


def refresh_local_evidence(*, force=False):
    """Bounded local refresh; no downloads and no change to original source rows."""
    import time
    from sqlalchemy.engine import make_url
    from spe.config import settings
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return {"status": "requires_export_job"}
    output = settings.parquet_dir / "desk_evidence"
    paths = [output/(lg+".json") for lg in ("nfl","fbs")]
    if not force and all(p.exists() and time.time()-p.stat().st_mtime < 21600 for p in paths):
        return {"status": "fresh"}
    with sqlite3.connect(Path(url.database).resolve().as_uri()+"?mode=ro",uri=True) as db:
        db.row_factory=sqlite3.Row
        return {"status":"refreshed",**export(db,output)}


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--database',type=Path,default=Path('data/caliber.db'))
    p.add_argument('--output',type=Path,default=Path('data/parquet/desk_evidence'))
    args=p.parse_args()
    with sqlite3.connect(args.database.resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        print(json.dumps(export(db,args.output),indent=2))
