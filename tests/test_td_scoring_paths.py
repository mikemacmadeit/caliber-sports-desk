from copy import deepcopy
from datetime import timedelta
import math

import pandas as pd
import pytest

from spe.analytics.nflverse_opportunities import pbp_context
from spe.analytics.td_scoring_paths import PATHS, bucket
from spe.models.player_td.scoring_paths import VERSION, project, validate
from spe.models.player_td.scoring_decision import choose
from spe.models.player_td.scouting_report import build_report
from spe.models.player_td.desk_combined import AllocationHistory
from spe.models.player_td.desk_opportunity import OpportunityHistory, prepare_history
from test_desk_opportunity import history_row
from test_desk_combined import combined_model, CUTOFF
from test_td_scouting import candidate


def priced(pid, p, price, low=None, team='BUF'):
    r=candidate(pid,p,team,book_price=price)
    r['source_timestamp']=r['price_source_at']=r['forecast']['data_cutoff']
    r['desk_analysis']['combination']['scoring_paths']={'status':'active',
        'baseline_probability':p,'probability_low':p if low is None else low,'probability_high':p,
        'rush_lambda':.3,'rec_lambda':.2,'paths':[{'path':'rec_redzone','label':'other red-zone targets',
            'player_sample':4,'recent_share':.3,'longer_share':.2,'share':.25,
            'projected_opportunities':1.,'opponent_position_sample':12,'opponent_rate_ratio':1.2,
            'inherited_share':0,'unscaled_expected_tds':.15,'conversion_rate':.15}]}
    r['desk_analysis']['features'].update(recent_measured_opportunity_games=2,recent_measured_touches=6,
        carries=4,targets=2,receptions=1)
    return r


def test_price_alternative_never_replaces_the_more_likely_main_scorer():
    leader=priced('expensive',.70,-300)
    receiver=priced('receiver',.48,180,.43)
    receiver['position']='WR'
    rows=[leader,receiver]
    before=deepcopy(rows)
    pick=choose(rows)
    assert pick['player_id']=='expensive'
    assert pick['decision']['most_likely']['name']=='expensive'
    assert not pick['decision']['selected']['price_supported']
    assert pick['decision']['value_candidate']['name']=='receiver'
    assert pick['decision']['value_candidate']['price_supported']
    assert rows==before


def test_choose_largest_chance_among_supported_prices_not_largest_payout():
    main=priced('main',.60,110,.52)
    longshot=priced('longshot',.25,1000,.20)
    assert choose([main,longshot])['player_id']=='main'


def test_disagreement_or_stale_price_cannot_be_called_supported_value():
    r=priced('cheap',.6,120,.4)
    assert not choose([r])['decision']['selected']['price_supported']
    r['desk_analysis']['combination']['scoring_paths']['probability_low']=.55
    r['source_timestamp']=r['price_source_at']='2024-10-02T08:00:00Z'
    assert choose([r])['decision']['status']=='no_fresh_price'


def test_bad_prices_fall_back_to_explicit_scorer_only_and_no_21_point_cutoff():
    r=priced('leader',.6,-250)
    r['team_implied_points']=18
    other=priced('other',.4,-200,team='NYJ')
    other['team_implied_points']=30
    pick=choose([r,other])
    assert pick['player_id']=='leader'
    assert pick['decision']['status']=='no_supported_value'
    assert 'Price is assessed separately' in pick['selection_reason']


def test_inactive_is_excluded_from_scorer_and_value_lanes():
    inactive=priced('inactive',.9,300)
    inactive['desk_analysis']['current_context']['availability']={'status':'Out'}
    pick=choose([inactive,priced('safe',.5,150,.45)])
    assert pick['player_id']=='safe'
    assert pick['decision']['most_likely']['name']=='safe'
    assert len(pick['decision']['excluded_candidates'])==1


def test_missing_college_charting_does_not_invent_a_stress_test():
    r=priced('college',.6,150)
    r['league_id']='fbs'
    r['desk_analysis']['combination'].pop('scoring_paths')
    pick=choose([r])
    assert not pick['decision']['selected']['price_supported']
    assert pick['decision']['value_candidate'] is None


def test_missing_research_uses_scoring_rank_without_inventing_an_advantage():
    leader=priced('leader',.65,-250)
    cheap=priced('cheap',.45,500,.4)
    cheap['desk_analysis']['features']['recent_measured_touches']=0
    fallback=choose([leader,cheap])['decision']['value_candidate']
    assert fallback['name']=='cheap' and not fallback['research_selected']
    assert fallback['alternative_basis']=='next_scoring_chance'
    cheap['desk_analysis']['features']['recent_measured_touches']=6
    cheap['desk_analysis']['combination']['scoring_paths']['paths']=[]
    pick=choose([leader,cheap])
    assert pick['decision']['value_candidate']['name']=='cheap'
    assert 'not established a special matchup' in pick['decision']['value_candidate']['customer']['caution']
    assert not next(c for c in pick['decision']['alternatives'] if c['name']=='cheap')['football_case']['supported']


def test_alternative_is_distinct_more_affordable_and_has_its_own_football_explanation():
    leader=priced('leader',.65,-200)
    expensive=priced('expensive',.6,-220,.55)
    value=priced('value',.4,250,.35)
    pick=choose([leader,expensive,value])
    alternative=pick['decision']['value_candidate']
    assert alternative['name']=='value' and alternative['football_case']['supported']
    assert alternative['customer']['label']=='Alternate pick'
    assert alternative['customer']['why'] and alternative['customer']['matchup']
    assert 'lower' not in alternative['customer']['label']
    assert choose([leader])['decision']['value_candidate'] is None


def test_alternate_remains_available_when_its_price_is_more_expensive():
    main=priced('main',.65,150)
    alternate=priced('alternate',.5,-200)
    pick=choose([main,alternate])
    assert pick['player_id']=='main'
    other=pick['decision']['value_candidate']
    assert other['name']=='alternate' and other['alternative_basis']=='football_research'
    assert not other['bet_qualified']
    assert other['customer']['label']=='Alternate pick'


def test_college_alternate_uses_model_rank_and_never_an_inactive_or_backup_qb():
    rows=[priced('main',.7,None),priced('second',.5,None),priced('out',.6,300),priced('backup',.9,400)]
    for row in rows:
        row['league_id']='fbs'
        row['desk_analysis']['combination'].pop('scoring_paths')
    rows[2]['desk_analysis']['current_context']['availability']={'status':'out'}
    rows[3]['position']='QB'
    rows[3]['desk_analysis']['current_context']['depth']={'rank':2}
    pick=choose(rows)
    assert pick['player_name']=='main'
    alt=pick['decision']['value_candidate']
    assert alt['name']=='second' and not alt['bet_qualified'] and not alt['research_selected']


def test_london_is_a_more_likely_scorer_while_dotson_can_be_a_separate_value_case():
    london=priced('Drake London',.3005,155,.2831)
    dotson=priced('Jahan Dotson',.2447,450,.2257)
    london['position']=dotson['position']='WR'
    pick=choose([dotson,london])
    assert pick['player_name']=='Drake London'
    assert pick['decision']['value_candidate']['name']=='Jahan Dotson'
    copy=build_report(pick,[dotson,london])['customer']
    assert copy['label']=='Most likely scorer'
    assert '+155' in copy['price'] and '39%' in copy['price']
    assert 'not established' in copy['price']
    assert not any(term in str(copy) for term in ('logit','lambda','calibration','artifact'))


def test_availability_suffix_normalization_is_exact_not_fuzzy():
    from spe.ingest.nfl_availability import name_key
    assert name_key('Kyle Pitts Sr.')==name_key('Kyle Pitts')
    assert name_key('Brian Robinson Jr.')==name_key('Brian Robinson')
    assert name_key('Brian Robinson')!=name_key('Bijan Robinson')
    assert name_key('D.J. Moore')==name_key('DJ Moore')


def test_availability_matches_reported_team_and_rejects_ambiguous_suffixes(db, monkeypatch):
    from types import SimpleNamespace
    from spe.db.models import Team, Player, InjuryEvent
    import spe.ingest.nfl_availability as feed
    from test_publication_pipeline import NOW, seed
    game=seed(db)
    db.add_all([Team(team_id='nfl:BUF',league_id='nfl',name='Bills',abbreviation='BUF'),
        Team(team_id='nfl:NYJ',league_id='nfl',name='Jets',abbreviation='NYJ')])
    for pid in ('buf','nyj','ambiguous1','ambiguous2'):
        db.add(Player(player_id=pid,name=pid,position='WR',position_group='WR'))
    db.flush()
    game.home_team_id='nfl:BUF';game.away_team_id='nfl:NYJ';game.status='scheduled'
    db.flush()
    catalog=[SimpleNamespace(player_id=pid,team_id=team,name=name) for pid,team,name in
        [('buf','nfl:BUF','Same Name'),('nyj','nfl:NYJ','Same Name'),
         ('ambiguous1','nfl:BUF','A Name Jr.'),('ambiguous2','nfl:BUF','A Name Sr.')]]
    monkeypatch.setattr(feed,'eligible_players_for_game',lambda *a,**kw:catalog)
    payload={'injuries':[{'injuries':[
        {'athlete':{'displayName':'Same Name Jr.','team':{'abbreviation':'NYJ'}},'status':'Out','date':NOW.isoformat()},
        {'athlete':{'displayName':'A Name','team':{'abbreviation':'BUF'}},'status':'Out','date':NOW.isoformat()}]}]}
    report=feed.refresh_nfl_availability(db,now=NOW,payload=payload)
    assert report['matched']==1 and len(report['unmatched'])==1
    assert {r.player_id for r in db.query(InjuryEvent)}=={'nyj'}


def test_pbp_bins_are_disjoint_and_passing_td_belongs_to_receiver_only():
    common={'game_id':'2026_01_A_B','posteam':'A','defteam':'B','no_play':0,'two_point_attempt':0,'extra_point_attempt':0,'qb_kneel':0,'qb_spike':0,'sack':0,'qtr':1,'score_differential':0,'drive':1}
    frame=pd.DataFrame([
        {**common,'play_id':1,'play_type':'pass','yardline_100':8,'air_yards':9,
            'receiver_player_id':'receiver','passer_player_id':'qb','rusher_player_id':None,'pass_touchdown':1,'rush_touchdown':0},
        {**common,'play_id':2,'play_type':'run','yardline_100':4,'air_yards':None,
            'receiver_player_id':None,'rusher_player_id':'qb','pass_touchdown':0,'rush_touchdown':1},
        {**common,'play_id':3,'play_type':'run','yardline_100':1,'rusher_player_id':'qb','two_point_attempt':1},
        {**common,'play_id':4,'play_type':'pass','yardline_100':40,'air_yards':float('nan'),
            'receiver_player_id':'receiver','rusher_player_id':None,'pass_touchdown':0,'rush_touchdown':0}])
    rows=pbp_context(frame,{'receiver':'TE','qb':'QB'})
    player={r['entity_id']:r['metrics']['scoring_paths'] for r in rows if r['dataset']=='pbp_player'}
    assert player['nfl:receiver']['buckets']['rec_endzone']['tds']==1
    assert player['nfl:receiver']['buckets']['rec_unknown']['opportunities']==1
    assert player['nfl:qb']['buckets']['rush_5']['tds']==1
    assert sum(v['tds'] for v in player['nfl:qb']['buckets'].values())==1
    assert bucket('rush',None)=='rush_unknown'


def path_model():
    m=combined_model()
    m['scoring_paths']={'version':VERSION,'calibration':[0,.5,.5],
        'rates':{k:{'rate':.15 if k.endswith('5') else .04} for k in PATHS}}
    return m


def path_history():
    rows=[]
    for week in (1,2,3):
        for pid,pos,n in [('starter','RB',10),('backup','RB',4),('qb','QB',2)]:
            r=history_row(pid,week,position=pos,carries=n,targets=0,receptions=0,gl_carries=1,rz_targets=0)
            available=(CUTOFF-timedelta(days=(4-week)*7)).isoformat()
            p={'buckets':{'rush_5':{'opportunities':n,'tds':1}},'by_position':{pos:{'rush_5':{'opportunities':n,'tds':1}}}}
            t={'buckets':{'rush_5':{'opportunities':16,'tds':3}},'by_position':{'RB':{'rush_5':{'opportunities':14,'tds':2}},'QB':{'rush_5':{'opportunities':2,'tds':1}}}}
            r['nflverse_context']={'player':{'metrics':{'scoring_paths':p},'effective_at':available,'observed_at':available},
                'team':{'metrics':{'field_position_coverage':1.,'scoring_paths':t},'effective_at':available,'observed_at':available}}
            rows.append(r)
    return OpportunityHistory(prepare_history(rows))


def test_mass_is_reserved_and_rb_absence_does_not_assign_rb_work_to_qb():
    history=path_history(); m=path_model(); allocator=AllocationHistory(history)
    normal=allocator.allocate(m,'home','away',CUTOFF,True)['scoring_paths']
    changed=allocator.allocate(m,'home','away',CUTOFF,True,{'starter'})['scoring_paths']
    assert normal and changed and 'starter' not in changed['players']
    assert changed['players']['backup']['paths'][0]['inherited_share']>0
    assert changed['players']['qb']['paths'][0]['inherited_share']==0
    assert sum(p['rush_lambda'] for p in changed['players'].values())<2
    assert set(changed['players']['backup']['scenario_probabilities'])=={'central','longer_role','recent_role','no_inheritance','full_inheritance'}


def test_future_observation_cannot_enter_path_projection():
    history=path_history(); m=path_model()
    for rows in history.teams.values():
        for r in rows:
            r['nflverse_context']['team']['observed_at']=(CUTOFF+timedelta(days=1)).isoformat()
    assert AllocationHistory(history).allocate(m,'home','away',CUTOFF,True)['scoring_paths'] is None


def test_case_is_outcome_blind_and_new_private_fields_are_redacted():
    from app.gate import redact
    r=choose([priced('receiver',.5,160,.45)])
    report=build_report(r,[r])
    r.update(result='blank',actual=0,final_score='0-50')
    assert build_report(r,[r])==report
    r.update(kickoff_utc='2099-01-01T20:00:00Z',market_id='anytime_td')
    locked=redact('/v1/anytime-td/replay',{'rows':[r]},subscribed=False)['rows'][0]
    assert locked['locked'] and 'decision' not in locked and 'desk_analysis' not in locked


def test_artifact_guard_rejects_negative_probability_weights_and_missing_paths():
    m=path_model()['scoring_paths']
    assert validate(m)
    m['calibration'][1]=-1
    assert not validate(m)
    m['calibration'][1]=1
    m['rates'].pop('rec_endzone')
    assert not validate(m)


@pytest.mark.parametrize('fresh', [True, False])
def test_saved_strategy_lanes_grade_separately_without_reselecting_final(db, monkeypatch, fresh):
    from spe.db.models import BoardSnapshot, Player, PublishedStat
    import spe.models.player_td.desk_replay as replay
    from test_publication_pipeline import NOW, seed
    game=seed(db)
    game.kickoff_utc=NOW-timedelta(hours=5)
    game.status='final'
    for pid, actual in [('leader',0),('value',1)]:
        db.add(Player(player_id=pid,name=pid,position_group='WR',position='WR'))
        db.flush()
        db.add(PublishedStat(key='g|'+pid+'|anytime_td',game_id='g',player_id=pid,
            market_id='anytime_td',actual=actual,source='espn.summary:test',observed_at=NOW))
    candidates=[priced('leader',.7,-300),priced('value',.5,160,.45)]
    if not fresh:
        from spe.db.base import as_utc
        for r in candidates:
            r['source_timestamp']=r['price_source_at']=(as_utc(r['forecast']['data_cutoff'])-timedelta(hours=12)).isoformat()
    pick=choose(candidates)
    pick.update(game_id='g',kickoff_utc=game.kickoff_utc.isoformat(),retrospective=False)
    snapshot=deepcopy(pick)
    db.add(BoardSnapshot(key='scorer-desk-v1:g',updated_at=NOW-timedelta(hours=6),payload={'row':pick,'evaluated':True}))
    db.flush()
    monkeypatch.setattr(replay,'load_combined_model',lambda *a,**kw:combined_model())
    monkeypatch.setattr(replay,'load_history',lambda *a,**kw:(_ for _ in ()).throw(AssertionError('Never refit a finished pick')))
    board=replay.replay_day(db,'2026-11-15',now=NOW,cached_only=True)
    assert board['decision_record']['most_likely']['losses']==1
    assert board['decision_record']['value_candidate']['wins']==1
    assert board['decision_record']['value_candidate']['hypothetical_unit_profit']==(pytest.approx(1.6) if fresh else None)
    assert board['decision_record']['value_candidate']['priced_bets']==int(fresh)
    assert board['rows'][0]['player_id']=='leader'
    assert db.get(BoardSnapshot,'scorer-desk-v1:g').payload['row']==snapshot


def test_new_inactive_report_withholds_alternative_without_removing_main_pick(db, monkeypatch):
    from spe.db.models import BoardSnapshot
    import spe.models.player_td.desk_replay as replay
    from test_publication_pipeline import NOW, seed
    game=seed(db)
    pick=choose([priced('leader',.7,-300),priced('value',.5,160,.45)])
    pick.update(game_id='g',kickoff_utc=game.kickoff_utc.isoformat(),retrospective=False)
    original=deepcopy(pick)
    db.add(BoardSnapshot(key='scorer-desk-v1:g',updated_at=NOW,payload={'row':pick,'evaluated':True}))
    db.flush()
    monkeypatch.setattr(replay,'load_combined_model',lambda *a,**kw:combined_model())
    monkeypatch.setattr('spe.models.player_td.desk_evidence.current_context',
        lambda db,gid,pid,*a,**kw:{'availability':{'status':'out' if pid=='value' else 'active'}})
    board=replay.replay_day(db,'2026-11-15',now=NOW,cached_only=True)
    assert board['rows'][0]['player_id']=='leader'
    assert board['rows'][0]['decision']['value_candidate'] is None
    assert board['rows'][0]['decision']['alternative_status']=='withheld_availability'
    assert db.get(BoardSnapshot,'scorer-desk-v1:g').payload['row']==original
