"""Disjoint scoring opportunities from public PBP, not route/coverage charting.

Passing touchdowns are credited only to the targeted receiver. Unknown field
position/air yards have their own bins rather than fabricated red-zone labels.
"""
from collections import defaultdict
import math

PATHS = ('rush_5', 'rush_10', 'rush_20', 'rush_open', 'rush_unknown',
         'rec_endzone', 'rec_redzone', 'rec_deep', 'rec_other', 'rec_unknown')
LABELS = dict(zip(PATHS, ('carries inside five', 'carries from six to ten',
    'carries from eleven to twenty', 'open-field carries', 'unlocated carries',
    'estimated end-zone targets', 'other red-zone targets',
    'deep targets outside the red zone', 'other targets', 'unclassified targets')))


def finite(value):
    try:
        return float(value) if math.isfinite(float(value)) else None
    except (ValueError, TypeError):
        return None


def bucket(kind, yardline, air=None):
    yl, air = finite(yardline), finite(air)
    if yl is None or not 0 <= yl <= 100:
        return kind + '_unknown'
    if kind == 'rush':
        return 'rush_' + ('5' if yl <= 5 else '10' if yl <= 10 else '20' if yl <= 20 else 'open')
    if air is not None and air >= yl:
        return 'rec_endzone'
    if yl <= 20:
        return 'rec_redzone'
    return 'rec_unknown' if air is None else 'rec_deep' if air >= 15 else 'rec_other'


def _new():
    return {'version': 2, 'buckets': {}, 'by_position': {}, 'states': {}, 'passers': {}}


def aggregate(frame, positions):
    """Input is deduplicated, valid run/pass plays from pbp_context."""
    result = defaultdict(_new)
    def add_bins(mapping, key, td, yards):
        entry = mapping.setdefault(key, {'opportunities': 0, 'tds': 0, 'yards': 0.})
        entry['opportunities'] += 1
        entry['tds'] += td
        entry['yards'] += yards
    for r in frame.to_dict('records'):
        kind = 'rush' if r['carry'] else 'rec' if r['target'] else None
        if kind is None:
            continue
        pid = r['rusher_player_id'] if kind == 'rush' else r['receiver_player_id']
        key = bucket(kind, r.get('yl'), r.get('air'))
        td = int(finite(r.get('rush_touchdown' if kind == 'rush' else 'pass_touchdown')) == 1)
        yards = finite(r.get('yards_gained')) or 0.
        score = finite(r.get('score_differential'))
        state = None if score is None else 'leading' if score >= 8 else 'trailing' if score <= -8 else 'close'
        pos = positions.get(str(pid), 'UNK')
        pos = 'RB' if pos == 'FB' else pos
        for entity in (str(pid), '__team__'):
            item = result[(str(r['game_id']), str(r['posteam']), entity)]
            add_bins(item['buckets'], key, td, yards)
            add_bins(item['by_position'].setdefault(pos, {}), key, td, yards)
            # A separate audit total; not in PATHS and never added to TD mass.
            inside20 = kind == 'rec' and finite(r.get('yl')) is not None and r['yl'] <= 20
            if inside20:
                add_bins(item['buckets'], 'rec_inside20', td, yards)
                add_bins(item['by_position'][pos], 'rec_inside20', td, yards)
            if state:
                add_bins(item['states'].setdefault(state, {}), key, td, yards)
            passer = r.get('passer_player_id')
            if kind == 'rec' and isinstance(passer, str) and passer:
                add_bins(item['passers'].setdefault(passer, {}), key, td, yards)
                if inside20:
                    add_bins(item['passers'][passer], 'rec_inside20', td, yards)
    return result
