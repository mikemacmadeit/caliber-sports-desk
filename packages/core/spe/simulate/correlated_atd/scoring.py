"""Shared scoring helpers for Phase 4/5 correlated ATD evaluation.

Does not write artifacts owned by M3–M6 or team_td.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.config import settings
from spe.models.metrics import brier_score, expected_calibration_error, log_loss
from spe.models.player_td.dataset import build_player_td_frame
from spe.models.player_td.integration import TeamScoringContext
from spe.models.player_td.shares import DEFAULT_OTHER_SHARE, default_weights
from spe.simulate.correlated_atd.engine import CorrelatedAtdResult, simulate_correlated_atd
from spe.simulate.correlated_atd.player_weights import load_m6, opportunities_from_frame_rows, PlayerOpportunity


def load_model_artifact(league_id: str, stem: str) -> dict[str, Any] | None:
    root = Path(settings.parquet_dir) / "models"
    for path in (root / f"{league_id}_{stem}.json", root / "player_td" / f"{league_id}_{stem}.json"):
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return None


def shrink_rate(art: dict[str, Any] | None, player_id: str, team_id: str, pos: str) -> float | None:
    if not art:
        return None
    rates = art.get("player_rates") or {}
    ns = art.get("player_n") or {}
    league = float(art.get("league_rate") or 0.15)
    pos_r = float((art.get("pos_rates") or {}).get(pos, league))
    team_r = float((art.get("team_rates") or {}).get(team_id or "", pos_r))
    k = float(art.get("k_player") or 12.0)
    n = int(ns.get(player_id, 0))
    raw = rates.get(player_id)
    prior = team_r if raw is None else float(raw)
    shrink = n / (n + k) if n > 0 else 0.0
    mean = shrink * prior + (1.0 - shrink) * team_r
    mean = max(1e-6, float(mean))
    return float(1.0 - np.exp(-mean))


def bootstrap_ci(y: np.ndarray, p: np.ndarray, metric: Callable[[np.ndarray, np.ndarray], float], *, n_boot: int = 400, seed: int = 0) -> dict[str, float]:
    rng = np.random.default_rng(seed)
    n = len(y)
    if n == 0:
        return {"point": float("nan"), "lo": float("nan"), "hi": float("nan")}
    point = float(metric(y, p))
    samples = np.empty(n_boot, dtype=float)
    for i in range(n_boot):
        idx = rng.integers(0, n, size=n)
        samples[i] = metric(y[idx], p[idx])
    return {
        "point": point,
        "lo": float(np.quantile(samples, 0.025)),
        "hi": float(np.quantile(samples, 0.975)),
        "n_boot": float(n_boot),
    }


def bucket_metrics(y: np.ndarray, p: np.ndarray, edges: list[float]) -> list[dict[str, float]]:
    out = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        mask = (p >= lo) & (p < hi if i < len(edges) - 2 else p <= hi)
        if not np.any(mask):
            continue
        out.append(
            {
                "lo": lo,
                "hi": hi,
                "n": float(mask.sum()),
                "mean_p": float(np.mean(p[mask])),
                "mean_y": float(np.mean(y[mask])),
                "brier": brier_score(y[mask], p[mask]),
                "gap": float(np.mean(p[mask]) - np.mean(y[mask])),
            }
        )
    return out


def by_position(meta: pd.DataFrame, y: np.ndarray, p: np.ndarray) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for pos in meta["position_group"].astype(str).unique():
        mask = meta["position_group"].astype(str).eq(str(pos)).to_numpy()
        if int(mask.sum()) < 20:
            continue
        out[str(pos)] = {
            "n": float(mask.sum()),
            "brier": brier_score(y[mask], p[mask]),
            "log_loss": log_loss(y[mask], p[mask]),
            "ece": expected_calibration_error(y[mask], p[mask]),
            "base_rate": float(np.mean(y[mask])),
            "mean_p": float(np.mean(p[mask])),
        }
    return out


def summarize_predictions(
    y: np.ndarray,
    p: np.ndarray,
    meta: pd.DataFrame,
    *,
    n_boot: int = 400,
    seed: int = 0,
) -> dict[str, Any]:
    return {
        "n": int(len(y)),
        "brier": brier_score(y, p),
        "log_loss": log_loss(y, p),
        "ece": expected_calibration_error(y, p),
        "mean_p": float(np.mean(p)),
        "base_rate": float(np.mean(y)),
        "brier_ci": bootstrap_ci(y, p, brier_score, n_boot=n_boot, seed=seed),
        "log_loss_ci": bootstrap_ci(y, p, log_loss, n_boot=n_boot, seed=seed + 1),
        "by_position": by_position(meta, y, p),
        "by_probability_range": bucket_metrics(y, p, [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0]),
    }


def team_contexts_from_frame(gdf: pd.DataFrame, league_id: str, game_id: str) -> dict[str, TeamScoringContext]:
    ctx: dict[str, TeamScoringContext] = {}
    for tid, part in gdf.groupby("team_id"):
        lam_off = float(pd.to_numeric(part["team_lambda_off_td"], errors="coerce").mean())
        lam_pass = float(pd.to_numeric(part["team_lambda_pass_td"], errors="coerce").mean())
        lam_rush = float(pd.to_numeric(part["team_lambda_rush_td"], errors="coerce").mean())
        if not np.isfinite(lam_off):
            lam_off = 2.3 if league_id == "nfl" else 3.0
        if not np.isfinite(lam_pass):
            lam_pass = lam_off * 0.6
        if not np.isfinite(lam_rush):
            lam_rush = lam_off * 0.4
        ctx[str(tid)] = TeamScoringContext(
            game_id=str(game_id),
            team_id=str(tid),
            league_id=league_id,
            expected_off_tds=lam_off,
            expected_rush_tds=lam_rush,
            expected_rec_tds=lam_pass,
            model_version="team_td_v1_frame",
            research_only=True,
            notes=("from_player_td_frame",),
        )
    return ctx


def home_away_ids(gdf: pd.DataFrame) -> tuple[str, str]:
    teams = sorted(gdf["team_id"].dropna().astype(str).unique().tolist())
    if not teams:
        return "home", "away"
    if "is_home" in gdf.columns:
        home_rows = gdf[gdf["is_home"].astype(float) > 0.5]
        away_rows = gdf[gdf["is_home"].astype(float) < 0.5]
        home_id = str(home_rows["team_id"].iloc[0]) if len(home_rows) else teams[0]
        away_id = str(away_rows["team_id"].iloc[0]) if len(away_rows) else (teams[1] if len(teams) > 1 else teams[0])
        return home_id, away_id
    return teams[0], teams[1] if len(teams) > 1 else teams[0]


OppTransform = Callable[[list[PlayerOpportunity], pd.DataFrame, dict[str, TeamScoringContext]], list[PlayerOpportunity]]


def score_games(
    frame: pd.DataFrame,
    league_id: str,
    game_ids: list[str],
    *,
    draw_count: int = 2_000,
    seed: int = 42,
    other_share: float | None = None,
    opp_transform: OppTransform | None = None,
    ctx_transform: Callable[[dict[str, TeamScoringContext], pd.DataFrame], dict[str, TeamScoringContext]] | None = None,
    progress_every: int = 25,
    label: str = "score",
) -> dict[str, Any]:
    """Score identical game/player set; return y, model vectors, joints, examined game ids."""
    test = frame[frame["game_id"].astype(str).isin(game_ids)].copy()
    test = test.sort_values("kickoff_utc")
    m3 = load_model_artifact(league_id, "m3")
    m5 = load_model_artifact(league_id, "m5")
    m6 = load_m6(league_id)
    other = other_share if other_share is not None else float(default_weights(league_id).other_share or DEFAULT_OTHER_SHARE)

    sim_ps: list[float] = []
    m3_ps: list[float] = []
    m5_ps: list[float] = []
    m6_ps: list[float] = []
    ys: list[float] = []
    meta_rows: list[dict[str, Any]] = []
    joint_rows: list[dict[str, Any]] = []
    scored_games: list[str] = []

    for g_i, (game_id, gdf) in enumerate(test.groupby("game_id", sort=False)):
        if progress_every and g_i > 0 and g_i % progress_every == 0:
            print(f"[atd_sim/{label}] {league_id} games {g_i}/{len(game_ids)}…", flush=True)
        home_id, away_id = home_away_ids(gdf)
        ctx = team_contexts_from_frame(gdf, league_id, str(game_id))
        if ctx_transform:
            ctx = ctx_transform(ctx, gdf)
        opps = opportunities_from_frame_rows(gdf, league_id)
        if opp_transform:
            opps = opp_transform(opps, gdf, ctx)
        if not opps:
            continue
        result = simulate_correlated_atd(
            league_id,
            str(game_id),
            home_id,
            away_id,
            opps,
            ctx,
            draw_count=draw_count,
            seed=seed + g_i,
            other_share=other,
        )
        scored_games.append(str(game_id))
        by_pid = {p.player_id: p for p in result.players}
        _collect_joints(result, gdf, joint_rows)

        for rec in gdf.itertuples(index=False):
            pid = str(rec.player_id)
            pos = str(rec.position_group)
            tid = str(rec.team_id)
            y = float((float(getattr(rec, "anytime_tds", 0) or 0) > 0))
            sim_p = by_pid[pid].p_anytime_td if pid in by_pid else 0.0
            p3 = shrink_rate(m3, pid, tid, pos)
            p5 = shrink_rate(m5, pid, tid, pos)
            p6 = by_pid[pid].m6_p if pid in by_pid and by_pid[pid].m6_p is not None else shrink_rate(m6, pid, tid, pos)
            base = float((m6 or m5 or {}).get("league_rate") or 0.15)
            sim_ps.append(sim_p)
            m3_ps.append(p3 if p3 is not None else base)
            m5_ps.append(p5 if p5 is not None else base)
            m6_ps.append(p6 if p6 is not None else base)
            ys.append(y)
            meta_rows.append(
                {
                    "position_group": pos,
                    "player_id": pid,
                    "game_id": str(game_id),
                    "team_id": tid,
                }
            )

    y = np.asarray(ys, dtype=float)
    meta = pd.DataFrame(meta_rows) if meta_rows else pd.DataFrame(columns=["position_group", "player_id", "game_id", "team_id"])
    models = {
        "atd_sim_v1": np.asarray(sim_ps, dtype=float),
        "m3": np.asarray(m3_ps, dtype=float),
        "m5": np.asarray(m5_ps, dtype=float),
        "m6": np.asarray(m6_ps, dtype=float),
    }
    return {
        "y": y,
        "meta": meta,
        "models": models,
        "joint_rows": joint_rows,
        "scored_games": scored_games,
        "other_share": other,
        "n_games": len(scored_games),
        "n": int(len(y)),
    }


def _collect_joints(result: CorrelatedAtdResult, gdf: pd.DataFrame, joint_rows: list[dict[str, Any]]) -> None:
    # All same-team skill pairs with both players in frame — not just top pair.
    y_map = {
        str(r.player_id): float((float(getattr(r, "anytime_tds", 0) or 0) > 0))
        for r in gdf.itertuples(index=False)
    }
    for key, jp in result.joint_same_team.items():
        a_id, b_id = key.split("|", 1)
        if a_id not in y_map or b_id not in y_map:
            continue
        ya, yb = y_map[a_id], y_map[b_id]
        pa = next((p.p_anytime_td for p in result.players if p.player_id == a_id), None)
        pb = next((p.p_anytime_td for p in result.players if p.player_id == b_id), None)
        if pa is None or pb is None:
            continue
        joint_rows.append(
            {
                "p_sim": float(jp),
                "p_indep": float(pa * pb),
                "y": float(ya * yb),
                "game_id": result.game_id,
                "pair": key,
            }
        )


def load_league_frame(session: Session, league_id: str) -> pd.DataFrame:
    print(f"[atd_sim] building player frame for {league_id}…", flush=True)
    frame, _ = build_player_td_frame(session, league_id)
    if frame.empty:
        return frame
    frame = frame.copy()
    frame["kickoff_utc"] = pd.to_datetime(frame["kickoff_utc"], utc=True, errors="coerce")
    return frame.sort_values("kickoff_utc")
