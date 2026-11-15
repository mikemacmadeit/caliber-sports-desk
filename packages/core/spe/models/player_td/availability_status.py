"""Resolve game designations without letting a practice note clear an absence."""
from spe.db.base import as_utc

INACTIVE = {'out', 'confirmed_inactive', 'inactive', 'suspended', 'injured reserve',
            'reserve/injured', 'ir', 'pup', 'reserve/pup', 'nfi', 'reserve/nfi'}
GAME_STATUS = INACTIVE | {'doubtful', 'questionable', 'probable', 'active', 'available', 'healthy'}


def normalize(status):
    value = str(status or 'unknown').strip().lower().replace('_', ' ')
    aliases = {'injured-reserve':'injured reserve', 'reserve - injured':'injured reserve',
               'physically unable to perform':'pup', 'non-football injury':'nfi',
               'confirmed inactive':'confirmed_inactive', 'deactivated':'inactive'}
    return aliases.get(value, value)


def resolved_reports(reports):
    """Callers filter observation/publication times before resolution.

    A later explicit return can clear an old IR report. A practice-only note
    cannot; practice participation is not a game designation. Ties favor an
    explicit absence. Neither missing reports nor roster membership mean active.
    """
    chosen = {}
    absent = {}
    practice = {}
    for r in reports:
        if getattr(r, 'practice_status', None) and (r.player_id not in practice or
                as_utc(r.reported_at) > as_utc(practice[r.player_id].reported_at)):
            practice[r.player_id] = r
        status = normalize(r.designation)
        key = (status in GAME_STATUS, as_utc(r.reported_at), status in INACTIVE)
        if status in INACTIVE and (r.player_id not in absent or as_utc(r.reported_at)>as_utc(absent[r.player_id].reported_at)):
            absent[r.player_id]=r
        if r.player_id in chosen and key <= chosen[r.player_id][0]:
            continue
        chosen[r.player_id] = (key, r, status)
    for pid,r in absent.items():
        _,latest,status=chosen[pid]
        # Some archive feeds stamp retrieval time as report time. A subsequent
        # Questionable/practice entry cannot clear an explicit game absence.
        # Only a newer explicit return/active designation can do that.
        if status not in INACTIVE and not (status in {'active','available','healthy'} and
                as_utc(latest.reported_at)>as_utc(r.reported_at)):
            chosen[pid]=(None,r,normalize(r.designation))
    return {pid: {'status':status, 'raw_status':r.designation, 'source':r.source,
            'observed_at':as_utc(r.observed_at).isoformat() if r.observed_at else None,
            'reported_at':as_utc(r.reported_at).isoformat(),
            'practice_status':practice[pid].practice_status if pid in practice else None,
            'practice_reported_at':as_utc(practice[pid].reported_at).isoformat() if pid in practice else None,
            'practice_source':practice[pid].source if pid in practice else None}
            for pid, (_,r,status) in chosen.items()}
