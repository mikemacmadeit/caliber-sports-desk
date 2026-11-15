"""Build a pregame pool independently of the target game's box-score participants."""
from datetime import timedelta


def team_candidates(history, allocator, team, opponent, cutoff, is_home):
    # AllocationHistory.pool enforces season/team identity and a 21-day recency
    # window. Read identities only from prior appearances, never target labels.
    for player in sorted(allocator.pool(team, cutoff)):
        prior = history.recent(history.players.get((player, team), []), cutoff)
        if not prior or cutoff-prior[-1]['kickoff_utc'] > timedelta(days=21):
            continue
        position = prior[-1]['position']
        f = history.features(player, team, opponent, position, cutoff, is_home)
        if f is None or f['carries']+f['receptions'] < 1:
            continue
        f['days_since_appearance'] = (cutoff-prior[-1]['kickoff_utc']).total_seconds()/86400
        f['recent_role_observation_games'] = len(history.recent(history.teams.get(team, []), cutoff)[-2:])
        yield {'player_id':player, 'player_name':prior[-1].get('player_name',player),
               'position':position, 'features':f}
