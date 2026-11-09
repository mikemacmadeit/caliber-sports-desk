"""Real-game research examples for the correlated ATD simulator."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from spe.db.models import Game
from spe.models.player_td.shares import default_weights
from spe.simulate.correlated_atd.compare_notes import explain_m6_deltas
from spe.simulate.correlated_atd.engine import simulate_correlated_atd
from spe.simulate.correlated_atd.player_weights import build_opportunities
from spe.simulate.correlated_atd.team_adapter import TeamTdV1Provider, team_names, upcoming_games
from spe.simulate.correlated_atd.validate import compare_to_m6


def simulate_game_example(
    session: Session,
    game: Game,
    provider: TeamTdV1Provider,
    *,
    draw_count: int = 6_000,
    seed: int = 42,
) -> dict[str, Any]:
    ctx = provider.score_game(game)
    if not ctx:
        return {
            "game_id": game.game_id,
            "league_id": game.league_id,
            "error": "team_td_model_unavailable",
            "research_only": True,
            "validated_play": False,
        }
    opps = build_opportunities(session, game, ctx)
    if not opps:
        return {
            "game_id": game.game_id,
            "league_id": game.league_id,
            "error": "no_eligible_skill_players",
            "research_only": True,
            "validated_play": False,
        }
    other = float(default_weights(game.league_id).other_share)

    result = simulate_correlated_atd(
        game.league_id,
        game.game_id,
        game.home_team_id,
        game.away_team_id,
        opps,
        ctx,
        draw_count=draw_count,
        seed=seed,
        other_share=other,
        exp_possessions={
            tid: float((provider.raw_prediction(game.game_id, tid) or {}).get("exp_possessions") or 11.5)
            for tid in ctx
        },
    )
    names = team_names(session, game)
    m6_cmp = compare_to_m6(result)
    factors = []
    for tid in (game.home_team_id, game.away_team_id):
        raw = provider.raw_prediction(game.game_id, tid)
        snap = (raw or {}).get("features_snapshot") or {}
        factors.append(
            {
                "team_id": tid,
                "epa_edge": snap.get("epa_edge"),
                "opp_prior_off_td_allowed": snap.get("opp_prior_off_td_allowed"),
                "home_field": snap.get("home_field"),
                "weather_available": snap.get("weather_available"),
                "injury_report_complete": snap.get("injury_report_complete"),
                "exp_possessions": snap.get("exp_possessions"),
                "sample_size": snap.get("sample_size"),
            }
        )
    return {
        "game_id": game.game_id,
        "league_id": game.league_id,
        "kickoff_utc": str(game.kickoff_utc),
        "home": names["home"],
        "away": names["away"],
        "research_only": True,
        "validated_play": False,
        "play_enabled": False,
        "label": "research_simulation_not_validated",
        "team_td_distribution": result.team_expected,
        "expected_offensive_tds": {
            tid: body.get("expected_off_tds") for tid, body in result.team_expected.items()
        },
        "top_five_scorers": result.to_dict()["top_scorers"],
        "players": [p.to_dict() for p in sorted(result.players, key=lambda x: x.p_anytime_td, reverse=True)[:24]],
        "scoring_opportunity_factors": factors,
        "vs_m6": m6_cmp,
        "delta_explanations": explain_m6_deltas(result),
        "data_limitations": [
            "Probabilities are research-only and not labeled validated.",
            "FBS red-zone / goal-line features are not fabricated when missing.",
            "No invented individual defensive assignments (CB/WR matchups).",
            "Residual OTHER share preserves non-modeled scorers.",
            "Not connected to live Anytime TD Finder.",
            f"other_share={other}",
        ],
        "mechanics": result.mechanics,
        "convergence": result.convergence,
        "n_eligible_modeled": len(opps),
    }


def run_upcoming_examples(
    session: Session,
    *,
    leagues: tuple[str, ...] = ("nfl", "fbs"),
    horizon_hours: int = 24 * 14,
    limit_per_league: int = 2,
    draw_count: int = 6_000,
) -> dict[str, Any]:
    provider = TeamTdV1Provider(session)
    games = upcoming_games(
        session,
        leagues=leagues,
        horizon_hours=horizon_hours,
        limit_per_league=limit_per_league,
    )
    examples = [simulate_game_example(session, g, provider, draw_count=draw_count) for g in games]
    return {
        "n_games": len(examples),
        "examples": examples,
        "research_only": True,
        "validated_play": False,
    }
