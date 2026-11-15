"""Shared pregame eligibility. No result or same-game box score is inspected."""
import math

from spe.models.player_td.desk_combined import INACTIVE


def exclusions(row):
    analysis = row.get('desk_analysis') or {}
    context = analysis.get('current_context') or {}
    forecast = row.get('forecast') or {}
    from spe.models.player_td.availability_status import normalize
    status = normalize((context.get('availability') or {}).get('status'))
    forecast_status = normalize(forecast.get('availability_status'))
    reasons = []
    if status in INACTIVE | {'doubtful'} or forecast_status in INACTIVE | {'doubtful'}:
        reasons.append('Reported out, inactive, suspended or doubtful')
    if row.get('position') == 'QB' and ((context.get('depth') or {}).get('rank') or 1) > 1:
        reasons.append('Documented backup quarterback')
    f = analysis.get('features') or {}
    depth=context.get('depth') or {}
    if f.get('recent_measured_opportunity_games',0)>=2 and f.get('recent_measured_touches')==0 and (depth.get('rank') or 1)>1:
        reasons.append('No measured touches in the latest two team games and currently listed behind a starter')
    if f.get('role_complete_boxes', 0) >= 2 and f.get('role_recent_appearance_rate') == 0 and status not in ('available', 'active', 'healthy'):
        reasons.append('No recorded appearance in two complete prior team boxes; return unconfirmed')
    p = forecast.get('p_estimate')
    if not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 < p <= 1:
        reasons.append('Scoring probability unavailable')
    return reasons
