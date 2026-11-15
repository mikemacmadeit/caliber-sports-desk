"""Explicit chance/price tradeoff, kept separate from learned probabilities."""
import math
from spe.db.base import as_utc
from spe.market.odds import american_to_implied

MAX_CHANCE_GAP = .05
MIN_MODEL_EDGE = .03
MAX_PRICE_AGE_HOURS = 2


def quote_value(row):
    price=row.get('book_price'); p=(row.get('forecast') or {}).get('p_estimate')
    cutoff=as_utc((row.get('forecast') or {}).get('data_cutoff'))
    captured=as_utc(row.get('source_timestamp'))
    source=as_utc(row.get('price_source_at') or row.get('source_timestamp'))
    if not all(isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) for x in (price,p)) or abs(price)<100 or not 0<p<=1:
        return None
    if not cutoff or not captured or not source or not 0 <= (cutoff-source).total_seconds() <= MAX_PRICE_AGE_HOURS*3600 or not source <= captured <= cutoff:
        return None
    decimal=1+price/100 if price>0 else 1+100/abs(price)
    return {'break_even':american_to_implied(price), 'model_edge':p-american_to_implied(price),
        'expected_return':p*decimal-1, 'decimal':decimal}


def choose(eligible):
    best=max(eligible,key=lambda r:(r['forecast']['p_estimate'],r['player_id']))
    top=best['forecast']['p_estimate']
    priced=[(r,quote_value(r)) for r in eligible if r['forecast']['p_estimate'] >= top-MAX_CHANCE_GAP-1e-12]
    qualified=[(r,v) for r,v in priced if v and v['model_edge']>=MIN_MODEL_EDGE and v['expected_return']>0]
    selected=max(qualified,key=lambda x:(x[1]['expected_return'],x[0]['forecast']['p_estimate'],x[0]['player_id']))[0] if qualified else best
    policy={'version':'chance_value_v1','maximum_chance_gap':MAX_CHANCE_GAP,
        'minimum_model_edge':MIN_MODEL_EDGE,'maximum_quote_age_hours':MAX_PRICE_AGE_HOURS,
        'best_chance_player':best['player_name'],'best_chance_probability':top,
        'chance_given_up':top-selected['forecast']['p_estimate'],
        'fresh_priced_candidates':sum(quote_value(r) is not None for r in eligible),
        'selected_price_value':quote_value(selected),
        'reason':'positive_model_edge_within_five_points_of_top' if qualified else 'highest_chance_no_qualifying_price',
        'note':'This is a declared selection preference, not a calibrated accuracy or profit guarantee.'}
    return selected,policy
