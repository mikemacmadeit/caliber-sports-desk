"""Append-only eligibility audit for existing ATS freezes under spread-sign contract.

Does NOT rewrite AtsPrediction rows. Records defects for exclusion from the clean
prospective ATS comparison.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "research" / "ats_audit"
ELIG_PATH = OUT / "ats_freeze_eligibility.jsonl"
SUMMARY_PATH = OUT / "ats_freeze_eligibility_summary.json"


def main() -> None:
    import os
    import sys

    sys.path.insert(0, str(ROOT / "packages" / "core"))
    os.chdir(ROOT)

    from spe.db.models import AtsPrediction, AtsSettlement, Game
    from spe.db.session import session_factory
    from spe.models.ats.spread_contract import classify_spread_source, to_home_handicap
    from spe.models.ats.labels import normal_cover_probs

    OUT.mkdir(parents=True, exist_ok=True)
    records = []
    with session_factory() as session:
        preds = session.query(AtsPrediction).order_by(AtsPrediction.predicted_at).all()
        settlements = {s.ats_id: s for s in session.query(AtsSettlement).all()}
        for pred in preds:
            comps = pred.components or {}
            contract = comps.get("spread_contract")
            book = pred.bookmaker_id or ""
            kind = classify_spread_source(book)
            if kind == "unknown":
                kind = (
                    "nflverse_margin_forecast"
                    if "consensus" in str(book).lower()
                    else "sportsbook_handicap"
                )

            defects = []
            eligible = True
            reasons = []

            if contract != "nflverse_L_v1":
                defects.append("missing_spread_contract_tag")
                eligible = False
                reasons.append(
                    "Frozen before nflverse L contract fix; p_cover / side selection used inverted orientation."
                )

            # Reconstruct what home line the freeze believed.
            raw_home = comps.get("home_spread_line")
            if raw_home is None and comps.get("spread_line_raw") is not None:
                raw_home = comps.get("spread_line_raw")
            if raw_home is None:
                raw_home = pred.spread_line if pred.side == "home" else -pred.spread_line

            try:
                correct_h = to_home_handicap(float(raw_home), source_kind=kind) if contract != "nflverse_L_v1" else float(
                    comps.get("home_handicap")
                    if comps.get("home_handicap") is not None
                    else to_home_handicap(float(comps.get("spread_line_raw", raw_home)), source_kind=kind)
                )
            except Exception:  # noqa: BLE001
                correct_h = None
                defects.append("cannot_resolve_home_handicap")
                eligible = False

            # If legacy freeze stored L in home_spread_line and graded as h, the implied
            # handicap used at freeze time was wrongly raw_home itself for consensus.
            if contract != "nflverse_L_v1" and kind == "nflverse_margin_forecast" and raw_home is not None:
                wrong_h_used = float(raw_home)  # treated L as h
                if correct_h is not None and abs(wrong_h_used - correct_h) > 1e-9:
                    defects.append("consensus_L_treated_as_handicap")
                    eligible = False

            # Probability sanity: recompute raw p under correct h; large divergence ⇒ defect.
            p_recomputed = None
            if correct_h is not None:
                probs = normal_cover_probs(float(pred.predicted_margin), float(correct_h), float(pred.margin_sigma))
                p_side = probs["p_home_cover"] if pred.side == "home" else probs["p_away_cover"]
                p_recomputed = p_side
                if abs(p_side - float(pred.p_cover)) > 0.05:
                    defects.append("p_cover_diverges_from_corrected_orientation")
                    eligible = False
                    reasons.append(
                        f"|p_cover - p_recomputed|={abs(p_side - float(pred.p_cover)):.3f} > 0.05"
                    )

            game = session.get(Game, pred.game_id)
            kickoff = game.kickoff_utc.isoformat() if game and game.kickoff_utc else None
            settled = settlements.get(pred.ats_id)

            rec = {
                "ts": datetime.now(UTC).isoformat(),
                "ats_id": pred.ats_id,
                "game_id": pred.game_id,
                "league_id": pred.league_id,
                "bookmaker_id": pred.bookmaker_id,
                "side": pred.side,
                "spread_line_stored": pred.spread_line,
                "predicted_at": pred.predicted_at.isoformat() if pred.predicted_at else None,
                "kickoff_utc": kickoff,
                "model_version_id": pred.model_version_id,
                "p_cover_frozen": pred.p_cover,
                "p_cover_recomputed_corrected": p_recomputed,
                "home_handicap_corrected": correct_h,
                "spread_source_kind": kind,
                "components_spread_contract": contract,
                "defects": defects,
                "eligible_for_clean_prospective_ats": eligible,
                "reasons": reasons,
                "settlement_result": settled.result if settled else None,
                "research_only": True,
                "play_enabled": False,
                "original_prediction_preserved": True,
                "note": (
                    "Append-only eligibility. Do not silently repair frozen probabilities. "
                    "Corrected replacement forecasts only before kickoff under a new version."
                ),
            }
            records.append(rec)

    with ELIG_PATH.open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, default=str) + "\n")

    eligible_n = sum(1 for r in records if r["eligible_for_clean_prospective_ats"])
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "n_freezes_audited": len(records),
        "n_eligible_clean_prospective": eligible_n,
        "n_excluded": len(records) - eligible_n,
        "eligible_ats_ids": [r["ats_id"] for r in records if r["eligible_for_clean_prospective_ats"]],
        "excluded_ats_ids": [r["ats_id"] for r in records if not r["eligible_for_clean_prospective_ats"]],
        "defect_counts": {},
        "append_only_path": str(ELIG_PATH),
        "original_freezes_rewritten": False,
        "validated_play": False,
        "atd_untouched": True,
    }
    for r in records:
        for d in r["defects"]:
            summary["defect_counts"][d] = summary["defect_counts"].get(d, 0) + 1

    SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
