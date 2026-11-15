"""Explicit, bounded free-source refresh. No odds credits or purchases.

Use --college-week N to attempt one CFBD play-stat request. On rate limit,
stop; no retries, alternate keys or paid-tier changes. The local export still runs.
"""
import argparse
import json
from datetime import UTC, datetime

from spe.db.session import session_factory
from spe.ingest.desk_evidence import refresh_local_evidence

if __name__ == "__main__":
    p=argparse.ArgumentParser()
    p.add_argument('--college-week',type=int)
    p.add_argument('--year',type=int,default=datetime.now(UTC).year)
    p.add_argument('--weather',action='store_true')
    p.add_argument('--nfl',action='store_true')
    p.add_argument('--free-college',action='store_true')
    p.add_argument('--public-availability',action='store_true')
    p.add_argument('--situations',action='store_true')
    p.add_argument('--rosters',action='store_true')
    args=p.parse_args()
    report={}
    if args.rosters:
        from spe.ingest.college_rosters import refresh_college_rosters
        with session_factory() as db:
            report['rosters']=refresh_college_rosters(db)
            db.commit()
    if args.situations:
        from spe.ingest.college_situations import refresh_college_situations
        with session_factory() as db:
            report['situations']=refresh_college_situations(db)
    if args.free_college:
        from spe.ingest.free_college import refresh_free_college
        with session_factory() as db:
            report['free_college']=refresh_free_college(db, years=tuple(range(2023,args.year+1)),refresh=True)
    if args.public_availability:
        from spe.ingest.public_availability import refresh_public_availability
        with session_factory() as db:
            report['public_availability']=refresh_public_availability(db)
            db.commit()
    if args.college_week:
        from spe.ingest.cfbd_play_stats import refresh_play_stats
        try:
            with session_factory() as db:
                report['college']=refresh_play_stats(db,year=args.year,week=args.college_week)
                db.commit()
        except Exception as e:
            report['college']={'status':'blocked','reason':str(e)}
    if args.weather:
        from spe.ingest.nws_live import ingest_nws_forecasts
        with session_factory() as db:
            report['weather']=ingest_nws_forecasts(db,horizon_hours=48)
            db.commit()
    if args.nfl:
        from spe.ingest.nflverse_desk import refresh_nflverse_desk
        with session_factory() as db:
            report['nfl']=refresh_nflverse_desk(db,year=args.year)
            db.commit()
    report['local']=refresh_local_evidence(force=True)
    print(json.dumps(report,indent=2,default=str))
