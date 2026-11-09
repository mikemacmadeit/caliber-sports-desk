"""Historical replay: score last week's finished FBS games, grade vs actual TDs.

This is a chronological holdout replay (priors use only prior games). It is NOT
the same as live pre-kickoff freezes. Research-only; Validated Play stays off.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.models.metrics import brier_score, log_loss
from spe.models.player_td.dataset import build_player_td_frame
from spe.models.player_td.model import evaluate_binary, predict_frame
from spe.models.player_td.train import load_player_td_model
from spe.models.rolling_td import load_m5, predict_anytime_td_m5
from spe.research.m5_fbs_lock_handoff import _score_c_recent
from spe.research.m5_fbs_refit import RECENT_MODEL_ID, out_dir as m5_refit_dir


def report_dir() -> Path:
    path = Path("data") / "atd_sim_dev" / "live_weekend"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_c_recent_artifact() -> dict[str, Any] | None:
    root = m5_refit_dir()
    candidates = [
        root / "locked" / "m5_fbs_research_recent_form_v1.locked.json",
        root / "locked" / "C_RECENT_LOCKED.json",
        root / "locked" / "fbs_m5_c_recent_locked_v1.json",
        root / f"{RECENT_MODEL_ID}.json",
    ]
    for p in candidates:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    # Fall back to production m5 weights with C_recent scoring wrapper.
    return load_m5("fbs")


def run_last_week_replay(
    session: Session,
    *,
    end_date: str | None = None,
    days: int = 7,
) -> dict[str, Any]:
    """Replay models on finished FBS player-games in the trailing window."""
    end = datetime.fromisoformat(end_date).replace(tzinfo=UTC) if end_date else datetime.now(UTC)
    # Default: week ending yesterday (exclude today live slate).
    end_day = (end - timedelta(days=1)).date()
    start_day = end_day - timedelta(days=days - 1)

    print(f"[replay] building player_td frame…", flush=True)
    frame, _ = build_player_td_frame(session, "fbs", year_max=2026, universe="corrected")
    if frame.empty:
        return {"status": "empty_frame", "play_enabled": False, "validated_play": False}

    ko = pd.to_datetime(frame["kickoff_utc"], utc=True)
    mask = (ko.dt.date >= start_day) & (ko.dt.date <= end_day)
    week = frame.loc[mask].copy()
    if week.empty:
        return {
            "status": "no_rows_in_window",
            "window": {"start": str(start_day), "end": str(end_day)},
            "play_enabled": False,
            "validated_play": False,
        }

    week["y"] = (pd.to_numeric(week.get("anytime_tds"), errors="coerce").fillna(0) > 0).astype(float)
    week["y_atd"] = week["y"]

    # player_td_v1
    v1_model = load_player_td_model("fbs")
    v1_metrics = None
    if v1_model is not None:
        scored_v1 = predict_frame(v1_model, week)
        week["p_v1"] = scored_v1["p_atd"].to_numpy(dtype=float)
        v1_metrics = evaluate_binary(week.assign(p_atd=week["p_v1"]), p_col="p_atd")
    else:
        week["p_v1"] = np.nan

    # M5 baseline + C_recent on the same historical rows (leakage-safe priors in frame).
    m5 = load_m5("fbs")
    c_art = _load_c_recent_artifact()
    p_m5 = []
    p_c = []
    feat_keys = (
        "prior_td_rate",
        "recent8_td_rate",
        "recent8_td_rate_current_team",
        "prior_carries",
        "prior_receptions",
        "opp_def_z",
        "prior_atd_game_rate",
    )
    for pl in week.to_dict(orient="records"):
        feats = {
            k: float(pl[k])
            for k in feat_keys
            if k in pl and pl[k] is not None and np.isfinite(float(pl[k]))
        }
        if m5:
            try:
                pred = predict_anytime_td_m5(
                    m5,
                    player_id=str(pl.get("player_id")),
                    team_id=str(pl.get("team_id") or ""),
                    position_group=str(pl.get("position_group") or "WR"),
                    opp_def_z=float(feats.get("opp_def_z") or 0.0),
                    features=feats,
                )
                p_m5.append(float(pred["p"]))
            except Exception:
                p_m5.append(np.nan)
        else:
            p_m5.append(np.nan)
        if c_art:
            try:
                scored = _score_c_recent(c_art, pl) if isinstance(c_art, dict) else {}
                raw = None
                if isinstance(scored, dict):
                    raw = scored.get("p_atd", scored.get("p"))
                p_c.append(float(raw) if raw is not None and np.isfinite(float(raw)) else np.nan)
            except Exception:
                p_c.append(np.nan)
        else:
            p_c.append(np.nan)

    week["p_m5"] = p_m5
    week["p_c_recent"] = p_c
    # Prefer full blend; if C_recent fails a row, fall back to v1 so history still grades.
    week["p_blend_raw"] = np.where(
        np.isfinite(week["p_v1"]) & np.isfinite(week["p_c_recent"]),
        0.60 * week["p_v1"] + 0.40 * week["p_c_recent"],
        week["p_v1"],
    )
    try:
        from spe.shadow.atd_role_calibration import apply_role_calibration, load_artifact

        art = load_artifact()
        if art and art.get("ok"):
            week["p_blend"] = apply_role_calibration(
                week["p_blend_raw"].to_numpy(dtype=float),
                frame=week,
                artifact=art,
            )
        else:
            week["p_blend"] = week["p_blend_raw"]
    except Exception:  # noqa: BLE001
        week["p_blend"] = week["p_blend_raw"]

    def _metrics(col: str) -> dict[str, Any] | None:
        part = week[np.isfinite(week[col])].copy()
        if part.empty:
            return None
        y = part["y"].to_numpy(dtype=float)
        p = np.clip(part[col].to_numpy(dtype=float), 1e-6, 1 - 1e-6)
        return {
            "n": int(len(part)),
            "brier": float(brier_score(y, p)),
            "log_loss": float(log_loss(y, p)),
            "base_rate": float(y.mean()),
            "mean_p": float(p.mean()),
            "hit_rate_top20pct": _top_bucket_hit(part, col, 0.20),
        }

    one_per_game = _one_pick_per_game(week, "p_blend")

    # Sample top predicted scorers vs actual
    examples = []
    ranked = week[np.isfinite(week["p_blend"])].sort_values("p_blend", ascending=False)
    for _, r in ranked.head(12).iterrows():
        examples.append(
            {
                "player_id": r.get("player_id"),
                "game_id": r.get("game_id"),
                "position": r.get("position_group"),
                "p_blend": round(float(r["p_blend"]), 4),
                "p_v1": round(float(r["p_v1"]), 4) if np.isfinite(r["p_v1"]) else None,
                "p_c_recent": round(float(r["p_c_recent"]), 4) if np.isfinite(r["p_c_recent"]) else None,
                "scored_td": bool(r["y"] >= 1),
                "anytime_tds": float(r.get("anytime_tds") or 0),
            }
        )

    blend = _metrics("p_blend")
    confidence = _confidence_plain(blend, one_per_game)

    report = {
        "title": "FBS Anytime TD historical replay",
        "generated_at": datetime.now(UTC).isoformat(),
        "label": "historical_chronological_holdout_replay",
        "not_live_prospective": True,
        "play_enabled": False,
        "validated_play": False,
        "window": {"start": str(start_day), "end": str(end_day), "days": days},
        "n_player_games": int(len(week)),
        "n_games": int(week["game_id"].nunique()) if "game_id" in week.columns else None,
        "c_recent_artifact_loaded": c_art is not None,
        "n_c_recent_scored": int(np.isfinite(week["p_c_recent"]).sum()),
        "metrics": {
            "player_td_v1": _metrics("p_v1"),
            "m5": _metrics("p_m5"),
            "c_recent": _metrics("p_c_recent"),
            "blend_v1_c_recent": blend,
            "player_td_v1_evaluate_binary": v1_metrics,
        },
        "one_pick_per_game": one_per_game,
        "confidence": confidence,
        "top_blend_predictions": examples,
        "how_to_read": (
            "We re-scored finished games using only info available before each kickoff "
            "(expanding priors). Lower Brier is better. One-pick-per-game is the closest "
            "practice version of the desk. This is practice against history, not the live "
            "frozen board report card — Official Play stays off."
        ),
    }
    path = report_dir() / ("fbs_historical_replay.json" if days > 7 else "fbs_last_week_replay.json")
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    return report


def _top_bucket_hit(frame: pd.DataFrame, col: str, frac: float) -> float | None:
    if frame.empty:
        return None
    n = max(1, int(round(len(frame) * frac)))
    top = frame.nlargest(n, col)
    return float(top["y"].mean())


def _one_pick_per_game(frame: pd.DataFrame, col: str) -> dict[str, Any]:
    """Desk-style: highest model chance in each game, then grade hit rate."""
    part = frame[np.isfinite(frame[col])].copy()
    if part.empty or "game_id" not in part.columns:
        return {"n_games": 0, "hits": 0, "hit_rate": None}
    idx = part.groupby("game_id")[col].idxmax()
    picks = part.loc[idx]
    hits = int((picks["y"] >= 1).sum())
    n = int(len(picks))
    return {
        "n_games": n,
        "hits": hits,
        "misses": n - hits,
        "hit_rate": (hits / n) if n else None,
        "mean_p": float(picks[col].mean()) if n else None,
        "base_rate_all_rows": float(part["y"].mean()) if len(part) else None,
    }


def _confidence_plain(blend: dict[str, Any] | None, one_per: dict[str, Any]) -> dict[str, Any]:
    """Plain-English confidence from historical practice — not a Play unlock."""
    if not blend or not blend.get("n"):
        return {
            "level": "none",
            "summary": "Not enough graded history to judge.",
            "ready_for_official_play": False,
        }
    n = int(blend["n"])
    brier = float(blend["brier"])
    games = int(one_per.get("n_games") or 0)
    hit = one_per.get("hit_rate")
    base = float(blend.get("base_rate") or 0)

    if n < 200 or games < 20:
        level = "thin"
    elif brier <= 0.18 and hit is not None and hit >= max(0.28, base + 0.05):
        level = "encouraging"
    elif brier <= 0.22:
        level = "okay"
    else:
        level = "weak"

    bits = [
        f"Graded {n} player-games across {games} games.",
        f"Blend Brier {brier:.3f} (lower is better; under ~0.22 is usable).",
    ]
    if hit is not None:
        bits.append(
            f"One top pick per game hit {one_per.get('hits')}/{games} "
            f"({hit:.0%}) — random skill players score ~{base:.0%} of the time."
        )
    bits.append("This is practice history. Official Play still needs live freeze→settle proof.")

    return {
        "level": level,
        "summary": " ".join(bits),
        "ready_for_official_play": False,
        "n": n,
        "games": games,
        "brier": brier,
        "one_pick_hit_rate": hit,
    }
