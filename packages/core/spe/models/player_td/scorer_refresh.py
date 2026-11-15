"""Serve stored scorer cards promptly; coalesce expensive refreshes per day."""
import logging
import threading
import time

from spe.config import settings

_lock = threading.Lock()
_source_lock = threading.Lock()
_states = {}


def ensure_refresh(date):
    key = (settings.database_url, date)
    with _lock:
        state = _states.get(key, {})
        if state.get('running'):
            return 'running'
        if time.monotonic()-state.get('completed', -1000) < (15 if state.get('failed') else 60):
            return 'failed' if state.get('failed') else 'ready'
        _states[key] = {'running':True}

    def refresh():
        failed = False
        try:
            from spe.db.session import session_factory
            from spe.models.player_td.desk_replay import replay_day
            from sqlalchemy.exc import IntegrityError
            with _source_lock, session_factory() as db:
                from spe.ingest.nfl_availability import refresh_nfl_availability
                status = refresh_nfl_availability(db)
                if status.get('status') == 'unavailable':
                    logging.getLogger(__name__).warning('NFL availability refresh unavailable: %s', status)
                from spe.models.player_td.desk_replay import requested_retrain
                simulation = (requested_retrain(date, 'nfl') or {}).get('simulation')
                if settings.app_env=='dev' or simulation:
                    from spe.ingest.nflverse_desk import refresh_nflverse_desk
                    # This free-source job has its own six-hour due check.
                    # It retains prior good data on partial source failure.
                    refresh_nflverse_desk(db)
                db.commit()
            with session_factory() as db:
                for attempt in range(2):
                    try:
                        replay_day(db, date, 'all')
                        db.commit()
                        break
                    except IntegrityError as exc:
                        db.rollback()
                        if attempt or 'board_snapshots.key' not in str(exc.orig):
                            raise
        except Exception:
            failed = True
            logging.getLogger(__name__).exception('Scorer refresh failed for %s; saved picks retained', date)
        finally:
            with _lock:
                _states[key] = {'running':False, 'failed':failed, 'completed':time.monotonic()}

    threading.Thread(target=refresh, name='td-scorers-'+date, daemon=True).start()
    return 'running'
