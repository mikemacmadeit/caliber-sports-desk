"""Preserve the actual Desk selection before kickoff, including a decision to pass."""
import hashlib
import json
from datetime import UTC, datetime

from spe.db.base import as_utc, new_id
from sqlalchemy import event, func, select
from spe.db.models import DeskDecision, ShadowPrediction
from spe.market.odds import american_to_implied, ev_per_dollar
from spe.shadow.atd_desk_takes import desk_candidate_blockers, select_desk_takes


@event.listens_for(DeskDecision, "before_update")
@event.listens_for(DeskDecision, "before_delete")
def immutable(mapper, connection, target):
    raise ValueError("Desk decisions are append-only")


def latest_decisions(db, game_ids=None):
    stmt = select(DeskDecision.decision_id.label("id"), func.row_number().over(
        partition_by=DeskDecision.game_id,
        order_by=[DeskDecision.decided_at.desc(), DeskDecision.decision_id.desc()]).label("rn"))
    if game_ids is not None:
        stmt = stmt.where(DeskDecision.game_id.in_(game_ids))
    ranked = stmt.subquery()
    return {r.game_id: r for r in db.scalars(select(DeskDecision).join(
        ranked, DeskDecision.decision_id == ranked.c.id).where(ranked.c.rn == 1))}


def freeze_desk_rows(db, rows, now=None):
    now = as_utc(now) or datetime.now(UTC)
    atd = [r for r in rows if r.get("market_id") == "anytime_td"]
    chosen = {r["game_id"]: r for r in select_desk_takes(atd, now=now)}
    game_rows = {r["game_id"]: r for r in atd}
    records = latest_decisions(db, list(game_rows))
    capture_hybrid_comparisons(db, atd, now)
    saved = 0
    for gid, sample in game_rows.items():
        kick = as_utc(sample.get("kickoff_utc"))
        if not kick or kick <= now:
            continue
        row = chosen.get(gid)
        candidates = [{"player_id": r.get("player_id"), "outcome_id": r.get("outcome_id"),
            "blockers": sorted(set([*desk_candidate_blockers(r, now=now)[0],
                                    *(r.get("desk_analysis") or {}).get("blockers", [])]))}
            for r in atd if r["game_id"] == gid]
        candidates.sort(key=lambda r: str(r["outcome_id"]))
        payload = json.loads(json.dumps({"row": row, "called_at": now.isoformat(),
            "game_id": gid, "kickoff_utc": kick.isoformat(),
            "pass_reasons": candidates if row is None else []}, default=str))
        signature = hashlib.sha256(json.dumps({"player": row.get("player_id") if row else None,
            "p": (row.get("forecast") or {}).get("p_estimate") if row else None,
            "price": row.get("book_price") if row else None,
            "book": row.get("bookmaker") if row else None,
            "model": (row.get("forecast") or {}).get("model_version") if row else None,
            "analysis": (row.get("desk_analysis") or {}).get("features") if row else None,
            "availability": (row.get("forecast") or {}).get("availability_status") if row else None,
            "quote": row.get("price_freshness") if row else None,
            "risks": (row.get("desk_analysis") or {}).get("risks") if row else None,
            "pass_reasons": payload["pass_reasons"],
        }, sort_keys=True, default=str).encode()).hexdigest()
        record = records.get(gid)
        if record and (record.payload or {}).get("signature") == signature:
            continue
        payload["signature"] = signature
        if row and row.get("book_price") is not None and abs(row["book_price"]) >= 100:
            p = float(row["forecast"]["p_estimate"])
            price = int(row["book_price"])
            sid = new_id()
            db.add(ShadowPrediction(shadow_id=sid, player_id=row["player_id"], game_id=gid,
                league_id=row["league_id"], market_id="anytime_td", outcome_id=row["outcome_id"],
                bookmaker_id=row.get("bookmaker_id") or row.get("bookmaker") or "unknown", line=None, side="yes", price_american=price,
                forecast_probability=p, market_implied_raw=american_to_implied(price),
                expected_value=ev_per_dollar(p, price), model_version_id=row["forecast"]["model_version"],
                forecast_as_of=now, data_quality_status="research", abstain_reasons=["DESK_EXPERIMENTAL"],
                recommendation_status="no_bet", research_only=True, play_enabled=False, created_at=now,
                components={"desk_take": "game_scorer", "desk_take_kind": "game_scorer",
                    "desk_take_at": now.isoformat(), "desk_take_source": "desk_opportunity_v2",
                    "player_name": row["player_name"], "desk_analysis": row.get("desk_analysis"),
                    "quote_verified": bool((row.get("price_freshness") or {}).get("is_current_opportunity"))}))
            payload["shadow_id"] = sid
        db.add(DeskDecision(game_id=gid, payload=payload, decided_at=now))
        saved += 1
    db.flush()
    return saved


def current_desk_ids(db):
    return {r.payload["shadow_id"] for r in latest_decisions(db).values()
            if (r.payload or {}).get("shadow_id")}


def capture_hybrid_comparisons(db, rows, now):
    """Append matched pregame forecasts, including candidates neither model picks.

    The challenger cannot overwrite the displayed prediction or create a Desk
    selection. Settlement uses the existing ShadowPrediction lifecycle.
    """
    import math
    from spe.publishing.policy import assess
    candidates = [r for r in rows if (r.get("desk_analysis") or {}).get("hybrid")]
    if not candidates:
        return 0
    existing = db.query(ShadowPrediction).filter(
        ShadowPrediction.game_id.in_({r["game_id"] for r in candidates}),
        ShadowPrediction.model_version_id.like("desk_hybrid_v1-%")).all()
    signatures = {(p.components or {}).get("paired_signature") for p in existing}
    saved = 0
    for row in candidates:
        blockers = assess(row, now)["reasons"]
        if any(b in blockers for b in ("game_not_pregame", "price_not_verified_recently", "invalid_price", "missing_book_or_selection")):
            continue
        fc, analysis = row.get("forecast") or {}, row["desk_analysis"]
        hybrid = analysis["hybrid"]
        p, incumbent = hybrid.get("probability"), fc.get("p_estimate")
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in (p, incumbent)):
            continue
        quote = row.get("price_freshness") or fc.get("price_freshness")
        snapshot = {"outcome": row["outcome_id"], "game": row["game_id"],
            "kickoff_utc": row["kickoff_utc"], "position": row.get("position"),
            "incumbent_probability": incumbent, "incumbent_version": fc.get("model_version"),
            "hybrid": hybrid, "price": row["book_price"], "bookmaker": row.get("bookmaker_id") or row.get("bookmaker"),
            "quote": quote, "features": analysis.get("features"), "availability": fc.get("availability_status"),
            "common_blockers": [b for b in analysis.get("blockers", []) if b not in
                                {"insufficient_scoring_chance", "insufficient_price_advantage"}]}
        signature = hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()
        if signature in signatures:
            continue
        price = int(row["book_price"])
        db.add(ShadowPrediction(player_id=row["player_id"], game_id=row["game_id"], league_id=row["league_id"],
            market_id="anytime_td", outcome_id=row["outcome_id"], bookmaker_id=snapshot["bookmaker"],
            line=None, side="yes", price_american=price, forecast_probability=p,
            market_implied_raw=american_to_implied(price), expected_value=ev_per_dollar(p, price),
            model_version_id="desk_hybrid_v1-" + hybrid["artifact_sha256"][:16], forecast_as_of=now,
            data_quality_status="research", abstain_reasons=["HYBRID_NOT_PROMOTED"],
            recommendation_status="no_bet", research_only=True, play_enabled=False, created_at=now,
            components={"paired_signature": signature, "paired_snapshot": snapshot,
                        "player_name": row["player_name"], "comparison_only": True, "quote_verified": True}))
        signatures.add(signature)
        saved += 1
    return saved
