"""Chronological backtest of correlated ATD sim vs M3/M4/M5/M6 baselines."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.config import settings
from spe.models.metrics import brier_score, expected_calibration_error, log_loss
from spe.models.player_td.dataset import build_player_td_frame
from spe.models.player_td.integration import TeamScoringContext
from spe.models.player_td.shares import DEFAULT_OTHER_SHARE
from spe.simulate.correlated_atd.engine import simulate_correlated_atd
from spe.simulate.correlated_atd.isolation import artifact_dir
from spe.simulate.correlated_atd.player_weights import load_m6, opportunities_from_frame_rows


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _shrink_rate(art: dict[str, Any] | None, player_id: str, team_id: str, pos: str) -> float | None:
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


def _bucket_metrics(y: np.ndarray, p: np.ndarray, edges: list[float]) -> list[dict[str, float]]:
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
            }
        )
    return out


def _by_position(frame: pd.DataFrame, y: np.ndarray, p: np.ndarray) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for pos in frame["position_group"].astype(str).unique():
        mask = frame["position_group"].astype(str).eq(str(pos)).to_numpy()
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


def backtest_league(
    session: Session,
    league_id: str,
    *,
    shadow_days: int = 60,
    draw_count: int = 2_500,
    seed: int = 42,
    max_games: int | None = 120,
) -> dict[str, Any]:
    """Score last ``shadow_days`` of completed skill-player rows with the correlated sim.

    Uses prior features + team λ already on the player_td frame (chronological priors).
    Does not write live forecasts.
    """
    print(f"[atd_sim] building player frame for {league_id}…", flush=True)
    frame, _shares = build_player_td_frame(session, league_id)
    if frame.empty:
        return {"league_id": league_id, "error": "empty_frame"}

    frame = frame.copy()
    frame["kickoff_utc"] = pd.to_datetime(frame["kickoff_utc"], utc=True, errors="coerce")
    kick = frame["kickoff_utc"]
    cutoff = kick.max() - pd.Timedelta(days=shadow_days)
    test = frame[kick >= cutoff].copy()
    if test.empty:
        cut = int(len(frame) * 0.9)
        test = frame.iloc[cut:].copy()
    test = test.sort_values("kickoff_utc")

    # Limit games for runtime.
    game_ids = list(dict.fromkeys(test["game_id"].astype(str).tolist()))
    if max_games is not None:
        game_ids = game_ids[-max_games:]
    test = test[test["game_id"].astype(str).isin(game_ids)].copy()

    models_root = Path(settings.parquet_dir) / "models"
    m3 = _load_json(models_root / f"{league_id}_m3.json")
    m4 = _load_json(models_root / f"{league_id}_m4.json")
    m5 = _load_json(models_root / f"{league_id}_m5.json")
    m6 = load_m6(league_id)

    sim_ps: list[float] = []
    m3_ps: list[float] = []
    m4_ps: list[float] = []
    m5_ps: list[float] = []
    m6_ps: list[float] = []
    ys: list[float] = []
    meta_rows: list[dict[str, Any]] = []
    joint_rows: list[dict[str, Any]] = []

    rng_seed = seed
    for g_i, (game_id, gdf) in enumerate(test.groupby("game_id", sort=False)):
        if g_i > 0 and g_i % 20 == 0:
            print(f"[atd_sim] {league_id} games {g_i}/{len(game_ids)}…", flush=True)
        teams = sorted(gdf["team_id"].dropna().astype(str).unique().tolist())
        if len(teams) < 2:
            # Still simulate with whatever teams present; skip joint home/away naming.
            if len(teams) == 0:
                continue
            teams = [teams[0], teams[0]]
        home_id = str(gdf["team_id"].iloc[0]) if "is_home" not in gdf.columns else None
        if "is_home" in gdf.columns:
            home_rows = gdf[gdf["is_home"].astype(float) > 0.5]
            away_rows = gdf[gdf["is_home"].astype(float) < 0.5]
            home_id = str(home_rows["team_id"].iloc[0]) if len(home_rows) else teams[0]
            away_id = str(away_rows["team_id"].iloc[0]) if len(away_rows) else (teams[1] if len(teams) > 1 else teams[0])
        else:
            home_id, away_id = teams[0], teams[1] if len(teams) > 1 else teams[0]

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
                notes=("backtest_from_player_td_frame",),
            )

        opps = opportunities_from_frame_rows(gdf, league_id)
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
            seed=rng_seed + g_i,
            other_share=DEFAULT_OTHER_SHARE if league_id == "nfl" else 0.10,
        )
        by_pid = {p.player_id: p for p in result.players}

        # Joint accuracy where both scored or neither — use top same-team pair by product.
        same = list(result.joint_same_team.items())
        if same:
            key, jp = max(same, key=lambda kv: kv[1])
            a_id, b_id = key.split("|", 1)
            ya = float((gdf.loc[gdf["player_id"].astype(str) == a_id, "anytime_tds"].fillna(0) > 0).any())
            yb = float((gdf.loc[gdf["player_id"].astype(str) == b_id, "anytime_tds"].fillna(0) > 0).any())
            joint_rows.append({"p": jp, "y": float(ya * yb), "independent": None})

        for rec in gdf.itertuples(index=False):
            pid = str(rec.player_id)
            pos = str(rec.position_group)
            tid = str(rec.team_id)
            y = float((float(getattr(rec, "anytime_tds", 0) or 0) > 0))
            sim_p = by_pid[pid].p_anytime_td if pid in by_pid else 0.0
            p3 = _shrink_rate(m3, pid, tid, pos)
            p4 = _shrink_rate(m4, pid, tid, pos)
            p5 = _shrink_rate(m5, pid, tid, pos)
            p6 = by_pid[pid].m6_p if pid in by_pid and by_pid[pid].m6_p is not None else _shrink_rate(m6, pid, tid, pos)
            # Fallbacks keep row in eval with league base.
            base = float((m6 or m5 or {}).get("league_rate") or 0.15)
            sim_ps.append(sim_p)
            m3_ps.append(p3 if p3 is not None else base)
            m4_ps.append(p4 if p4 is not None else base)
            m5_ps.append(p5 if p5 is not None else base)
            m6_ps.append(p6 if p6 is not None else base)
            ys.append(y)
            meta_rows.append({"position_group": pos, "player_id": pid, "game_id": str(game_id)})

    if not ys:
        return {"league_id": league_id, "error": "no_scored_rows"}

    y = np.asarray(ys, dtype=float)
    models = {
        "atd_sim_v1": np.asarray(sim_ps, dtype=float),
        "m3": np.asarray(m3_ps, dtype=float),
        "m4": np.asarray(m4_ps, dtype=float),
        "m5": np.asarray(m5_ps, dtype=float),
        "m6": np.asarray(m6_ps, dtype=float),
    }
    meta = pd.DataFrame(meta_rows)
    summary: dict[str, Any] = {"league_id": league_id, "n": int(len(y)), "n_games": len(game_ids), "base_rate": float(y.mean())}
    for name, p in models.items():
        summary[name] = {
            "brier": brier_score(y, p),
            "log_loss": log_loss(y, p),
            "ece": expected_calibration_error(y, p),
            "mean_p": float(np.mean(p)),
            "by_position": _by_position(meta, y, p),
            "by_odds_proxy_bucket": _bucket_metrics(y, p, [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0]),
        }

    # Delta vs M6
    summary["sim_vs_m6"] = {
        "brier_delta": summary["atd_sim_v1"]["brier"] - summary["m6"]["brier"],
        "log_loss_delta": summary["atd_sim_v1"]["log_loss"] - summary["m6"]["log_loss"],
        "note": (
            "Negative delta means sim improves vs M6. Positive does not invalidate the joint model; "
            "marginal Brier can worsen while teammate dependence improves."
        ),
    }

    if joint_rows:
        jy = np.array([r["y"] for r in joint_rows], dtype=float)
        jp = np.array([r["p"] for r in joint_rows], dtype=float)
        summary["joint_same_team_top_pair"] = {
            "n_games": len(joint_rows),
            "brier": brier_score(jy, jp),
            "base_rate": float(jy.mean()),
            "mean_p": float(jp.mean()),
        }

    out_path = artifact_dir() / f"{league_id}_atd_sim_backtest.json"
    out_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    summary["artifact_path"] = str(out_path)
    return summary
