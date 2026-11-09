"""Orchestrate Anytime TD unification: features → serialize → forecast → prospective freeze.

Research-only. Never promotes. Never enables Validated Play. Never rebuilds the simulator.
Preserves protected model artifacts and historical forecasts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from spe.config import settings
from spe.db.base import as_utc
from spe.models.player_td.isolation import (
    assert_protected_unchanged,
    snapshot_protected_artifacts,
)
from spe.research.atd_features import (
    VERSION as FEATURES_VERSION,
    dump_feature_schema,
    verify_feature_reproducibility,
)
from spe.research.atd_forecast import (
    forecast_upcoming,
    freeze_prospective,
    research_dir,
)
from spe.research.m6_hist_gbm import (
    VERSION as M6_GBM_VERSION,
    load_m6_hist_gbm,
    served_m6_path,
    train_and_serialize,
)


def report_path() -> Path:
    return research_dir() / "unification_report.json"


def run_unification(
    session: Session,
    *,
    now: datetime | None = None,
    horizon_hours: int = 24 * 14,
    skip_serialize: bool = False,
    skip_forecast: bool = False,
    skip_prospective: bool = False,
    max_games_per_league: int = 3,
) -> dict[str, Any]:
    moment = as_utc(now) or datetime.now(UTC)
    before = snapshot_protected_artifacts()
    served_before = {
        lg: served_m6_path(lg).read_bytes() if served_m6_path(lg).exists() else None
        for lg in ("nfl", "fbs")
    }

    report: dict[str, Any] = {
        "status": "ok",
        "as_of": moment.isoformat(),
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "validated_play": False,
        "simulator_rebuilt": False,
        "1_unified_architecture": {
            "feature_pipeline": FEATURES_VERSION,
            "team_scoring": "team_td_v1",
            "nfl_primary": "player_td_v1",
            "nfl_ensemble_candidate": "ens_m6_gbm_player_td (optional, research)",
            "fbs_primary": "m5",
            "correlated_sim": "atd_sim_v1 (available, not auto-combined)",
            "m6_hist_gbm_artifact": M6_GBM_VERSION,
            "served_m6_shrink": "preserved (*_m6_player_td.json)",
        },
        "2_duplicate_code_consolidated": [],
        "3_serialized_model_verification": {},
        "4_nfl_research_forecast_examples": [],
        "5_fbs_research_forecast_examples": [],
        "6_remaining_bugs": [],
        "7_prospective_tracking_readiness": {},
        "8_recommended_next_milestone": [],
        "phases": {},
    }

    # --- Phase 1: feature unification + reproducibility ---
    print("[unify] Phase 1 — canonical opportunity features…", flush=True)
    feature_checks = {}
    for lg in ("nfl", "fbs"):
        feature_checks[lg] = verify_feature_reproducibility(session, lg)
        feature_checks[lg]["schema"] = dump_feature_schema(lg)
    report["phases"]["1_features"] = feature_checks
    report["2_duplicate_code_consolidated"] = [
        {
            "responsibility": "player_opportunity_features",
            "canonical": "spe.research.atd_features → spe.models.player_td.features",
            "consumers": ["M5 prepare_td_frame", "M6 study frame", "Player TD v1 dataset"],
            "removed_duplication": [
                "rolling_td.prepare_td_frame now calls build_canonical_opportunity_frame",
                "player_td.dataset.build_player_td_frame prefers canonical opportunity frame",
            ],
        },
        {
            "responsibility": "team_scoring",
            "canonical": "team_td_v1",
            "note": "unchanged; Player TD and sim continue to consume it",
        },
        {
            "responsibility": "player_td_probability",
            "canonical_nfl": "player_td_v1 (+ optional serialized m6_hist_gbm ensemble)",
            "canonical_fbs": "m5",
        },
        {
            "responsibility": "correlated_simulation",
            "canonical": "existing atd_sim_v1 — not rebuilt",
        },
    ]
    for lg, chk in feature_checks.items():
        if not chk.get("identical"):
            report["6_remaining_bugs"].append(f"{lg}_feature_fingerprint_mismatch")

    # --- Phase 2: serialize M6 HistGBM (NFL required; FBS optional research) ---
    print("[unify] Phase 2 — serialize M6 HistGBM…", flush=True)
    ser: dict[str, Any] = {}
    if not skip_serialize:
        for lg in ("nfl", "fbs"):
            print(f"[unify] serializing {lg} hist_gbm…", flush=True)
            ser[lg] = train_and_serialize(session, lg, now=moment)
    else:
        for lg in ("nfl", "fbs"):
            loaded = load_m6_hist_gbm(lg)
            ser[lg] = {
                "status": "skipped_serialize",
                "artifact_present": loaded is not None,
                "model_sha256": (loaded or {}).get("model_sha256"),
            }
    report["phases"]["2_serialize"] = ser
    report["3_serialized_model_verification"] = {
        lg: {
            "status": body.get("status"),
            "model_sha256": body.get("model_sha256"),
            "features": body.get("features"),
            "reload_verify": body.get("reload_verify") or body.get("reload_recheck"),
            "shadow_brier": body.get("shadow_brier"),
            "served_m6_untouched": body.get("served_m6_untouched", True),
            "meta_path": body.get("meta_path"),
            "model_path": body.get("model_path"),
        }
        for lg, body in ser.items()
    }
    for lg, body in ser.items():
        rv = body.get("reload_verify") or {}
        if body.get("status") == "ok" and not rv.get("ok", True):
            report["6_remaining_bugs"].append(f"{lg}_hist_gbm_reload_mismatch")

    # --- Phase 3–5: unified forecast + real-game inference ---
    print("[unify] Phase 3–5 — unified research forecasts…", flush=True)
    forecasts: dict[str, Any] = {}
    if not skip_forecast:
        forecasts = forecast_upcoming(
            session,
            horizon_hours=horizon_hours,
            now=moment,
            include_nfl_ensemble=True,
            max_games_per_league=max_games_per_league,
        )
        report["phases"]["3_5_forecast"] = {
            "status": forecasts.get("status"),
            "routing": forecasts.get("routing"),
            "nfl_games": (forecasts.get("leagues") or {}).get("nfl", {}).get("n_games"),
            "fbs_games": (forecasts.get("leagues") or {}).get("fbs", {}).get("n_games"),
            "n_rejected": len(forecasts.get("rejected") or []),
            "bugs": forecasts.get("bugs"),
            "forecast_path": forecasts.get("forecast_path"),
        }
        report["4_nfl_research_forecast_examples"] = (forecasts.get("examples") or {}).get("nfl") or []
        report["5_fbs_research_forecast_examples"] = (forecasts.get("examples") or {}).get("fbs") or []
        report["6_remaining_bugs"].extend(list(forecasts.get("bugs") or []))
        # Surface eligibility / rejection patterns without dumping every row.
        rejects = forecasts.get("rejected") or []
        if rejects:
            reasons: dict[str, int] = {}
            for r in rejects:
                reasons[str(r.get("reason"))] = reasons.get(str(r.get("reason")), 0) + 1
            report["phases"]["4_consistency"] = {
                "reject_policy": "omit missing features; reject when no usable M5 features or non-finite p",
                "n_rejected": len(rejects),
                "reject_reasons": reasons,
            }
    else:
        report["phases"]["3_5_forecast"] = {"status": "skipped"}

    # --- Phase 6: prospective freeze ---
    print("[unify] Phase 6 — prospective freeze…", flush=True)
    if not skip_prospective:
        freeze = freeze_prospective(
            session,
            horizon_hours=max(horizon_hours, 24 * 72),
            now=moment,
            include_nfl_ensemble=True,
        )
        report["phases"]["6_prospective"] = freeze
        report["7_prospective_tracking_readiness"] = freeze.get("readiness") or {}
        report["7_prospective_tracking_readiness"]["freeze_path"] = freeze.get("freeze_path")
        report["7_prospective_tracking_readiness"]["n_games"] = freeze.get("n_games")
        report["7_prospective_tracking_readiness"]["n_prospective_unexamined"] = freeze.get(
            "n_prospective_unexamined"
        )
    else:
        report["7_prospective_tracking_readiness"] = {"status": "skipped"}

    # Integrity checks
    assert_protected_unchanged(before)
    for lg in ("nfl", "fbs"):
        after = served_m6_path(lg).read_bytes() if served_m6_path(lg).exists() else None
        if after != served_before[lg]:
            raise RuntimeError(f"Served M6 shrink artifact changed for {lg}")
    report["protected_artifacts_unchanged"] = True
    report["served_m6_shrink_unchanged"] = True

    report["8_recommended_next_milestone"] = [
        "Settle prospective freezes on genuinely new games; compare vs M3 and sportsbook prices.",
        "Acquire receiver-vs-defender coverage and OL matchup features — only after the unified pipe is stable.",
        "If NFL ensemble continues to win prospectively, decide whether to serve serialized m6_hist_gbm (still research-gated).",
        "Do not promote; keep Validated Play disabled until prospective Brier/log-loss clear a pre-registered bar.",
    ]

    # Deduplicate bugs
    report["6_remaining_bugs"] = sorted(set(report["6_remaining_bugs"]))

    path = report_path()
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    print(f"[unify] wrote {path}", flush=True)
    return report
