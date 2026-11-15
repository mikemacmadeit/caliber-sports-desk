"""Free-source input lane. No paid API, subscriptions or model retraining."""
from datetime import UTC, datetime
from spe.config import settings


def refresh_free_desk_inputs(db, now=None):
    from spe.ingest.free_college import refresh_free_college
    from spe.ingest.public_availability import refresh_public_availability
    from spe.ingest.desk_evidence import refresh_local_evidence
    now=now or datetime.now(UTC)
    result={}
    path=settings.parquet_dir/'desk_evidence'/'fbs_free_opportunities.json'
    if path.exists() and now.timestamp()-path.stat().st_mtime<21600:
        result['college']={'status':'fresh'}
    else:
        year=now.year-int(now.month<7)
        try:
            result['college']=refresh_free_college(db,years=tuple(range(2023,year+1)),refresh=True)
            from spe.ingest.college_situations import refresh_college_situations
            result['situations']=refresh_college_situations(db)
            result['derived']=refresh_local_evidence(force=True)
        except Exception as exc:
            result['college']={'status':'unavailable','reason':type(exc).__name__}
    from spe.ingest.college_rosters import refresh_college_rosters
    result['rosters']=refresh_college_rosters(db,now=now)
    result['availability']=refresh_public_availability(db,now=now)
    from zoneinfo import ZoneInfo
    from spe.ingest.recent_td_boxes import refresh_recent_boxes
    path=settings.parquet_dir/'desk_evidence'/'recent_complete_boxes.json'
    if path.exists() and now.timestamp()-path.stat().st_mtime<21600:
        result['recent_boxes']={'status':'fresh'}
    else:
        try:result['recent_boxes']=refresh_recent_boxes(db,now.astimezone(ZoneInfo('America/Chicago')).date().isoformat())
        except Exception as exc:result['recent_boxes']={'status':'unavailable','reason':type(exc).__name__}
    return result
