"""ATS prediction freeze + auto-grading. Parallel to prop shadow; does not modify ATD rows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from spe.db.base import as_utc, new_id, utcnow
from spe.db.models import AtsPrediction, AtsSettlement, Game
from spe.market.odds import american_to_decimal
from spe.models.ats.labels import grade_side
from spe.models.ats.predict import predict_upcoming_ats
from spe.models.ats.research_bundle import CLEAN_PROSPECTIVE_COHORT, PIPELINE_VERSION
from spe.models.ats.spread_contract import classify_spread_source, to_home_handicap


def capture_ats_predictions(session: Session, now: datetime | None = None) -> dict[str, Any]:
    """Freeze corrected research predictions before kickoff under the clean cohort version.

    - Does not rewrite legacy freezes.
    - One clean_prospective freeze per game (live book preferred).
    - Consensus quotes stored as consensus_research, excluded from clean ATS evaluation.
    - Rows without valid cover probs are not frozen.
    """
    now = now or datetime.now(UTC)
    payload = predict_upcoming_ats(session, now=now, include_simulation=False)
    written = unchanged = skipped = 0
    written_clean = written_consensus = 0

    existing = {
        (
            r.game_id,
            r.bookmaker_id,
            r.side,
            round(float(r.spread_line), 3),
            r.model_version_id,
            r.odds_tick_id,
        )
        for r in session.query(AtsPrediction).all()
    }
    # Unique-game gate for clean cohort: at most one clean freeze per game_id+cohort.
    clean_games_existing = {
        r.game_id
        for r in session.query(AtsPrediction).all()
        if (r.components or {}).get("cohort_version") == CLEAN_PROSPECTIVE_COHORT
        and (r.components or {}).get("eligibility") == "clean_prospective"
    }

    # Prefer live_book over consensus when both appear for the same game.
    ranked = sorted(
        payload.get("rows") or [],
        key=lambda r: 0 if r.get("quote_source_class") == "live_book" else 1,
    )

    for row in ranked:
        kickoff = as_utc(datetime.fromisoformat(str(row["kickoff_utc"]).replace("Z", "+00:00")))
        if kickoff is not None and kickoff <= now:
            skipped += 1
            continue
        if row.get("p_cover") is None or not row.get("cover_probs_enabled"):
            skipped += 1
            continue
        if row.get("home_handicap") is None and row.get("spread_line") is None:
            skipped += 1
            continue

        eligibility = row.get("eligibility") or "ineligible"
        if eligibility == "ineligible":
            skipped += 1
            continue

        if eligibility == "clean_prospective" and row["game_id"] in clean_games_existing:
            unchanged += 1
            continue

        side = row["favored_side"]
        book = row.get("sportsbook") or "consensus"
        handicap = row.get("home_handicap")
        if handicap is None:
            kind = classify_spread_source(str(book))
            if kind == "unknown":
                kind = (
                    "nflverse_margin_forecast"
                    if "consensus" in str(book).lower()
                    else "sportsbook_handicap"
                )
            handicap = to_home_handicap(float(row["spread_line"]), source_kind=kind)
        side_line = float(handicap) if side == "home" else float(-handicap)
        key = (row["game_id"], book, side, round(side_line, 3), row["model_version_id"], None)
        if key in existing:
            unchanged += 1
            continue

        quote_obs = now.isoformat()
        session.add(
            AtsPrediction(
                ats_id=new_id(),
                game_id=row["game_id"],
                league_id=row["league_id"],
                home_team_id=row["home_team_id"],
                away_team_id=row["away_team_id"],
                bookmaker_id=book,
                market_id="point_spread",
                side=side,
                spread_line=side_line,
                price_american=row.get("price_american"),
                odds_tick_id=None,
                predicted_margin=float(row["predicted_margin"]),
                predicted_home_score=float(row["predicted_home_score"]),
                predicted_away_score=float(row["predicted_away_score"]),
                p_cover=float(row["p_cover"]),
                p_push=float(row.get("p_push") or 0.0),
                margin_sigma=float(row["margin_sigma"]),
                model_vs_market=row.get("model_vs_market"),
                expected_value=row.get("expected_value"),
                model_version_id=row["model_version_id"],
                predicted_at=now,
                data_quality_status="research",
                confidence=float(row.get("confidence") or 0.5),
                warnings=list(row.get("warnings") or []),
                components={
                    "factors": row.get("factors") or {},
                    "p_home_cover": row.get("p_home_cover"),
                    "p_away_cover": row.get("p_away_cover"),
                    "home_handicap": float(handicap),
                    "spread_line_raw": row.get("spread_line"),
                    "spread_source_kind": row.get("spread_source_kind"),
                    "market_home_margin": row.get("market_home_margin"),
                    "spread_contract": "nflverse_L_v1",
                    "pipeline_version": row.get("pipeline_version") or PIPELINE_VERSION,
                    "cohort_version": row.get("cohort_version") or CLEAN_PROSPECTIVE_COHORT,
                    "eligibility": eligibility,
                    "quote_source_class": row.get("quote_source_class"),
                    "quote_observed_at": quote_obs,
                    "prediction_input_cutoff": now.isoformat(),
                    "research_bundle": row.get("research_bundle") or {},
                    "home_spread_price": row.get("home_spread_price"),
                    "away_spread_price": row.get("away_spread_price"),
                    "freshness": "pregame_capture",
                },
                research_only=True,
                play_enabled=False,
            )
        )
        existing.add(key)
        written += 1
        if eligibility == "clean_prospective":
            written_clean += 1
            clean_games_existing.add(row["game_id"])
        else:
            written_consensus += 1
    session.flush()
    return {
        "status": "ok",
        "written": written,
        "written_clean_prospective": written_clean,
        "written_consensus_research": written_consensus,
        "unchanged": unchanged,
        "skipped": skipped,
        "cohort_version": CLEAN_PROSPECTIVE_COHORT,
        "pipeline_version": PIPELINE_VERSION,
        "research_only": True,
        "play_enabled": False,
        "as_of": now.isoformat(),
        "research_bundles": payload.get("research_bundles"),
    }


def settle_ats_predictions(session: Session, now: datetime | None = None) -> dict[str, Any]:
    """Grade frozen ATS predictions after finals. Never modifies prediction rows."""
    now = now or datetime.now(UTC)
    settled = voided = skipped = 0
    settled_clean = 0
    open_rows = (
        session.query(AtsPrediction)
        .filter(~AtsPrediction.ats_id.in_(session.query(AtsSettlement.ats_id)))
        .all()
    )
    for pred in open_rows:
        game = session.get(Game, pred.game_id)
        if game is None:
            session.add(
                AtsSettlement(
                    settlement_id=new_id(),
                    ats_id=pred.ats_id,
                    result="void",
                    settled_at=now,
                    official_source="missing_game",
                    void_reason="game_row_missing",
                    evidence={},
                )
            )
            voided += 1
            continue
        if game.status != "final" or game.home_score is None or game.away_score is None:
            skipped += 1
            continue
        margin = float(game.home_score - game.away_score)
        comps = pred.components or {}
        if comps.get("home_handicap") is not None:
            home_h = float(comps["home_handicap"])
            grading_note = "components.home_handicap"
        elif comps.get("spread_contract") == "nflverse_L_v1" and comps.get("spread_line_raw") is not None:
            kind = comps.get("spread_source_kind") or classify_spread_source(pred.bookmaker_id)
            if kind == "unknown":
                kind = (
                    "nflverse_margin_forecast"
                    if pred.bookmaker_id and "consensus" in str(pred.bookmaker_id).lower()
                    else "sportsbook_handicap"
                )
            home_h = to_home_handicap(float(comps["spread_line_raw"]), source_kind=kind)
            grading_note = "converted_from_spread_line_raw"
        elif comps.get("home_spread_line") is not None:
            raw = float(comps["home_spread_line"])
            kind = classify_spread_source(pred.bookmaker_id)
            if kind == "unknown":
                kind = (
                    "nflverse_margin_forecast"
                    if pred.bookmaker_id and "consensus" in str(pred.bookmaker_id).lower()
                    else "sportsbook_handicap"
                )
            home_h = to_home_handicap(raw, source_kind=kind)
            grading_note = "legacy_home_spread_line_converted"
        else:
            home_h = float(pred.spread_line if pred.side == "home" else -pred.spread_line)
            grading_note = "derived_from_side_spread_line"
        result = grade_side(margin, home_h, pred.side)
        mapped = {"cover": "cover", "loss": "loss", "push": "push"}[result]
        stake_return = None
        if pred.price_american is not None:
            if mapped == "push":
                stake_return = 0.0
            elif mapped == "cover":
                stake_return = float(american_to_decimal(int(pred.price_american)) - 1.0)
            else:
                stake_return = -1.0
        session.add(
            AtsSettlement(
                settlement_id=new_id(),
                ats_id=pred.ats_id,
                result=mapped,
                home_score=int(game.home_score),
                away_score=int(game.away_score),
                actual_margin=margin,
                settled_at=now,
                official_source="games.final_score",
                evidence={
                    "home_handicap": home_h,
                    "grading_note": grading_note,
                    "side": pred.side,
                    "predicted_margin": pred.predicted_margin,
                    "p_cover": pred.p_cover,
                    "cohort_version": comps.get("cohort_version"),
                    "eligibility": comps.get("eligibility"),
                    "spread_contract_note": (
                        "Outcome grading uses corrected handicap orientation. "
                        "Legacy p_cover values remain as originally frozen."
                    ),
                },
                flat_stake_return=stake_return,
            )
        )
        settled += 1
        if comps.get("eligibility") == "clean_prospective":
            settled_clean += 1
    session.flush()
    return {
        "status": "ok",
        "settled": settled,
        "settled_clean_prospective": settled_clean,
        "voided": voided,
        "skipped_open": skipped,
        "as_of": now.isoformat(),
        "research_only": True,
        "play_enabled": False,
    }


def ats_track_record(session: Session, league_id: str | None = None) -> dict[str, Any]:
    q = session.query(AtsPrediction, AtsSettlement).join(
        AtsSettlement, AtsSettlement.ats_id == AtsPrediction.ats_id
    )
    if league_id:
        q = q.filter(AtsPrediction.league_id == league_id)
    rows = q.all()
    by_bucket: dict[str, Any] = {}
    for pred, settle in rows:
        comps = pred.components or {}
        eligibility = comps.get("eligibility") or "legacy_excluded"
        cohort = comps.get("cohort_version") or "legacy_pre_contract"
        key = f"{pred.league_id}|{cohort}|{eligibility}"
        bucket = by_bucket.setdefault(
            key,
            {
                "league_id": pred.league_id,
                "cohort_version": cohort,
                "eligibility": eligibility,
                "n": 0,
                "cover": 0,
                "loss": 0,
                "push": 0,
                "void": 0,
                "returns": [],
                "brier_terms": [],
                "included_in_clean_prospective_eval": eligibility == "clean_prospective",
            },
        )
        bucket["n"] += 1
        bucket[settle.result] = bucket.get(settle.result, 0) + 1
        if settle.flat_stake_return is not None and settle.result in {"cover", "loss"}:
            bucket["returns"].append(float(settle.flat_stake_return))
        if settle.result in {"cover", "loss"} and pred.p_cover is not None:
            y = 1.0 if settle.result == "cover" else 0.0
            bucket["brier_terms"].append((float(pred.p_cover) - y) ** 2)
    summary = {}
    for key, b in by_bucket.items():
        graded = b["cover"] + b["loss"]
        summary[key] = {
            "league_id": b["league_id"],
            "cohort_version": b["cohort_version"],
            "eligibility": b["eligibility"],
            "included_in_clean_prospective_eval": b["included_in_clean_prospective_eval"],
            "n": b["n"],
            "cover": b["cover"],
            "loss": b["loss"],
            "push": b["push"],
            "void": b["void"],
            "cover_rate": (b["cover"] / graded) if graded else None,
            "flat_stake_roi": (sum(b["returns"]) / len(b["returns"])) if b["returns"] else None,
            "brier": (sum(b["brier_terms"]) / len(b["brier_terms"])) if b["brier_terms"] else None,
            "research_only": True,
            "play_enabled": False,
        }
    return {
        "as_of": utcnow().isoformat(),
        "buckets": summary,
        "note": (
            "Clean prospective eval uses eligibility=clean_prospective only. "
            "Legacy freezes and consensus_research are excluded from that comparison. "
            "Prior 0.1568 claim is superseded."
        ),
        "superseded_claims": [
            {
                "claim": "NFL holdout cover Brier ≈ 0.1568",
                "status": "superseded",
                "reason": "inverted nflverse spread-sign",
            }
        ],
        "research_only": True,
        "play_enabled": False,
    }
