"""Versioned ATS research configuration. Does not promote or overwrite production artifacts.

Live research paths (API Finder, capture) MUST load through ``load_ats_research_bundle``.
Production ``model_{league}.json`` ridge weights stay protected; legacy Platt inside that
file is never applied under the corrected spread contract.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spe.config import settings
from spe.models.ats.model import AtsModelArtifact, artifact_platt_is_valid
from spe.models.ats.spread_contract import CURRENT_SPREAD_CONTRACT, LEGACY_SPREAD_CONTRACT

# Outcome-independent cohort tag for new pregame freezes.
CLEAN_PROSPECTIVE_COHORT = "ats_clean_prospective_v1"
PIPELINE_VERSION = "ats-research-pipeline-nflverse_L_v1"
TRAINING_CUTOFF_UTC = "2024-09-01T00:00:00+00:00"
HOLDOUT_LABEL = "examined_research_holdout"
SCORING_CODE_VERSION = "ats_score_predictions_v2_handicap"
FEATURE_SCHEMA_VERSION = "ats_FEATURE_COLS_v1"
SUPERSEDED_CLAIM = {
    "claim": "NFL holdout cover Brier ≈ 0.1568 vs market ≈ 0.246",
    "status": "superseded",
    "reason": "Computed under inverted nflverse spread-sign convention",
    "replacement": "data/research/ats_audit/spread_contract_recompute.json",
    "corrected_brier_approx": 0.2505,
}


@dataclass
class AtsResearchBundle:
    league_id: str
    pipeline_version: str
    cohort_version: str
    model_version_id: str
    margin_artifact_path: str
    margin_artifact_sha256: str
    margin_model_version_id: str
    calibrator_path: str | None
    calibrator_sha256: str | None
    calibrator_status: str  # valid_research | missing | legacy_invalid_suppressed
    training_cutoff_utc: str
    spread_contract: str
    feature_schema_version: str
    scoring_code_version: str
    cover_probs_enabled: bool
    play_enabled: bool = False
    research_only: bool = True
    holdout_label: str = HOLDOUT_LABEL
    superseded_claims: list[dict[str, Any]] = field(default_factory=lambda: [SUPERSEDED_CLAIM])
    loaded_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def production_margin_path(league_id: str) -> Path:
    return Path(settings.parquet_dir) / "models" / "ats" / f"model_{league_id}.json"


def research_calibrator_path(league_id: str) -> Path:
    """First-class research calibrator (never written over production model_*.json)."""
    return Path(settings.parquet_dir) / "models" / "ats" / f"research_calibrator_{league_id}.json"


def research_bundle_manifest_path(league_id: str) -> Path:
    return Path(settings.parquet_dir) / "models" / "ats" / f"research_bundle_{league_id}.json"


def audit_research_calibrator_fallback(league_id: str) -> Path:
    # parquet_dir is typically ./data/parquet → sibling research/ats_audit
    return (
        Path(settings.parquet_dir).resolve().parent
        / "research"
        / "ats_audit"
        / f"model_{league_id}_spread_contract_research.json"
    )


def ensure_research_calibrator_installed(league_id: str) -> Path | None:
    """Copy audit research calibrator into parquet models/ if missing. Never touches model_*.json."""
    dest = research_calibrator_path(league_id)
    if dest.exists():
        return dest
    src = audit_research_calibrator_fallback(league_id)
    if not src.exists():
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


def load_ats_research_bundle(league_id: str) -> tuple[AtsModelArtifact | None, AtsResearchBundle]:
    """Load protected margin weights + corrected research calibrator for live research paths."""
    notes: list[str] = []
    margin_path = production_margin_path(league_id)
    if not margin_path.exists():
        bundle = AtsResearchBundle(
            league_id=league_id,
            pipeline_version=PIPELINE_VERSION,
            cohort_version=CLEAN_PROSPECTIVE_COHORT,
            model_version_id=f"missing-margin-{league_id}",
            margin_artifact_path=str(margin_path),
            margin_artifact_sha256="",
            margin_model_version_id="",
            calibrator_path=None,
            calibrator_sha256=None,
            calibrator_status="missing",
            training_cutoff_utc=TRAINING_CUTOFF_UTC,
            spread_contract=CURRENT_SPREAD_CONTRACT,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            scoring_code_version=SCORING_CODE_VERSION,
            cover_probs_enabled=False,
            notes=["Production margin artifact missing"],
        )
        return None, bundle

    margin_raw = json.loads(margin_path.read_text(encoding="utf-8"))
    margin_sha = _sha256(margin_path)
    artifact = AtsModelArtifact.from_dict(margin_raw)
    # Explicitly strip legacy Platt from the in-memory scoring object until a valid
    # research calibrator is layered on. Production file on disk is untouched.
    legacy_a, legacy_b = artifact.platt_a, artifact.platt_b
    artifact.platt_a = 0.0
    artifact.platt_b = 1.0
    artifact.metrics = {
        **(artifact.metrics or {}),
        "spread_contract": LEGACY_SPREAD_CONTRACT,
        "legacy_platt_a_on_disk": legacy_a,
        "legacy_platt_b_on_disk": legacy_b,
        "legacy_platt_suppressed": True,
    }
    notes.append("Loaded production ridge margin weights; legacy on-disk Platt suppressed in memory")

    cal_path = ensure_research_calibrator_installed(league_id)
    cal_status = "missing"
    cal_sha = None
    cover_ok = False
    if cal_path is not None and cal_path.exists():
        cal_raw = json.loads(cal_path.read_text(encoding="utf-8"))
        cal_sha = _sha256(cal_path)
        # Overlay only Platt + contract metadata; ridge coefs stay from production file.
        cal_metrics = cal_raw.get("metrics") or {}
        margin_cols = list(margin_raw.get("feature_cols") or [])
        cal_cols = list(cal_raw.get("feature_cols") or [])
        if cal_cols and margin_cols and cal_cols != margin_cols:
            notes.append(
                "Research calibrator was fit on a different feature list; "
                "its Platt scaling is not applied to this margin model"
            )
        elif cal_metrics.get("spread_contract") == CURRENT_SPREAD_CONTRACT:
            artifact.platt_a = float(cal_raw.get("platt_a", 0.0))
            artifact.platt_b = float(cal_raw.get("platt_b", 1.0))
            artifact.metrics = {
                **(artifact.metrics or {}),
                **cal_metrics,
                "spread_contract": CURRENT_SPREAD_CONTRACT,
                "calibrator_source": str(cal_path),
                "margin_source": str(margin_path),
                "margin_sha256": margin_sha,
                "calibrator_sha256": cal_sha,
            }
            if artifact_platt_is_valid(artifact):
                cal_status = "valid_research"
                cover_ok = True
                notes.append("Applied train-only research Platt under nflverse_L_v1")
            else:
                cal_status = "legacy_invalid_suppressed"
                artifact.platt_a, artifact.platt_b = 0.0, 1.0
                notes.append("Research calibrator present but failed contract gate; cover probs suppressed")
        else:
            cal_status = "legacy_invalid_suppressed"
            notes.append("Calibrator lacks spread_contract=nflverse_L_v1; cover probs suppressed")
    else:
        notes.append("No research calibrator installed; cover probs suppressed")

    # Version id for new freezes: production margin id + cohort + contract.
    model_version_id = (
        f"{artifact.model_version_id or f'ats-ridge-{league_id}'}:"
        f"{CLEAN_PROSPECTIVE_COHORT}:{CURRENT_SPREAD_CONTRACT}"
    )
    artifact.model_version_id = model_version_id

    bundle = AtsResearchBundle(
        league_id=league_id,
        pipeline_version=PIPELINE_VERSION,
        cohort_version=CLEAN_PROSPECTIVE_COHORT,
        model_version_id=model_version_id,
        margin_artifact_path=str(margin_path),
        margin_artifact_sha256=margin_sha,
        margin_model_version_id=str(margin_raw.get("model_version_id") or ""),
        calibrator_path=str(cal_path) if cal_path else None,
        calibrator_sha256=cal_sha,
        calibrator_status=cal_status,
        training_cutoff_utc=TRAINING_CUTOFF_UTC,
        spread_contract=CURRENT_SPREAD_CONTRACT,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        scoring_code_version=SCORING_CODE_VERSION,
        cover_probs_enabled=cover_ok,
        notes=notes,
    )
    # Persist manifest for audit (does not overwrite model_*.json).
    manifest = research_bundle_manifest_path(league_id)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(bundle.to_dict(), indent=2), encoding="utf-8")
    return artifact, bundle
