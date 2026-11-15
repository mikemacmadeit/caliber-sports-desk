"""Isolated, possession-level TD research using frozen Desk inputs.

No database, ingestion, fitting or publication occurs here. All touchdown
budgets and scorer weights come from the supplied pregame model. The dynamic
variant changes rush/pass mix and player shares with the simulated score.
These are declared scenario assumptions, not a validated new probability model.
"""
from __future__ import annotations

import numpy as np

STATES = ('close', 'leading', 'trailing')


def _shares(team, channel, state, role_strength=1., unavailable=(), recent_role=False):
    """Preserve OTHER and reserve half of an absent player's work."""
    base = np.array(team['scorer_shares'][channel], dtype=float)
    weights = base.copy()
    for i, player in enumerate(team['players']):
        record = team['usage'][player['id']][channel]
        overall = record['total'] / max(1., team['usage_totals'][channel]['total'])
        measured = record['states'][state]
        total = team['usage_totals'][channel]['states'][state]
        posterior = (measured + 25*overall)/(total+25)
        ratio = posterior/overall if overall else 1.
        # Declared shrinkage/bounds protect sparse game-state splits.
        weights[i] *= np.clip(ratio, .5, 2.)**role_strength
        if recent_role and player['position'] in ('RB','FB'):
            recent_total=team['usage_totals'][channel]['latest']
            recent=(record['latest']+8*overall)/(recent_total+8)
            weights[i] *= np.clip(recent/overall if overall else 1., .25, 4.)
        if channel=='rec' and player['position']=='QB':
            # This experiment excludes rare QB receptions as well as pass TDs.
            weights[-1] += weights[i]
            weights[i] = 0.
    for i, player in enumerate(team['players']):
        if player['id'] in unavailable:
            vacated=weights[i];weights[i]=0.
            peers=[j for j,p in enumerate(team['players']) if p['position']==player['position'] and p['id'] not in unavailable]
            denom=sum(weights[j] for j in peers)
            weights[-1] += vacated*(.5 if denom else 1.)
            if denom:
                for j in peers:weights[j] += .5*vacated*weights[j]/denom
    assert np.all(weights>=0) and weights.sum()>0
    return weights/weights.sum()


def _rush_fraction(team, state, strength):
    base=team['expected_tds']['rush']/sum(team['expected_tds'].values())
    rush=team['usage_totals']['rush'];rec=team['usage_totals']['rec']
    overall=rush['total']/max(1.,rush['total']+rec['total'])
    adjusted=(rush['states'][state]+25*overall)/(rush['states'][state]+rec['states'][state]+25)
    def logit(p):return np.log(np.clip(p,.01,.99)/(1-np.clip(p,.01,.99)))
    offset=np.clip(logit(adjusted)-logit(overall),-.75,.75)*strength
    return float(1/(1+np.exp(-logit(base)-offset)))


def simulate(inputs, *, draws=50000, seed=20261115, mode='gameflow', pace=1.,
             script_strength=1., role_strength=1., unavailable=(), recent_role=False):
    """Regulation possessions, seven-point TDs and three-point field goals.

    poisson_control exactly reproduces analytical path probabilities, apart
    from MC error. static_drives isolates possessions and efficiency variance.
    gameflow additionally uses only the score already simulated at that drive.
    No future/final score is used to allocate earlier touchdowns.
    """
    if draws<100 or pace<=0 or mode not in ('poisson_control','static_drives','gameflow'):
        raise ValueError('Invalid simulation configuration')
    rng=np.random.default_rng(seed)
    teams=inputs['teams']; names=list(teams)
    if len(names)!=2:raise ValueError('Exactly two teams required')
    n=int(draws)
    counts={tid:np.zeros((len(t['players'])+1,n),dtype=np.int16) for tid,t in teams.items()}
    rushing={tid:np.zeros_like(v) for tid,v in counts.items()}
    receiving={tid:np.zeros_like(v) for tid,v in counts.items()}
    team_tds={tid:np.zeros(n,dtype=np.int16) for tid in names}
    scores={tid:np.zeros(n,dtype=np.int16) for tid in names}
    state_counts={tid:np.zeros((3,n),dtype=np.int16) for tid in names}
    first=rng.integers(0,2,size=n)
    pace_mean=np.mean([t['expected_possessions'] for t in teams.values()])
    samples=np.asarray(inputs['possession_samples'],dtype=float)
    relative=samples/max(.01,samples.mean())
    pace_factor=rng.choice(relative,size=n)
    possessions={}
    efficiency={};field_goal={}
    for tid,t in teams.items():
        # Shared game pace; stochastic rounding preserves the requested mean.
        expected=t['expected_possessions']*pace*pace_factor
        possessions[tid]=np.clip(np.floor(expected)+(rng.random(n)<expected%1),5,20).astype(int)
        td_rate=sum(t['expected_tds'].values())/t['expected_possessions']
        fg_rate=max(0.,(t['implied_points']-7*sum(t['expected_tds'].values()))/3)/t['expected_possessions']
        if not 0<td_rate<.8 or td_rate+fg_rate>=1:
            raise ValueError('Incompatible team scoring/possession inputs')
        concentration=t['measured_drive_count']+25
        efficiency[tid]=rng.beta(td_rate*concentration,(1-td_rate)*concentration,size=n)
        field_goal[tid]=fg_rate
    for tid,t in teams.items():
        if mode!='poisson_control':continue
        for channel,out in (('rush',rushing),('rec',receiving)):
            weights=_shares(t,channel,'close',0,unavailable,recent_role)
            # Independent Poisson thinning is the analytical allocation control.
            out[tid][:]=rng.poisson(t['expected_tds'][channel]*weights[:,None],size=out[tid].shape)
        counts[tid]=rushing[tid]+receiving[tid]
        team_tds[tid]=counts[tid].sum(axis=0)
        scores[tid]=team_tds[tid]*7
    tables={tid:{state:{channel:_shares(t,channel,state,role_strength if mode=='gameflow' else 0,
        unavailable,recent_role) for channel in ('rush','rec')} for state in STATES} for tid,t in teams.items()}
    rush_mix={tid:{state:_rush_fraction(t,state,script_strength if mode=='gameflow' else 0) for state in STATES} for tid,t in teams.items()}
    if mode!='poisson_control':
        for possession in range(max(int(v.max()) for v in possessions.values())):
            for slot in (0,1):
                for ti,tid in enumerate(names):
                    opp=names[1-ti];t=teams[tid]
                    active=(possessions[tid]>possession)&((first+slot)%2==ti)
                    ids=np.flatnonzero(active)
                    if not len(ids):continue
                    margin=scores[tid][ids]-scores[opp][ids]
                    state=np.where(margin>=8,1,np.where(margin<=-8,2,0)) if mode=='gameflow' else np.zeros(len(ids),int)
                    state_counts[tid][state,ids]+=1
                    outcome=rng.random(len(ids))
                    td=outcome<efficiency[tid][ids]
                    fg=(~td)&(outcome<np.minimum(.99,efficiency[tid][ids]+field_goal[tid]))
                    scores[tid][ids[fg]]+=3
                    scoring=ids[td];scoring_state=state[td]
                    if not len(scoring):continue
                    team_tds[tid][scoring]+=1;scores[tid][scoring]+=7
                    for si,st in enumerate(STATES):
                        events=scoring[scoring_state==si]
                        is_rush=rng.random(len(events))<rush_mix[tid][st]
                        for channel,mask,dest in (('rush',is_rush,rushing),('rec',~is_rush,receiving)):
                            ds=events[mask]
                            picked=rng.choice(len(t['players'])+1,size=len(ds),p=tables[tid][st][channel])
                            dest[tid][picked,ds]+=1
        for tid in names:counts[tid]=rushing[tid]+receiving[tid]
    rows=[]
    for tid,t in teams.items():
        for i,p in enumerate(t['players']):
            hits=counts[tid][i]>0;prob=float(hits.mean())
            rows.append({'id':p['id'],'name':p['name'],'team':tid,'position':p['position'],
                'raw_probability':prob,'mc_se':float(np.sqrt(prob*(1-prob)/n)),
                'rush_probability':float((rushing[tid][i]>0).mean()),
                'rec_probability':float((receiving[tid][i]>0).mean())})
    a,b=names
    margin=scores[a]-scores[b]
    conditions={'first_team_wins':margin>0,'second_team_wins':margin<0,'close_finish':abs(margin)<=7,
        'first_team_wins_by_14':margin>=14,'second_team_wins_by_14':margin<=-14}
    conditional={}
    for label,mask in conditions.items():
        conditional[label]={'games':int(mask.sum()),'probability':float(mask.mean()),
            'player_probability':{p['id']:float((counts[tid][i,mask]>0).mean()) if mask.any() else None
                for tid,t in teams.items() for i,p in enumerate(t['players'])}}
    mechanics={'team_td_mass_conserved':all(np.array_equal(counts[tid].sum(axis=0),team_tds[tid]) for tid in names),
        'passing_tds_never_awarded_to_qb':all(not receiving[tid][i].any() for tid,t in teams.items() for i,p in enumerate(t['players']) if p['position']=='QB'),
        'unavailable_players_never_score':all(not counts[tid][i].any() for tid,t in teams.items() for i,p in enumerate(t['players']) if p['id'] in unavailable)}
    if not all(mechanics.values()):raise AssertionError(mechanics)
    return {'draws':n,'seed':seed,'mode':mode,'players':rows,'mechanics':mechanics,'conditions':conditional,
        'teams':{tid:{'mean_score':float(scores[tid].mean()),'mean_tds':float(team_tds[tid].mean()),
            'mean_rush_tds':float(rushing[tid].sum(axis=0).mean()),'mean_rec_tds':float(receiving[tid].sum(axis=0).mean()),
            'mean_possessions':float(possessions[tid].mean()),'score_quantiles':np.quantile(scores[tid],[.1,.5,.9]).tolist(),
            'drive_state_fractions':dict(zip(STATES,(state_counts[tid].sum(axis=1)/max(1,state_counts[tid].sum())).tolist())),
            'rush_fraction_by_state':rush_mix[tid] } for tid in names}}
