"""Expanded chronological validation with examined vs prospective split."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.models.metrics import brier_score, log_loss
from spe.simulate.correlated_atd.phase5_isolation import phase5_dir, save_examined_games
from spe.simulate.correlated_atd.scoring import load_league_frame, score_games, summarize_predictions


def _split_games(
    frame: pd.DataFrame,
    *,
    examined_n: int,
    prospective_n: int,
    prior_examined: list[str] | None = None,
) -> dict[str, list[str]]:
    """Chronological split: earlier = examined (Phase 4–style), later = prospective holdout."""
    all_games = list(dict.fromkeys(frame["game_id"].astype(str).tolist()))
    if not all_games:
        return {"examined": [], "prospective": [], "expanded_all": []}

    # Prefer trailing prospective window as genuinely newer than Phase 4 sample.
    if len(all_games) <= examined_n + prospective_n:
        # Use last 25% as prospective when sample is tight.
        cut = max(1, int(len(all_games) * 0.75))
        examined = all_games[:cut]
        prospective = all_games[cut:]
    else:
        prospective = all_games[-prospective_n:]
        examined = all_games[-(examined_n + prospective_n) : -prospective_n]

    # Exclude prior Phase 4 examined ids from prospective when known.
    prior = set(prior_examined or [])
    prospective = [g for g in prospective if g not in prior]
    # Expanded evaluation = examined + prospective (report separately).
    expanded = list(dict.fromkeys([*examined, *prospective]))
    return {"examined": examined, "prospective": prospective, "expanded_all": expanded}


def _pack_block(scored: dict[str, Any], *, n_boot: int, seed: int, role: str) -> dict[str, Any]:
    y = scored["y"]
    meta = scored["meta"]
    if len(y) == 0:
        return {"role": role, "error": "no_rows", "n": 0, "n_games": 0}
    out: dict[str, Any] = {
        "role": role,
        "n": scored["n"],
        "n_games": scored["n_games"],
        "base_rate": float(np.mean(y)),
        "other_share": scored["other_share"],
        "models": {},
    }
    for name, p in scored["models"].items():
        out["models"][name] = summarize_predictions(y, p, meta, n_boot=n_boot, seed=seed + hash(name) % 1000)
    out["sim_vs_m6"] = {
        "brier_delta": out["models"]["atd_sim_v1"]["brier"] - out["models"]["m6"]["brier"],
        "log_loss_delta": out["models"]["atd_sim_v1"]["log_loss"] - out["models"]["m6"]["log_loss"],
        "brier_ci_sim": out["models"]["atd_sim_v1"]["brier_ci"],
        "brier_ci_m6": out["models"]["m6"]["brier_ci"],
        "note": "Prospective role is the primary promotion gate; examined is diagnostic only.",
    }
    joints = scored.get("joint_rows") or []
    if joints:
        jy = np.array([r["y"] for r in joints], dtype=float)
        jp_sim = np.array([r["p_sim"] for r in joints], dtype=float)
        jp_ind = np.array([r["p_indep"] for r in joints], dtype=float)
        out["joint_same_team"] = {
            "n_pairs": len(joints),
            "base_rate": float(jy.mean()),
            "sim": {"brier": brier_score(jy, jp_sim), "log_loss": log_loss(jy, jp_sim), "mean_p": float(jp_sim.mean())},
            "independent_product": {
                "brier": brier_score(jy, jp_ind),
                "log_loss": log_loss(jy, jp_ind),
                "mean_p": float(jp_ind.mean()),
            },
            "sim_minus_indep_brier": brier_score(jy, jp_sim) - brier_score(jy, jp_ind),
            "note": "Joint validation is separate from marginal Anytime TD accuracy.",
        }
    return out


def validate_league(
    session: Session,
    league_id: str,
    *,
    examined_games: int = 120,
    prospective_games: int = 80,
    draw_count: int = 1_800,
    n_boot: int = 300,
    prior_examined: list[str] | None = None,
    seed: int = 42,
    frame: pd.DataFrame | None = None,
) -> dict[str, Any]:
    frame = frame if frame is not None else load_league_frame(session, league_id)
    if frame is None or frame.empty:
        return {"league_id": league_id, "error": "empty_frame"}

    splits = _split_games(
        frame,
        examined_n=examined_games,
        prospective_n=prospective_games,
        prior_examined=prior_examined,
    )
    report: dict[str, Any] = {
        "league_id": league_id,
        "splits": {k: {"n_games": len(v), "game_ids_head": v[:3], "game_ids_tail": v[-3:]} for k, v in splits.items()},
        "roles": {},
    }

    for role, gids in (
        ("examined", splits["examined"]),
        ("prospective", splits["prospective"]),
        ("expanded_all", splits["expanded_all"]),
    ):
        if not gids:
            report["roles"][role] = {"role": role, "error": "empty_split", "n": 0, "n_games": 0}
            continue
        print(f"[phase5] {league_id} scoring role={role} games={len(gids)}…", flush=True)
        scored = score_games(
            frame,
            league_id,
            gids,
            draw_count=draw_count,
            seed=seed,
            label=f"phase5-{role}",
        )
        report["roles"][role] = _pack_block(scored, n_boot=n_boot, seed=seed, role=role)
        report["roles"][role]["game_ids"] = scored["scored_games"]

    report["examined_game_ids"] = report["roles"].get("examined", {}).get("game_ids") or splits["examined"]
    report["prospective_game_ids"] = report["roles"].get("prospective", {}).get("game_ids") or splits["prospective"]
    path = phase5_dir() / f"{league_id}_expanded_validation.json"
    # Strip bulky game id lists from disk copy except counts for readability? Keep them for provenance.
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["artifact_path"] = str(path)
    return report


def merge_examined(nfl_ids: list[str], fbs_ids: list[str]) -> None:
    save_examined_games({"nfl": nfl_ids, "fbs": fbs_ids})
