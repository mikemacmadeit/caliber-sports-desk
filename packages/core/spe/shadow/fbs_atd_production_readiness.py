"""CFB Anytime TD production-readiness gates (pre-registered).

Validated Play stays off until every hard gate passes. This module only measures
and reports — it does not flip Play by itself unless explicitly asked to apply.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.research.atd_verify import settled_performance
from spe.shadow.fbs_board_live_proof import board_live_proof_snapshot
from spe.shadow.prospective_collection import settled_unique_counts

# Pre-registered hard gates for CFB board blend Validated Play.
MIN_BLEND_SETTLED_PLAYER_GAMES = 200
MIN_BLEND_SETTLED_GAMES = 20
# Blend must be no worse than market Brier on priced settled rows when n>=50.
REQUIRE_BEAT_OR_TIE_MARKET_BRIER = True
MAX_BLEND_BRIER = 0.22
# Soft ops gates (warn, do not alone unlock Play).
MAX_STALE_SHARE_ON_BOARD = 0.85


def report_dir() -> Path:
    path = Path("data") / "atd_sim_dev" / "live_weekend"
    path.mkdir(parents=True, exist_ok=True)
    return path


def readiness_path() -> Path:
    return report_dir() / "fbs_atd_production_readiness.json"


def _blend_settled_counts(session: Session) -> dict[str, Any]:
    row = session.execute(
        text(
            """
            SELECT
              COUNT(*) AS n_rows,
              COUNT(DISTINCT s.player_id||'|'||s.game_id) AS player_games,
              COUNT(DISTINCT s.game_id) AS games,
              SUM(CASE WHEN st.result='win' THEN 1 ELSE 0 END) AS wins,
              SUM(CASE WHEN st.result='loss' THEN 1 ELSE 0 END) AS losses
            FROM shadow_predictions s
            JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
            JOIN games g ON g.game_id = s.game_id
            WHERE s.league_id = 'fbs'
              AND s.market_id = 'anytime_td'
              AND s.model_version_id LIKE 'blend_v1_c_recent%'
              AND s.forecast_as_of < g.kickoff_utc
              AND st.result IN ('win', 'loss')
            """
        )
    ).mappings().first()
    return {
        "n_rows": int((row or {}).get("n_rows") or 0),
        "player_games": int((row or {}).get("player_games") or 0),
        "games": int((row or {}).get("games") or 0),
        "wins": int((row or {}).get("wins") or 0),
        "losses": int((row or {}).get("losses") or 0),
    }


def _blend_metrics(session: Session) -> dict[str, Any]:
    rows = session.execute(
        text(
            """
            SELECT
              s.forecast_probability AS p,
              CASE WHEN st.result='win' THEN 1.0 ELSE 0.0 END AS y,
              s.market_implied_raw AS mkt
            FROM shadow_predictions s
            JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
            JOIN games g ON g.game_id = s.game_id
            WHERE s.league_id = 'fbs'
              AND s.market_id = 'anytime_td'
              AND s.model_version_id LIKE 'blend_v1_c_recent%'
              AND s.forecast_as_of < g.kickoff_utc
              AND st.result IN ('win', 'loss')
            """
        )
    ).mappings().all()
    if not rows:
        return {"n": 0, "brier": None, "market_brier": None, "log_loss": None, "hit_rate": None}
    import numpy as np

    y = np.asarray([float(r["y"]) for r in rows], dtype=float)
    p = np.clip(np.asarray([float(r["p"]) for r in rows], dtype=float), 1e-6, 1 - 1e-6)
    mkt_vals = [float(r["mkt"]) for r in rows if r["mkt"] is not None]
    brier = float(np.mean((p - y) ** 2))
    log_loss = float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
    market_brier = None
    if len(mkt_vals) == len(rows):
        m = np.clip(np.asarray(mkt_vals, dtype=float), 1e-6, 1 - 1e-6)
        market_brier = float(np.mean((m - y) ** 2))
    return {
        "n": int(len(rows)),
        "brier": brier,
        "market_brier": market_brier,
        "log_loss": log_loss,
        "hit_rate": float(y.mean()),
        "mean_p": float(p.mean()),
    }


def _open_freeze_health(session: Session) -> dict[str, Any]:
    snap = board_live_proof_snapshot(session)
    open_blend = next(
        (r for r in (snap.get("open_prekickoff_freezes") or []) if r.get("family") == "blend_v1_c_recent"),
        None,
    )
    return {
        "open_blend_rows": int((open_blend or {}).get("n") or 0),
        "open_blend_games": int((open_blend or {}).get("n_games") or 0),
        "snapshot": snap,
    }


def evaluate_production_readiness(session: Session) -> dict[str, Any]:
    now = datetime.now(UTC)
    blend_counts = _blend_settled_counts(session)
    blend_metrics = _blend_metrics(session)
    open_health = _open_freeze_health(session)
    unique = settled_unique_counts(session)
    perf = settled_performance(session)

    gates: list[dict[str, Any]] = []

    def gate(code: str, ok: bool, detail: str, *, hard: bool = True) -> None:
        gates.append({"code": code, "ok": bool(ok), "hard": hard, "detail": detail})

    gate(
        "blend_settled_player_games",
        blend_counts["player_games"] >= MIN_BLEND_SETTLED_PLAYER_GAMES,
        f"{blend_counts['player_games']}/{MIN_BLEND_SETTLED_PLAYER_GAMES} unique blend player-games settled",
    )
    gate(
        "blend_settled_games",
        blend_counts["games"] >= MIN_BLEND_SETTLED_GAMES,
        f"{blend_counts['games']}/{MIN_BLEND_SETTLED_GAMES} unique blend games settled",
    )
    brier_ok = (
        blend_metrics["brier"] is not None
        and blend_metrics["n"] >= 50
        and float(blend_metrics["brier"]) <= MAX_BLEND_BRIER
    )
    gate(
        "blend_brier_ceiling",
        brier_ok,
        (
            f"blend brier={blend_metrics['brier']} n={blend_metrics['n']} "
            f"(need n>=50 and brier<={MAX_BLEND_BRIER})"
        ),
    )
    if REQUIRE_BEAT_OR_TIE_MARKET_BRIER:
        mkt = blend_metrics.get("market_brier")
        beat = (
            blend_metrics["brier"] is not None
            and mkt is not None
            and blend_metrics["n"] >= 50
            and float(blend_metrics["brier"]) <= float(mkt) + 1e-9
        )
        gate(
            "blend_vs_market_brier",
            beat,
            f"blend brier={blend_metrics['brier']} vs market={mkt} (n={blend_metrics['n']})",
        )

    gate(
        "open_prekickoff_pipeline_alive",
        open_health["open_blend_rows"] > 0 or blend_counts["player_games"] > 0,
        f"open blend freezes={open_health['open_blend_rows']} across {open_health['open_blend_games']} games",
        hard=False,
    )

    hard_fail = [g for g in gates if g["hard"] and not g["ok"]]
    soft_fail = [g for g in gates if (not g["hard"]) and not g["ok"]]
    ready = len(hard_fail) == 0 and blend_counts["player_games"] > 0

    report = {
        "title": "CFB Anytime TD production readiness",
        "generated_at": now.isoformat(),
        "play_enabled": False,
        "validated_play": False,
        "production_ready": ready,
        "would_enable_validated_play": ready,
        "note": (
            "Validated Play remains off in serving code until an explicit unlock after gates pass. "
            "This report never silently promotes."
        ),
        "thresholds": {
            "min_blend_settled_player_games": MIN_BLEND_SETTLED_PLAYER_GAMES,
            "min_blend_settled_games": MIN_BLEND_SETTLED_GAMES,
            "max_blend_brier": MAX_BLEND_BRIER,
            "require_beat_or_tie_market_brier": REQUIRE_BEAT_OR_TIE_MARKET_BRIER,
        },
        "blend_settled": blend_counts,
        "blend_metrics": blend_metrics,
        "open_freeze_health": {
            "open_blend_rows": open_health["open_blend_rows"],
            "open_blend_games": open_health["open_blend_games"],
        },
        "legacy_unique_settled": unique,
        "settled_performance_families": perf,
        "gates": gates,
        "hard_failures": [g["code"] for g in hard_fail],
        "soft_failures": [g["code"] for g in soft_fail],
        "remaining": [
            g["detail"] for g in hard_fail
        ] or (["All hard gates clear — explicit unlock still required to flip Play"] if ready else []),
    }
    readiness_path().write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(readiness_path())
    return report


def run_fbs_atd_ops_cycle(
    session: Session,
    *,
    year: int | None = None,
    skip_legacy_candidates: bool = True,
    ingest_boxes: bool = True,
) -> dict[str, Any]:
    """Scoreboard → optional CFBD boxes → freeze/settle → readiness report."""
    from spe.ingest.cfbd_players import ingest_fbs_players
    from spe.ingest.espn_scoreboard import ingest_football_scoreboard
    from spe.shadow.fbs_board_live_proof import run_fbs_board_live_proof

    moment = datetime.now(UTC)
    year = year or moment.year
    out: dict[str, Any] = {
        "generated_at": moment.isoformat(),
        "play_enabled": False,
        "validated_play": False,
        "steps": {},
    }

    print("[ops] scoreboard…", flush=True)
    out["steps"]["scoreboard"] = ingest_football_scoreboard(session, year=year)
    session.commit()

    if ingest_boxes:
        print("[ops] CFBD player boxes / usage…", flush=True)
        try:
            out["steps"]["cfbd_players"] = ingest_fbs_players(session, seasons=(year,))
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            out["steps"]["cfbd_players"] = {"status": "error", "error": str(exc)}

    print("[ops] live proof freeze+settle…", flush=True)
    # Scoreboard already refreshed above.
    out["steps"]["live_proof"] = run_fbs_board_live_proof(
        session,
        also_capture_legacy_candidates=not skip_legacy_candidates,
        refresh_scoreboard=False,
    )
    session.commit()

    print("[ops] anytime TD desk takes…", flush=True)
    try:
        from spe.shadow.atd_desk_takes import capture_atd_desk_takes

        out["steps"]["desk_takes"] = capture_atd_desk_takes(session, league="all")
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        out["steps"]["desk_takes"] = {"status": "error", "error": str(exc)}

    print("[ops] production readiness…", flush=True)
    out["readiness"] = evaluate_production_readiness(session)
    path = report_dir() / "fbs_atd_ops_cycle_latest.json"
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    out["report_path"] = str(path)
    return out
