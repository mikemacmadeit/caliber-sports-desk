"""Audit offensive TD labels against independent complete scoring identities.

This module is for evaluation only. Current-game results never enter features.
Missing player rows may be labeled zero only when the complete game's rushing
and receiving scorers agree, player by player, across the two stored sources.
"""
from collections import Counter, defaultdict

from spe.ingest.free_college import external_id
from spe.models.player_td.desk_opportunity import canonical_team


def verified_labels(frame, records, aliases, player_ids):
    expected = defaultdict(Counter)
    games = {}
    for r in records:
        gid = r['game_id'].split(':')[-1]
        games[gid] = r
        for kind, field in (('rush','rush_td'), ('rec','rec_td')):
            if r.get(field):
                expected[gid][(r['team_id'],r['player_id'],kind)] += int(r[field])
    observed = defaultdict(Counter)
    invalid = set()
    seen_teams = defaultdict(set)
    valid = frame['status_type_completed'].fillna(False).astype(bool)
    for key in ('penalty_no_play','penalty_negated_play'):
        valid &= ~frame[key].fillna(False).astype(bool)
    df = frame.loc[valid].drop_duplicates(['game_id','id'],keep='last')
    columns = ['game_id','pos_team_id','homeTeamId','awayTeamId','rush_td','pass_td',
        'rusher_player_id','receiver_player_id','type.text']
    for p in df[columns].to_dict('records'):
        gid = external_id(p['game_id'])
        game = games.get(gid)
        if game is None:
            continue
        teams = {game['home_team_id'],game['away_team_id']}
        if {canonical_team('fbs:'+str(external_id(p[k])),aliases) for k in ('homeTeamId','awayTeamId')} != teams:
            invalid.add(gid)
            continue
        team = canonical_team('fbs:'+str(external_id(p['pos_team_id'])),aliases)
        if team in teams:
            seen_teams[gid].add(team)
        typ = str(p['type.text']).lower()
        # Restrict to rushing/receiving TDs, matching Caliber's graded label.
        kind = 'rush' if typ == 'rushing touchdown' else 'rec' if typ == 'passing touchdown' else None
        if kind is None:
            continue
        player = player_ids.get(external_id(p['rusher_player_id' if kind=='rush' else 'receiver_player_id']))
        if not player or team not in teams:
            invalid.add(gid)
            continue
        observed[gid][(team,player,kind)] += 1
    output = {}
    for gid, game in games.items():
        if gid in invalid or len(seen_teams[gid]) != 2 or observed[gid] != expected[gid]:
            continue
        output[game['game_id']] = {'scorers':sorted({p for (_,p,_) in observed[gid]}),
            'source':'matching_CFBD_box_and_SportsDataverse_scoring_identities',
            'label_scope':'rushing_or_receiving_touchdown',
            'touchdowns':sum(observed[gid].values())}
    return output
