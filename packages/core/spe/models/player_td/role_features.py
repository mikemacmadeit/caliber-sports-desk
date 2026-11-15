"""Pregame role features aligned to team games, not just player appearances."""
ROLE_FEATURES = ['role_carries', 'role_receptions', 'role_targets',
                 'role_carry_share', 'role_receiving_share', 'role_carry_change',
                 'role_receiving_change', 'role_recent_appearance_rate',
                 'favorite_carry_share', 'favorite_receiving_share']


def recent_role(history, player, team, cutoff):
    own = history.recent(history.players.get((player, team), []), cutoff)
    games = history.recent(history.teams.get(team, []), cutoff)[-2:]
    by_game = {r['game_id']: r for r in own}
    if not games:
        return {k: 0.0 for k in ROLE_FEATURES}
    # Team games with stored boxes only. Missing rows mean no measured role,
    # not proof of inactivity; unknown whole-team boxes are not fabricated.
    mean = lambda key: sum(float(by_game.get(g['game_id'], {}).get(key) or 0) for g in games) / len(games)
    total = lambda key: sum(float(g.get(key) or 0) for g in games) / len(games)
    from spe.models.player_td.desk_opportunity import _mean
    carries, rec, targets = mean('carries'), mean('receptions'), mean('targets')
    return {'role_carries': carries, 'role_receptions': rec, 'role_targets': targets,
            'role_carry_share': carries/max(total('carries'), 1),
            'role_receiving_share': rec/max(total('receptions'), 1),
            'role_carry_change': carries-_mean(own, 'carries'),
            'role_receiving_change': rec-_mean(own, 'receptions'),
            'role_recent_appearance_rate': sum(g['game_id'] in by_game for g in games)/len(games),
            'role_complete_boxes':sum(bool(g.get('complete_player_box')) for g in games)}


def interactions(features):
    favored = -float(features.get('market_team_spread') or 0)/14
    return {'favorite_carry_share': favored*features.get('role_carry_share', 0),
            'favorite_receiving_share': favored*features.get('role_receiving_share', 0)}
