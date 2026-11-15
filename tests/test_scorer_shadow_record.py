from datetime import timedelta

from spe.db.models import BoardSnapshot
from spe.models.player_td.scorer_shadow_record import record
from test_publication_pipeline import seed, NOW


def capture(db, *, late=False):
    game=seed(db);game.kickoff_utc=NOW+timedelta(hours=1)
    captured=NOW+timedelta(hours=2) if late else NOW
    payload={'game_id':game.game_id,'captured_at':captured.isoformat(),'selected_player_id':'a',
        'challenger':{'selected_player_id':'b','data_cutoff':NOW.isoformat(),'model_artifact_sha256':'sha'},
        'candidates':[{'player_id':'a'}, {'player_id':'b','challenger_forecast':{
            'data_cutoff':NOW.isoformat(),'model_artifact_sha256':'sha'}}]}
    db.add(BoardSnapshot(key='td-pregame-candidates-v1:'+game.game_id,payload=payload,updated_at=captured));db.flush()
    return game


def test_shadow_record_rejects_late_capture_even_if_final_score_is_known(db):
    capture(db,late=True)
    result=record(db)
    assert result['captured_games']==0
    assert result['excluded']=={'not_verified_pregame':1}


def test_missing_final_player_evidence_is_not_fabricated_as_a_loss(db):
    game=capture(db);game.status='final';db.flush()
    result=record(db)
    assert result['captured_games']==1 and result['graded_matching_games']==0
    assert result['baseline_wins']==result['challenger_wins']==0
    assert result['rows'][0]['baseline_result']=='awaiting_box'
