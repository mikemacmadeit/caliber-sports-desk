"""Selection confidence is distinct from touchdown probability and price value."""
from datetime import timedelta
from spe.db.base import as_utc


def readiness(row, cutoff):
    analysis=row.get('desk_analysis') or {}
    context=analysis.get('current_context') or {}
    report=context.get('availability') or {}
    f=analysis.get('features') or {}
    reasons=[]
    if report.get('status') not in ('available','active','healthy'):
        reasons.append('Playing status is not confirmed')
    else:
        published=as_utc(report.get('reported_at'));observed=as_utc(report.get('observed_at'))
        if not published or not observed or not (cutoff-timedelta(hours=24)<=published<=cutoff and observed<=cutoff):
            reasons.append('Availability was not verified before the prediction cutoff')
    depth=context.get('depth') or {}
    if row.get('position')=='QB' and depth.get('rank')!=1:
        reasons.append('Starting quarterback role is not verified')
    old=float(f.get('carries') or 0)+float(f.get('receptions') or 0)
    recent=float(f.get('role_carries') or 0)+float(f.get('role_receptions') or 0)
    if old>0 and 'role_recent_appearance_rate' in f and recent<old*.5:
        reasons.append('Recent workload is less than half the longer-term average')
    if f.get('role_recent_appearance_rate')==0:
        reasons.append('No measured role in the latest observed team games')
    return {'status':'provisional' if reasons else 'availability_checked', 'reasons':reasons,
            'note':'Availability checks cannot guarantee participation; historical role is not a game-day depth chart.'}
