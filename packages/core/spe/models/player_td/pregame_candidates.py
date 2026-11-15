"""Freeze the candidate universe before kickoff for later honest evaluation."""
from copy import deepcopy
from spe.db.base import as_utc
from spe.db.models import BoardSnapshot


def save_candidates(db, game, rows, selected, now, model_sha, challenger=None, *, simulation=None):
    kick=as_utc(game.kickoff_utc)
    if now>=kick:
        return False
    key='td-pregame-candidates-v1:'+game.game_id
    old=db.get(BoardSnapshot,key)
    # Protect a newer concurrent capture from an older request.
    old_time=as_utc(old.payload.get('captured_at')) if old else None
    if old_time and old_time>=now:
        return False
    candidates=[]
    for row in rows:
        forecast=row.get('forecast') or {}
        cutoff=as_utc(forecast.get('data_cutoff'))
        if cutoff and cutoff>now:
            raise ValueError('Candidate information cutoff is after its capture time')
        analysis=row.get('desk_analysis') or {}
        context=analysis.get('current_context') or {}
        candidates.append(deepcopy({**{k:row.get(k) for k in ('player_id','player_name','team','position',
            'forecast','book_price','bookmaker','source_timestamp','price_source_at','selection_safety','challenger_forecast')},
            'desk_analysis':{'features':analysis.get('features'), 'combination':analysis.get('combination'),
                'scouting_inputs':analysis.get('scouting_inputs'), 'vacated_workload':analysis.get('vacated_workload'),
                'fitted_feature_names':analysis.get('fitted_feature_names'), 'simulation':analysis.get('simulation'),
                'current_context':{k:context.get(k) for k in ('availability','depth','weather','nflverse_context')}}}))
    payload={'game_id':game.game_id,'kickoff_utc':kick.isoformat(),'captured_at':now.isoformat(),
             'model_artifact_sha256':model_sha,'selected_player_id':selected.get('player_id') if selected else None,
             'market_context':deepcopy(rows[0].get('market_context')) if rows else None,
             'candidates':candidates,
             'selection_policy':deepcopy(selected.get('value_selection')) if selected else None,
             'decision':deepcopy(selected.get('decision')) if selected else None,
             'selected_scouting_report':deepcopy((selected.get('desk_analysis') or {}).get('scouting_report')) if selected else None,
             'challenger':deepcopy(challenger), 'simulation':deepcopy(simulation),
             'note':'Last saved candidate list before kickoff. Outcomes are excluded. Missing future participation is not automatically a loss.'}
    if old:
        old.payload=payload;old.updated_at=now
    else:
        db.add(BoardSnapshot(key=key,payload=payload,updated_at=now))
    return True
