"""Prospective selection checks. Policy thresholds, not fitted probability bonuses."""
import math
from datetime import timedelta
from spe.db.base import as_utc
from spe.market.odds import american_to_implied

POLICY_VERSION = "confirmed_role_value_v1"
MIN_CHANCE = .50
BLOCKER_LABELS = {
    'model_failed_validation':'model has not passed validation',
    'chance_below_50_percent':'estimated chance below 50%',
    'no_positive_price_edge':'price does not offer a positive estimated edge',
    'availability_not_confirmed':'player availability is unconfirmed or uncertain',
    'availability_report_stale_or_unverifiable':'availability report is stale or cannot be verified',
    'scoring_role_evidence_incomplete':'measured scoring-role history is incomplete',
}


def assess(row, cutoff, model_gate_passed):
    a=row.get('desk_analysis') or {};f=a.get('features') or {}
    p=(row.get('forecast') or {}).get('p_estimate');price=row.get('book_price')
    blockers=[]
    if not model_gate_passed:blockers.append('model_failed_validation')
    valid=isinstance(p,(int,float)) and math.isfinite(p) and 0<=p<=1
    if not valid or p<MIN_CHANCE:blockers.append('chance_below_50_percent')
    valid_price=isinstance(price,(int,float)) and math.isfinite(price) and abs(price)>=100
    implied=american_to_implied(price) if valid_price else None
    edge=p-implied if valid and implied is not None else None
    if edge is None or edge<=0:blockers.append('no_positive_price_edge')
    report=(a.get('current_context') or {}).get('availability') or {}
    published=as_utc(report.get('reported_at'));observed=as_utc(report.get('observed_at'))
    if report.get('status') not in ('available','active','healthy'):
        blockers.append('availability_not_confirmed')
    elif not published or not observed or not (cutoff-timedelta(hours=24)<=published<=cutoff and observed<=cutoff):
        blockers.append('availability_report_stale_or_unverifiable')
    role=(a.get('combination') or {})
    primary='rush' if role.get('rush_lambda',0)>role.get('rec_lambda',0) else 'target'
    if row.get('league_id')=='fbs':
        measured=bool(f.get(primary+'_pbp_known'))
    else:
        measured=bool(f.get('pbp_known') and f.get('snap_known'))
    if not measured or f.get('games',0)<2:
        blockers.append('scoring_role_evidence_incomplete')
    return {'policy_version':POLICY_VERSION,'eligible':not blockers,'blockers':blockers,
            'minimum_probability':MIN_CHANCE,'break_even_probability':implied,'estimated_edge':edge,
            'role_channel':primary,'note':'Eligibility checks do not establish profitability or guarantee a touchdown.'}
