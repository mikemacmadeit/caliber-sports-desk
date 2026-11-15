"""Capture a challenger before kickoff without substituting it for the Desk pick."""
from spe.models.player_td.desk_combined import load_combined_model, logit
from spe.models.player_td.scorer_candidate import predict


def compare(rows, selected, active_model, cutoff):
    if not rows or active_model.get('league') != 'fbs':
        return None
    candidate = load_combined_model('fbs', research_candidate=True, version='desk_combined_v8')
    if not candidate or candidate.get('baseline_artifact_sha256') != active_model.get('artifact_sha256'):
        return None  # A changed baseline requires a newly validated comparison.
    scored=[]
    for row in rows:
        analysis=row.get('desk_analysis') or {};f=analysis.get('features')
        original=(row.get('forecast') or {}).get('p_estimate')
        if f is None or original is None:
            continue
        raw=(float(logit(original))-active_model['calibration_intercept'])/active_model['calibration_slope']
        p=predict(candidate['scorer_model'], f, raw)
        forecast={**row['forecast'],'p_estimate':p,
            'model_version':'desk_combined_v8-fbs-'+candidate['artifact_sha256'][:10],
            'model_artifact_sha256':candidate['artifact_sha256'],
            'data_cutoff':cutoff.isoformat(),'research_only':True,'play_enabled':False,
            'promotion_gate_passed':bool(candidate.get('live_enabled'))}
        row['challenger_forecast']=forecast
        scored.append({**row,'forecast':forecast})
    # Identical availability/role rules; price is not used to choose the scorer.
    from spe.models.player_td.desk_replay import select_game_scorer
    proposed=select_game_scorer(scored)
    if not proposed:
        return None
    return {'status':'shadow_comparison','official_pick_unchanged':True,
        'model_version':'desk_combined_v8','model_artifact_sha256':candidate['artifact_sha256'],
        'promotion_gate_passed':bool(candidate.get('live_enabled')),
        'selected_player_id':proposed['player_id'],'selected_player_name':proposed.get('player_name'),
        'probability':proposed['forecast']['p_estimate'],
        'baseline_selected_player_id':selected.get('player_id') if selected else None,
        'candidate_count':len(scored),'data_cutoff':cutoff.isoformat(),
        'note':'Saved before kickoff for future comparison; not an original Desk pick or proven accuracy gain.'}
