from copy import deepcopy
import math

from spe.simulate.desk_gameflow import simulate


def fixture():
    teams={}
    for tid,tds in [('A',{'rush':2.,'rec':1.}),('B',{'rush':.6,'rec':.9})]:
        ps=[{'id':tid+'rb','name':'Runner','position':'RB'},{'id':tid+'wr','name':'Receiver','position':'WR'},
            {'id':tid+'qb','name':'Quarterback','position':'QB'}]
        totals={'rush':{'total':100,'latest':20,'states':{'close':40,'leading':50,'trailing':10}},
            'rec':{'total':100,'latest':20,'states':{'close':40,'leading':10,'trailing':50}}}
        fractions={'rush':[.7,0,.3],'rec':[.1,.9,0]}
        usage={p['id']:{ch:{'total':totals[ch]['total']*fractions[ch][i],
            'latest':20*fractions[ch][i],'states':{s:n*fractions[ch][i] for s,n in totals[ch]['states'].items()}}
            for ch in totals} for i,p in enumerate(ps)}
        teams[tid]={'players':ps,'expected_tds':tds,'implied_points':7*sum(tds.values())+3,
            'expected_possessions':10.,'measured_drive_count':60,'usage':usage,'usage_totals':totals,
            'scorer_shares':{'rush':[.63,0,.27,.1],'rec':[.09,.81,0,.1]}}
    return {'teams':teams,'possession_samples':[9,10,11]}


def test_control_matches_exact_probabilities_and_conserves_each_touchdown():
    inputs=fixture();result=simulate(inputs,draws=100000,mode='poisson_control')
    assert all(result['mechanics'].values())
    for r in result['players']:
        t=inputs['teams'][r['team']];i=next(i for i,p in enumerate(t['players']) if p['id']==r['id'])
        lam=sum(t['expected_tds'][k]*t['scorer_shares'][k][i] for k in ('rush','rec'))
        assert abs(r['raw_probability']-(1-math.exp(-lam))) < 5*r['mc_se']+.001


def test_dynamic_script_changes_run_pass_outcomes_and_is_reproducible():
    inputs=fixture()
    dynamic=simulate(inputs,draws=20000,seed=3)
    assert dynamic==simulate(inputs,draws=20000,seed=3)
    static=simulate(inputs,draws=20000,seed=3,mode='static_drives')
    assert dynamic['teams']['A']['mean_rush_tds']>static['teams']['A']['mean_rush_tds']+.05
    assert dynamic['teams']['B']['mean_rush_tds']<static['teams']['B']['mean_rush_tds']-.03
    assert all(dynamic['mechanics'].values())


def test_pace_really_changes_possessions_and_scoring():
    slow=simulate(fixture(),draws=20000,seed=4,pace=.85)
    fast=simulate(fixture(),draws=20000,seed=4,pace=1.15)
    assert fast['teams']['A']['mean_possessions']>slow['teams']['A']['mean_possessions']+2
    assert fast['teams']['A']['mean_tds']>slow['teams']['A']['mean_tds']+.5


def test_absent_player_cannot_score_and_inputs_never_mutate():
    inputs=fixture();before=deepcopy(inputs)
    result=simulate(inputs,draws=4000,unavailable=('Arb',))
    assert next(r for r in result['players'] if r['id']=='Arb')['raw_probability']==0
    assert all(result['mechanics'].values())
    assert inputs==before
