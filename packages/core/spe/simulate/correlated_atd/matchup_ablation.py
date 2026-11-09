"""Verified team-level defensive matchup ablation for the correlated simulator.

Never invents individual defender assignments.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.models.metrics import brier_score, expected_calibration_error
from spe.models.player_td.features import attach_defense_matchups
from spe.models.player_td.integration import TeamScoringContext
from spe.simulate.correlated_atd.phase5_isolation import phase5_dir
from spe.simulate.correlated_atd.player_weights import PlayerOpportunity
from spe.simulate.correlated_atd.scoring import load_league_frame, score_games, summarize_predictions

CANDIDATE_FEATURES = (
    "opp_rush_td_allowed",
    "opp_rec_td_allowed",
    "opp_rz_td_allowed",
    "opp_rush_epa_allowed",
    "opp_pass_epa_allowed",
    "opp_adj_def",  # may be absent; skipped if missing
)


def _zscore(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    mu = s.mean(skipna=True)
    sd = s.std(skipna=True)
    if not np.isfinite(sd) or sd < 1e-8:
        return pd.Series(np.zeros(len(s)), index=s.index)
    z = (s - mu) / sd
    return z.fillna(0.0)


def _apply_matchup(
    opps: list[PlayerOpportunity],
    gdf: pd.DataFrame,
    ctx: dict[str, TeamScoringContext],
    *,
    features: list[str],
    beta: float,
) -> list[PlayerOpportunity]:
    del ctx
    by_pid = {str(r.player_id): r for r in gdf.itertuples(index=False)}
    # Precompute z per feature on this game slice (relative to slate is weak; use available columns).
    zmap: dict[str, dict[str, float]] = {}
    for feat in features:
        if feat not in gdf.columns:
            continue
        z = _zscore(gdf[feat])
        zmap[feat] = {str(pid): float(zv) for pid, zv in zip(gdf["player_id"].astype(str), z)}

    out: list[PlayerOpportunity] = []
    for o in opps:
        nr = deepcopy(o)
        # Higher allowed TDs / worse defense → positive z → boost matching path.
        rush_boost = 0.0
        rec_boost = 0.0
        for feat, zm in zmap.items():
            z = zm.get(o.player_id, 0.0)
            if "rush" in feat:
                rush_boost += beta * z
            elif "rec" in feat or "pass" in feat:
                rec_boost += beta * z
            elif "rz" in feat:
                # RZ defense affects both, slightly more receiving for WR/TE.
                if o.position_group in {"WR", "TE"}:
                    rec_boost += 0.7 * beta * z
                else:
                    rush_boost += 0.5 * beta * z
            else:
                # Generic adj_def: higher = worse defense for offense scoring
                rush_boost += 0.4 * beta * z
                rec_boost += 0.4 * beta * z
        nr.rush_weight = max(0.0, nr.rush_weight * float(np.exp(np.clip(rush_boost, -0.5, 0.5))))
        if nr.position_group != "QB":
            nr.rec_weight = max(0.0, nr.rec_weight * float(np.exp(np.clip(rec_boost, -0.5, 0.5))))
        # Re-check player exists.
        if o.player_id in by_pid:
            nr.notes = list(nr.notes) + [f"matchup_feats={features}", f"beta={beta}"]
        out.append(nr)

    # Renormalize per team leaving 0.08/0.10 residual via sum scale to prior modeled mass.
    by_team: dict[str, list[PlayerOpportunity]] = {}
    for o in out:
        by_team.setdefault(o.team_id, []).append(o)
    final: list[PlayerOpportunity] = []
    for rows in by_team.values():
        # Preserve total modeled mass from original opportunity sums if possible.
        rush_sum = sum(r.rush_weight for r in rows) or 1.0
        rec_sum = sum(r.rec_weight for r in rows) or 1.0
        # Target mass ~ 0.9
        target = 0.90
        for r in rows:
            r.rush_weight = target * (r.rush_weight / rush_sum)
            r.rec_weight = 0.0 if r.position_group == "QB" else target * (r.rec_weight / rec_sum)
            final.append(r)
    return final


def run_matchup_ablation(
    session: Session,
    *,
    leagues: tuple[str, ...] = ("nfl", "fbs"),
    test_games: int = 70,
    draw_count: int = 1_500,
    seed: int = 11,
    beta: float = 0.12,
    frames: dict[str, pd.DataFrame] | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "beta": beta,
        "rule": "Retain feature set only if prospective-style test Brier improves and ECE does not worsen by >0.005",
        "leagues": {},
        "no_individual_matchups": True,
    }
    for league_id in leagues:
        print(f"[phase5/matchup] {league_id}…", flush=True)
        frame = (frames or {}).get(league_id)
        frame = frame if frame is not None else load_league_frame(session, league_id)
        if frame is None or frame.empty:
            report["leagues"][league_id] = {"error": "empty_frame"}
            continue
        # Attach verified defense priors when missing.
        need = [c for c in CANDIDATE_FEATURES if c not in frame.columns]
        if need:
            try:
                frame = attach_defense_matchups(session, frame, league_id)
            except Exception as exc:  # noqa: BLE001 — research path; record failure
                report.setdefault("attach_errors", {})[league_id] = str(exc)

        # Alias opp_adj_def if only prior_adj present
        if "opp_adj_def" not in frame.columns and "opp_adj_def" in CANDIDATE_FEATURES:
            if "prior_adj_def_def" in frame.columns:
                frame["opp_adj_def"] = pd.to_numeric(frame["prior_adj_def_def"], errors="coerce")

        games = list(dict.fromkeys(frame["game_id"].astype(str).tolist()))
        te = games[-test_games:] if len(games) > test_games else games[max(1, int(len(games) * 0.75)) :]
        available = [f for f in CANDIDATE_FEATURES if f in frame.columns and pd.to_numeric(frame[f], errors="coerce").notna().mean() > 0.2]
        coverage = {
            f: float(pd.to_numeric(frame[f], errors="coerce").notna().mean()) if f in frame.columns else 0.0
            for f in CANDIDATE_FEATURES
        }

        variants: dict[str, list[str]] = {"baseline_no_matchup": []}
        for feat in available:
            variants[f"only_{feat}"] = [feat]
        if available:
            variants["all_available"] = list(available)

        league_out: dict[str, Any] = {"coverage": coverage, "available": available, "variants": {}, "retained": [], "rejected": []}
        baseline_brier = baseline_ece = None
        for name, feats in variants.items():
            if name == "baseline_no_matchup":
                scored = score_games(frame, league_id, te, draw_count=draw_count, seed=seed, label=f"mu-{league_id}-base")
            else:

                def _xform(opps, gdf, ctx, feats=feats):  # noqa: ANN001
                    return _apply_matchup(opps, gdf, ctx, features=feats, beta=beta)

                scored = score_games(
                    frame,
                    league_id,
                    te,
                    draw_count=draw_count,
                    seed=seed,
                    opp_transform=_xform,
                    label=f"mu-{league_id}-{name}",
                )
            if scored["n"] == 0:
                continue
            y = scored["y"]
            p = scored["models"]["atd_sim_v1"]
            summary = summarize_predictions(y, p, scored["meta"], n_boot=150, seed=seed)
            league_out["variants"][name] = summary
            if name == "baseline_no_matchup":
                baseline_brier = summary["brier"]
                baseline_ece = summary["ece"]

        if baseline_brier is not None and baseline_ece is not None:
            for name, summary in league_out["variants"].items():
                if name == "baseline_no_matchup":
                    continue
                delta_b = summary["brier"] - baseline_brier
                delta_e = summary["ece"] - baseline_ece
                row = {"name": name, "delta_brier": delta_b, "delta_ece": delta_e, "brier": summary["brier"], "ece": summary["ece"]}
                if delta_b < -1e-4 and delta_e <= 0.005:
                    league_out["retained"].append(row)
                else:
                    league_out["rejected"].append(row)
        report["leagues"][league_id] = league_out

    path = phase5_dir() / "matchup_ablation.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["artifact_path"] = str(path)
    return report
