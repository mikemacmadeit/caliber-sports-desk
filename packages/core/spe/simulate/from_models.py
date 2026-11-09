"""Correlated simulations from trained rates. Calibration is tested before raising draw count."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.db.models import Game, ModelVersion, SimulationRun
from spe.models.dataset import build_model_frame
from spe.models.train import load_artifacts, predict_row
from spe.simulate.engine import PlayerRole, simulate_game

INITIAL_DRAWS = 500
RAISED_DRAWS = 2000
TARGET_SE = 0.02


def simulate_from_models(
    session: Session,
    league_id: str,
    *,
    game_id: str | None = None,
    seed: int = 42,
    persist: bool = True,
) -> dict[str, Any]:
    artifacts = load_artifacts(league_id)
    if not artifacts:
        return {"status": "no_artifacts", "league_id": league_id, "play_enabled": False}
    frame = build_model_frame(session, league_id)
    if frame.empty:
        return {"status": "no_stats", "league_id": league_id}
    game = _pick_game(session, league_id, game_id, frame)
    if game is None:
        return {"status": "no_game", "league_id": league_id}
    players = frame[frame["game_id"] == game.game_id].copy()
    if players.empty:
        return {"status": "no_players", "game_id": game.game_id}
    roles, model_p = _roles(players, artifacts, game)
    calib_500 = _run(league_id, game, roles, INITIAL_DRAWS, seed, model_p)
    draws = RAISED_DRAWS if calib_500["max_abs_td_gap"] > TARGET_SE or calib_500["max_td_mc_se"] > TARGET_SE else INITIAL_DRAWS
    result = _run(league_id, game, roles, draws, seed, model_p) if draws != INITIAL_DRAWS else calib_500
    result.update(
        {
            "status": "ok",
            "league_id": league_id,
            "game_id": game.game_id,
            "initial_draws": INITIAL_DRAWS,
            "final_draws": draws,
            "raised_draws": draws > INITIAL_DRAWS,
            "play_enabled": False,
            "research_only": True,
            "note": "Simulation is research-only. Validated Play stays gated until OOS promotion.",
        }
    )
    version_id = f"m3-{league_id}-anytime_td"
    if persist and session.get(ModelVersion, version_id) is not None:
        session.add(
            SimulationRun(
                game_id=game.game_id,
                as_of=game.kickoff_utc,
                seed=seed,
                draw_count=draws,
                assumptions={"source": "m3_trained_rates", "complete_after_hours": 4.5},
                scenario="research",
                overrides={"research_only": True},
                model_version_id=version_id,
                summary={k: v for k, v in result.items() if k not in {"player_td_prob", "model_td_p"}},
                convergence={"max_td_mc_se": result["max_td_mc_se"], "max_abs_td_gap": result["max_abs_td_gap"]},
                research_only=True,
            )
        )
        session.flush()
    return result


def _pick_game(session: Session, league_id: str, game_id: str | None, frame: pd.DataFrame) -> Game | None:
    if game_id:
        row = session.get(Game, game_id)
        if row and row.data_tier != "demo":
            return row
        return None
    counts = frame.groupby("game_id").size().sort_values(ascending=False)
    for gid in counts.index:
        row = session.get(Game, str(gid))
        if row and row.data_tier == "historical" and row.league_id == league_id:
            return row
    return None


def _roles(players: pd.DataFrame, artifacts: dict, game: Game) -> tuple[list[PlayerRole], dict[str, float]]:
    td_art = artifacts.get("anytime_td")
    rec_art = artifacts.get("receptions")
    rec_yds_art = artifacts.get("receiving_yards")
    rush_art = artifacts.get("rushing_yards")
    roles: list[PlayerRole] = []
    model_p: dict[str, float] = {}
    for rec in players.itertuples(index=False):
        z = float(getattr(rec, "opp_def_z", 0) or 0)
        n = int(getattr(rec, "prior_games", 0) or 0)
        pos = str(rec.position_group)
        team_id = str(getattr(rec, "team_id", "") or "")
        td = predict_row(td_art, rec.player_id, team_id, pos, z, n) if td_art else {"mean": 0.2, "p": 0.18}
        recs = predict_row(rec_art, rec.player_id, team_id, pos, z, n) if rec_art else {"mean": 3.0}
        rec_yds = predict_row(rec_yds_art, rec.player_id, team_id, pos, z, n) if rec_yds_art else {"mean": 35.0}
        rush = predict_row(rush_art, rec.player_id, team_id, pos, z, n) if rush_art else {"mean": 10.0}
        model_p[rec.player_id] = float(td["p"])
        roles.append(
            PlayerRole(
                player_id=rec.player_id,
                team_id=team_id or game.home_team_id,
                position_group=pos,
                availability=1.0,
                snap_share=max(0.15, min(0.95, float(getattr(rec, "prior_targets", 0) or 0) / 8.0 + 0.2)),
                td_weight=max(0.05, float(td["mean"])),
                rec_lambda=max(0.2, float(recs["mean"])),
                rec_yards_mu=max(1.0, float(rec_yds["mean"])),
                rush_yards_mu=max(0.5, float(rush["mean"])),
            )
        )
    return roles, model_p


def _run(league_id: str, game: Game, roles: list[PlayerRole], draws: int, seed: int, model_p: dict[str, float]) -> dict[str, Any]:
    sim = simulate_game(
        league_id,
        game.home_team_id,
        game.away_team_id,
        roles,
        draw_count=draws,
        seed=seed,
        overrides={"research_only": True},
    )
    gaps = []
    for pid, p_model in model_p.items():
        p_sim = sim.player_td_prob.get(pid, 0.0)
        gaps.append(abs(p_sim - p_model))
    return {
        "max_td_mc_se": sim.convergence["max_td_mc_se"],
        "max_abs_td_gap": float(max(gaps) if gaps else 0.0),
        "mean_abs_td_gap": float(np.mean(gaps) if gaps else 0.0),
        "player_td_prob": sim.player_td_prob,
        "model_td_p": model_p,
        "score_quantiles": sim.score_quantiles,
        "draw_count": draws,
    }
