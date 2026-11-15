"""Preserve the actual Desk selection before kickoff, including a decision to pass."""
import hashlib
import json
from datetime import UTC, datetime

from spe.db.base import as_utc, new_id
from sqlalchemy import func, select
from spe.db.models import DeskDecision, ShadowPrediction
from spe.market.odds import american_to_implied, ev_per_dollar
from spe.shadow.atd_desk_takes import select_desk_takes

def latest_decisions(db, game_ids=None):
    stmt = select(DeskDecision.decision_id.label("id"), func.row_number().over(
        partition_by=DeskDecision.game_id,
        order_by=[DeskDecision.decided_at.desc(), DeskDecision.decision_id.desc()]).label("rn"))
    if game_ids is not None:
        stmt = stmt.where(DeskDecision.game_id.in_(game_ids))
    ranked = stmt.subquery()
    return {r.game_id: r for r in db.scalars(select(DeskDecision).join(
        ranked, DeskDecision.decision_id == ranked.c.id).where(ranked.c.rn == 1))}


def freeze_desk_rows(db, rows, now=None, *, include_started=False):
    now = as_utc(now) or datetime.now(UTC)
    atd = [r for r in rows if r.get("market_id") == "anytime_td"]
    chosen = {r["game_id"]: r for r in select_desk_takes(atd)}
    game_rows = {r["game_id"]: r for r in atd}
    records = latest_decisions(db, list(game_rows))
    saved = 0
    for gid, sample in game_rows.items():
        kick = as_utc(sample.get("kickoff_utc"))
        if not kick or (kick <= now and not include_started):
            continue
        row = chosen.get(gid)
        comparisons = {}
        combined_comparisons = {}
        for item in atd:
            analysis = item.get("desk_analysis") or {}
            if item["game_id"] == gid and analysis.get("combination_status") == "active":
                combined_comparisons[item["player_id"]] = {
                    "player_id": item["player_id"], "components": analysis["combination"],
                    "probability": (item.get("forecast") or {}).get("p_estimate"),
                    "model": (item.get("forecast") or {}).get("model_version"),
                    "price": item.get("book_price"), "book": item.get("bookmaker"),
                    "price_freshness": item.get("price_freshness"),
                    "eligible": analysis.get("eligible", False), "blockers": analysis.get("blockers", []),
                }
            if item["game_id"] != gid or analysis.get("nflverse_research_probability") is None:
                continue
            # One comparable player probability, independent of how many books list it.
            comparisons[item["player_id"]] = {"player_id": item["player_id"],
                "probability": analysis["nflverse_research_probability"],
                "primary_probability": (item.get("forecast") or {}).get("p_estimate"),
                "model_sha256": analysis.get("nflverse_research_model"),
                "eligible": analysis.get("eligible", False), "blockers": analysis.get("blockers", []),
                "status": "shadow_only"}
        payload = json.loads(json.dumps({"row": row, "called_at": now.isoformat(),
            "game_id": gid, "kickoff_utc": kick.isoformat(),
            "combined_comparison": sorted(combined_comparisons.values(), key=lambda x: x["player_id"]),
            "nflverse_comparison": sorted(comparisons.values(), key=lambda x: x["player_id"])}, default=str))
        signature = hashlib.sha256(json.dumps({"player": row.get("player_id") if row else None,
            "p": (row.get("forecast") or {}).get("p_estimate") if row else None,
            "price": row.get("book_price") if row else None,
            "book": row.get("bookmaker") if row else None,
            "model": (row.get("forecast") or {}).get("model_version") if row else None,
            "selection_version": row.get("selection_version") if row else None,
            "analysis": (row.get("desk_analysis") or {}).get("features") if row else None,
            "comparison_model": (row.get("desk_analysis") or {}).get("nflverse_research_model") if row else None,
            "comparison_probabilities": payload["nflverse_comparison"],
            "combined_comparison": payload["combined_comparison"],
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
                    "selection_version": row.get("selection_version"),
                    "desk_take_at": now.isoformat(), "desk_take_source": (row.get("desk_analysis") or {}).get("version", "desk_opportunity_v2"),
                    "player_name": row["player_name"], "desk_analysis": row.get("desk_analysis"),
                    "quote_verified": bool((row.get("price_freshness") or {}).get("is_current_opportunity"))}))
            payload["shadow_id"] = sid
        db.add(DeskDecision(game_id=gid, payload=payload, decided_at=now))
        saved += 1
    db.flush()
    return saved


def grade_desk_scorer(settlement: str | None, actual: float | None) -> str:
    """A recorded box grades the scorer. An unfinished box stays pending."""
    if settlement in {"win", "loss", "push"}:
        return settlement
    if actual is None:
        return "pending"
    return "win" if float(actual) > 0 else "loss"


def chosen_desk_predictions(db, games):
    """The one pre-kickoff scorer still standing for each game."""
    from spe.shadow.atd_desk_takes import is_desk_take

    if not games:
        return []
    game_ids = [game.game_id for game in games]
    decisions = latest_decisions(db, game_ids)
    preds = db.scalars(
        select(ShadowPrediction).where(
            ShadowPrediction.game_id.in_(game_ids),
            ShadowPrediction.market_id == "anytime_td",
        )
    )
    chosen = {}
    for pred in preds:
        if not is_desk_take(pred):
            continue
        decision = decisions.get(pred.game_id)
        if decision and (decision.payload or {}).get("shadow_id") != pred.shadow_id:
            continue
        prior = chosen.get(pred.game_id)
        taken = str((pred.components or {}).get("desk_take_at") or "")
        if prior is None or taken >= str((prior.components or {}).get("desk_take_at") or ""):
            chosen[pred.game_id] = pred
    return list(chosen.values())


def current_desk_ids(db):
    return {r.payload["shadow_id"] for r in latest_decisions(db).values()
            if (r.payload or {}).get("shadow_id")}
