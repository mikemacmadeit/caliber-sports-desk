from datetime import timedelta
import copy

import pytest

from spe.db.models import ClosingLine
from spe.models.player_td.desk_market import context_for_game, market_features, summary_market
from spe.models.player_td.desk_replay import select_replay_candidate
from spe.models.player_td.desk_combined import combined_prediction
from test_desk_combined import allocator, combined_model, CUTOFF
from test_desk_replay import td_row
from test_publication_pipeline import seed, NOW


def line(db, market, value, source="nflverse_consensus", observed=None):
    observed = observed or NOW-timedelta(hours=1)
    db.add(ClosingLine(game_id="g",market_id=market,home_line=value,source=source,
        captured_at=observed,observed_at=observed,effective_at=observed,provider="test",provider_record_id=market+str(value)))
    db.flush()


def test_nfl_margin_and_cfbd_handicap_have_consistent_direction(db):
    seed(db)
    line(db,"point_spread",14)
    line(db,"total",48)
    c=context_for_game(db,"g",NOW)
    assert c["home_handicap"] == -14
    assert c["home_implied_points"] == 31
    assert c["away_implied_points"] == 17
    assert market_features(c,False)["market_team_spread"] == 14
    db.query(ClosingLine).delete()
    line(db,"point_spread",-14,source="cfbd")
    line(db,"total",48,source="cfbd")
    assert context_for_game(db,"g",NOW)["home_implied_points"] == 31


def test_postkickoff_archive_cannot_be_used_as_live_pregame_evidence(db):
    seed(db)
    line(db,"point_spread",-14,source="espn_summary_market",observed=NOW+timedelta(hours=2))
    line(db,"total",48,source="espn_summary_market",observed=NOW+timedelta(hours=2))
    assert not context_for_game(db,"g",NOW)["usable"]
    retrospective=context_for_game(db,"g",NOW,replay=True)
    assert retrospective["usable"] and retrospective["retrospective_market_data"]


def test_missing_or_stale_market_is_not_assumed_safe(db):
    seed(db)
    line(db,"point_spread",10,observed=NOW-timedelta(days=5))
    line(db,"total",50,observed=NOW-timedelta(days=5))
    assert not context_for_game(db,"g",NOW)["usable"]


@pytest.mark.parametrize("change", ["no_price","low_total","missing_market"])
def test_no_unpriced_or_weak_market_fallback(change):
    row=td_row(chance=.8)
    if change=="no_price": row["book_price"]=None
    if change=="low_total": row["team_implied_points"]=18
    if change=="missing_market": row["market_context"]={"usable":False}
    assert select_replay_candidate([row]) is None


def test_market_changes_both_team_forecast_and_player_probability():
    from spe.models.player_td.desk_market import MARKET_FEATURES
    e=allocator();m=copy.deepcopy(combined_model())
    m["market_features"]=True
    base=m["player_model"]
    base["features"]+=MARKET_FEATURES
    base["center"] += [0]*4; base["scale"] += [1]*4
    base["coefficients"] += [-.04,0,0,0]
    for kind in ("rush","rec"): m["team_coefficients"][kind] += [-.3,0,0,0]
    results=[]
    for spread in (-14,14):
        market={"home_handicap":spread,"total":48}
        f=e.history.features("p","home","away","RB",CUTOFF,True)
        f.update(market_features(market,True))
        allocated=e.allocate(m,"home","away",CUTOFF,True,market=market)
        results.append(combined_prediction(m,f,allocated,"p"))
    assert results[0]["probability"] > results[1]["probability"]
    assert results[0]["team_expected_tds"]["rush"] > results[1]["team_expected_tds"]["rush"]


def test_college_total_not_extrapolated_into_untrained_feature():
    f=market_features({"home_handicap":-10,"total":60},True,include_total=False)
    assert f["market_team_spread"] == -10
    assert f["market_game_total"] == f["market_total_known"] == 0


def test_summary_spread_sign_and_total():
    payload={"pickcenter":[{"spread":15.5,"overUnder":51.5,"awayTeamOdds":{"favorite":True}}]}
    parsed=summary_market(payload,"event",NOW)
    assert parsed["home_handicap"] == 15.5
    assert (parsed["total"]-parsed["home_handicap"])/2 == 18


def test_repeated_archive_refresh_preserves_first_observation(db):
    from spe.models.player_td.desk_market import store_archive
    from spe.db.base import as_utc
    seed(db)
    first={'home_handicap':-7,'total':50,'observed_at':NOW.isoformat()}
    assert store_archive(db,{'g':first})==2
    later={**first,'total':51,'observed_at':(NOW+timedelta(minutes=15)).isoformat()}
    assert store_archive(db,{'g':later})==0
    row=db.query(ClosingLine).filter_by(game_id='g',market_id='total',source='espn_summary_market').one()
    assert row.home_line==50 and as_utc(row.observed_at)==NOW


def test_live_market_refresh_keeps_cutoff_history_and_rejects_postkickoff(db):
    from spe.models.player_td.desk_market import store_archive, store_pregame_quote
    from spe.db.models import Game, RawSnapshot
    seed(db)
    db.get(Game, 'g').kickoff_utc = NOW + timedelta(hours=2)
    old = {'home_handicap': -7, 'total': 50, 'observed_at': (NOW-timedelta(days=2)).isoformat()}
    store_archive(db, {'g': old})
    assert not context_for_game(db, 'g', NOW)['usable']
    first = {**old, 'observed_at': NOW.isoformat()}
    assert store_pregame_quote(db, 'g', first, captured_at=NOW)
    later = NOW + timedelta(hours=1)
    second = {**first, 'total': 54, 'observed_at': later.isoformat()}
    assert store_pregame_quote(db, 'g', second, captured_at=later)
    assert context_for_game(db, 'g', NOW)['total'] == 50
    assert context_for_game(db, 'g', later)['total'] == 54
    assert context_for_game(db, 'g', later)['home_implied_points'] == 30.5
    assert not store_pregame_quote(db, 'g', second, captured_at=NOW)
    assert not store_pregame_quote(db, 'g', second, captured_at=NOW+timedelta(hours=3))
    assert db.query(RawSnapshot).count() == 2
    assert db.query(ClosingLine).filter_by(game_id='g', market_id='total').one().home_line == 50
