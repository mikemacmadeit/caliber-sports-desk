"""Version metadata for NEW Anytime TD freezes only.

Does not alter probabilities. Does not rewrite historical freezes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spe.research.m6_hist_gbm import VERSION as M6_HIST_VERSION
from spe.research.m6_hist_gbm import model_path as m6_hist_model_path

# Bump when scoring wiring or feature definitions change (not model weights).
SCORING_CODE_VERSION = "atd_score_wire_v2"
FEATURE_DEFINITION_VERSION = "atd_features_v2_current_season_priors"
CALIBRATION_FALLBACK_VERSION = "platt_or_none_v1"
OPPORTUNITY_LOOKBACK = "career_prior_avg_pre_kickoff_games"
TARGETS_DEFINITION = {
    "nfl": "actual_targets_when_present",
    "fbs": "receptions_used_as_proxy_not_actual_targets",
}

# Families that share underlying artifacts / are not independent evidence.
FAMILY_ALIASES = {
    "m6": {
        "artifact": "served_m6_player_td_shrink_json",
        "independent_evidence": True,
        "note": "Shrink-rate M6 served artifact",
    },
    "m6_histgbm": {
        "artifact": "m6_hist_gbm_v1_research_joblib",
        "independent_evidence": True,
        "note": "Research HistGBM — distinct from served m6 shrink",
        "not_alias_of": "m6",
    },
    "player_td_v1": {
        "artifact": "player_td_v1_team_lambda_x_shares",
        "independent_evidence": True,
    },
    "ens_m6_gbm_player_td": {
        "artifact": "equal_weight_player_td_v1_and_m6_hist_gbm",
        "independent_evidence": False,
        "composed_of": ["player_td_v1", "m6_histgbm"],
        "note": "Research ensemble — do not count as a third independent model",
    },
    "m3": {"artifact": "m3_anytime_td", "independent_evidence": True},
    "m4": {"artifact": "m4_anytime_td", "independent_evidence": True},
    "m5": {"artifact": "m5_anytime_td", "independent_evidence": True},
    "m5_c_recent": {
        "artifact": "m5_fbs_research_recent_form_v1_locked",
        "independent_evidence": True,
        "note": "Locked research variant: prior_td_rate:=recent8 when available. Paired vs production m5.",
        "not_alias_of": "m5",
    },
    "blend_v1_c_recent": {
        "artifact": "equal_weight_ish_player_td_v1_and_m5_c_recent_board",
        "independent_evidence": False,
        "composed_of": ["player_td_v1", "m5_c_recent"],
        "note": "CFB board primary (0.55 v1 + 0.45 C_recent). Not a third independent model.",
    },
    "atd_sim_v1": {"artifact": "correlated_simulator_v1", "independent_evidence": True},
}


def _sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def artifact_hash_for_family(family: str, league_id: str) -> str | None:
    if family in {"m6_histgbm", "m6_hist_gbm"}:
        return _sha256_file(m6_hist_model_path(league_id))
    # Best-effort hashes for JSON artifacts under models/
    roots = [
        Path("models") / "player_td" / f"{league_id}_m6_player_td.json",
        Path("models") / "rolling_td" / f"{league_id}_m5.json",
        Path("data") / "models" / f"{league_id}_m3_anytime_td.json",
    ]
    if family == "m6":
        return _sha256_file(Path("models") / "player_td" / f"{league_id}_m6_player_td.json")
    if family == "m5":
        for p in roots:
            if "m5" in p.name and p.exists():
                return _sha256_file(p)
    return None


def build_freeze_version_block(
    *,
    family: str,
    league_id: str,
    forecast_as_of: datetime,
    source_data_cutoff: datetime | None = None,
    price_captured_at: datetime | None = None,
    bookmaker_id: str | None = None,
    market_id: str = "anytime_td",
    missing_features: list[str] | None = None,
    proxy_flags: dict[str, Any] | None = None,
    opportunity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Metadata attached to NEW freeze components only."""
    fam_meta = FAMILY_ALIASES.get(family, {"independent_evidence": True})
    proxies = dict(proxy_flags or {})
    if league_id == "fbs":
        proxies.setdefault(
            "targets",
            {
                "is_proxy": True,
                "proxy_of": "receptions",
                "label": "fbs_receptions_as_targets_proxy",
                "definition": TARGETS_DEFINITION["fbs"],
            },
        )
    else:
        proxies.setdefault(
            "targets",
            {"is_proxy": False, "definition": TARGETS_DEFINITION["nfl"]},
        )

    opp = dict(opportunity or {})
    return {
        "pipeline_version": {
            "scoring_code_version": SCORING_CODE_VERSION,
            "feature_definition_version": FEATURE_DEFINITION_VERSION,
            "calibration_fallback_version": CALIBRATION_FALLBACK_VERSION,
            "m6_hist_gbm_version": M6_HIST_VERSION if family in {"m6_histgbm", "m6_hist_gbm", "ens_m6_gbm_player_td"} else None,
            "family": family,
            "family_meta": fam_meta,
            "artifact_sha256": artifact_hash_for_family(family, league_id),
            "prediction_timestamp": (forecast_as_of if forecast_as_of.tzinfo else forecast_as_of.replace(tzinfo=UTC)).isoformat(),
            "source_data_cutoff": (
                (source_data_cutoff if source_data_cutoff.tzinfo else source_data_cutoff.replace(tzinfo=UTC)).isoformat()
                if source_data_cutoff
                else None
            ),
            "pre_fix_vs_post_fix": "post_fix_wire_v2",
        },
        "price_provenance": {
            "market_id": market_id,
            "bookmaker_id": bookmaker_id,
            "quote_captured_at": (
                (price_captured_at if price_captured_at.tzinfo else price_captured_at.replace(tzinfo=UTC)).isoformat()
                if price_captured_at
                else None
            ),
            "eligibility": "pregame_tick_only",
            "novig": "yes_only_vigged_unless_two_way",
        },
        "opportunity_snapshot": {
            **opp,
            "lookback": OPPORTUNITY_LOOKBACK,
            "prior_carries_def": "avg carries in prior games before kickoff",
            "prior_targets_def": TARGETS_DEFINITION.get(league_id, TARGETS_DEFINITION["nfl"]),
            "team_td_share_def": "avg (player rush+rec TD / team rush+rec TD) over prior games",
            "fbs_receptions_not_actual_targets": league_id == "fbs",
        },
        "missing_features": list(missing_features or []),
        "proxy_flags": proxies,
    }
