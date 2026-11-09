"""Same-draw anytime-touchdown joint for one pair in one game."""

from __future__ import annotations

from sqlalchemy.orm import Session

from spe.db.models import FeatureRow, Game, Player
from spe.simulate.engine import PlayerRole, simulate_game


def anytime_td_joint(
    session: Session,
    game: Game,
    player_a: str,
    player_b: str,
    *,
    draw_count: int = 8000,
    seed: int = 7,
) -> float | None:
    """Joint probability both players score, counted on the same draws."""
    from spe.identity.roster import eligible_players_for_game

    roles: list[PlayerRole] = []
    for row in eligible_players_for_game(
        session,
        game,
        include_inactive_for_props=False,
        apply_eligibility_audit=False,
    ):
        player = session.get(Player, row.player_id)
        if not player:
            continue
        feat = (
            session.query(FeatureRow)
            .filter(FeatureRow.player_id == player.player_id, FeatureRow.game_id == game.game_id)
            .first()
        )
        usage = (feat.feature_blob.get("usage") if feat else {}) or {}
        avail = (feat.feature_blob.get("availability") if feat else {}) or {}
        roles.append(
            PlayerRole(
                player_id=player.player_id,
                team_id=row.team_id,
                position_group=player.position_group,
                availability=float(avail.get("prob", 0.9)),
                snap_share=float(usage.get("snap_share", 0.5)),
                td_weight=float(usage.get("td_weight", 0.15)),
                rec_lambda=float(usage.get("rec_lambda", 3.0)),
                rec_yards_mu=float(usage.get("rec_yards_mu", 40.0)),
                rush_yards_mu=float(usage.get("rush_yards_mu", 20.0)),
            )
        )
    if not roles:
        return None
    sim = simulate_game(
        game.league_id,
        game.home_team_id,
        game.away_team_id,
        roles,
        draw_count=draw_count,
        seed=seed,
    )
    return sim.joint_td.get(f"{player_a}|{player_b}") or sim.joint_td.get(f"{player_b}|{player_a}")
