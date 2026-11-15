"""Grade only genuinely pregame challenger captures, separate from Desk history."""
from collections import Counter

from sqlalchemy import select

from spe.db.base import as_utc
from spe.db.models import BoardSnapshot, Game
from spe.models.player_td.desk_replay import grade


def record(db):
    rows=[];excluded=Counter()
    snapshots=db.scalars(select(BoardSnapshot).where(BoardSnapshot.key.like('td-pregame-candidates-v1:%')))
    for snapshot in snapshots:
        payload=snapshot.payload or {};challenger=payload.get('challenger') or {}
        if not challenger:
            continue
        game=db.get(Game,payload['game_id'])
        if game is None:
            excluded['missing_game']+=1;continue
        captured=as_utc(payload.get('captured_at'));kick=as_utc(game.kickoff_utc)
        cutoff=as_utc(challenger.get('data_cutoff'))
        if not captured or not kick or not cutoff or not cutoff<=captured<kick:
            excluded['not_verified_pregame']+=1;continue
        candidates={r['player_id']:r for r in payload.get('candidates',[])}
        old=payload.get('selected_player_id');new=challenger.get('selected_player_id')
        if old not in candidates or new not in candidates:
            excluded['selection_absent_from_saved_universe']+=1;continue
        saved=candidates[new].get('challenger_forecast') or {}
        forecast_cutoff=as_utc(saved.get('data_cutoff'))
        if (saved.get('model_artifact_sha256')!=challenger.get('model_artifact_sha256')
                or not forecast_cutoff or forecast_cutoff>captured):
            excluded['unverifiable_challenger_forecast']+=1;continue
        baseline_result=grade(db,game,old);challenger_result=grade(db,game,new)
        comparable=baseline_result in ('scored','blank') and challenger_result in ('scored','blank')
        rows.append({'game_id':game.game_id,'kickoff_utc':kick.isoformat(),'captured_at':captured.isoformat(),
            'baseline_player_id':old,'challenger_player_id':new,
            'baseline_player_name':candidates[old].get('player_name'),
            'challenger_player_name':candidates[new].get('player_name'),
            'baseline_result':baseline_result,'challenger_result':challenger_result,
            'comparable':comparable,'challenger_model_sha256':challenger['model_artifact_sha256']})
    compared=[r for r in rows if r['comparable']]
    return {'captured_games':len(rows),'graded_matching_games':len(compared),
        'baseline_wins':sum(r['baseline_result']=='scored' for r in compared),
        'challenger_wins':sum(r['challenger_result']=='scored' for r in compared),
        'pending_or_unverified_games':len(rows)-len(compared),'excluded':dict(excluded),
        'rows':sorted(rows,key=lambda r:(r['kickoff_utc'],r['game_id'])),
        'note':'Prospective shadow comparison only. Missing final player evidence remains ungraded; prices, payouts and the official Desk record are unchanged.'}
