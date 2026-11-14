"""Price-aware research cards built only from completed worker snapshots."""
from collections import Counter
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from spe.db.base import as_utc
from spe.market.odds import american_to_decimal, american_to_implied, ev_per_dollar
from spe.publishing.policy import annotate, finite
from spe.recommend.engine import american_hold_factor

LABELS = {"anytime_td": "Anytime TD", "receptions": "Receptions", "receiving_yards": "Receiving yards", "rushing_yards": "Rushing yards"}
REASONS = {
    "price_not_verified_recently": "The price needs a fresh sportsbook check.",
    "availability_unconfirmed": "Player availability is not confirmed.",
    "insufficient_reliable_inputs": "Injury, identity, or model coverage is incomplete.",
    "insufficient_value": "The estimated value does not clear the uncertainty allowance.",
    "missing_or_future_model_provenance": "The model's data timing cannot be verified.",
    "inconsistent_value": "The price and probability do not support a reliable value estimate.",
    "game_not_pregame": "This game has started or is no longer scheduled.",
    "invalid_price": "A valid sportsbook price is missing.",
    "missing_book_or_selection": "The sportsbook or selection is not identified.",
    "invalid_probability": "No usable probability estimate is available.",
    "demo_data": "This is demonstration data.",
    "unsupported_market_or_side": "This market is not supported for qualification.",
}


def selection_key(row):
    value = [row.get(k) for k in ("game_id", "player_id", "market_id", "side", "line")]
    return sha256(repr(value).encode()).hexdigest()[:24]


def with_offer(row, offer):
    """A different book needs its own EV; never reuse the best book's value."""
    out = {**row, **offer, "forecast": dict(row.get("forecast") or {})}
    f = out["forecast"]
    price, p = finite(out.get("book_price")), finite(f.get("p_estimate"))
    if price is not None and abs(price) >= 100 and p is not None:
        ci = f.get("confidence_interval") or [p, p]
        width = max(0, float(ci[1]) - float(ci[0]))
        # Preserve any stricter model-specific allowance already present.
        old_price = finite(row.get("book_price"))
        old_edge = finite((row.get("forecast") or {}).get("ev_after_haircut"))
        allowance = .5 * width * american_hold_factor(int(price))
        if old_price is not None and abs(old_price) >= 100 and old_edge is not None:
            conservative = (1 + old_edge) / american_to_decimal(int(old_price))
            allowance = max(allowance, (p - conservative) * american_to_decimal(int(price)))
        f.update(book_price=int(price), price_freshness=out.get("price_freshness"),
                 ev=ev_per_dollar(p, int(price)), ev_after_haircut=ev_per_dollar(p, int(price)) - allowance)
    return out


def changes_for(row, old, now):
    """Keep the latest material change until replaced, including book identity."""
    if not old:
        return []
    before = old.get("forecast") or {}
    after = row.get("forecast") or {}
    changes = []
    fields = [("probability", before.get("p_estimate"), after.get("p_estimate")),
              ("availability", before.get("availability_status"), after.get("availability_status"))]
    if old.get("bookmaker") == row.get("bookmaker"):
        fields.append(("price", old.get("book_price"), row.get("book_price")))
    for kind, previous, current in fields:
        if previous is None or current is None or previous == current:
            continue
        if kind == "probability" and abs(float(current) - float(previous)) < .005:
            continue
        changes.append({"kind": kind, "before": previous, "after": current,
                        "at": now.isoformat(), "bookmaker": row.get("bookmaker") if kind == "price" else None})
    if changes:
        return changes
    return [c for c in old.get("changes", []) if as_utc(c.get("at")) and as_utc(c["at"]) > now - timedelta(hours=24)]


def card(row, now):
    row = annotate(row, now)
    f, q = row["forecast"], row["qualification"]
    p, price = finite(f.get("p_estimate")), finite(row.get("book_price"))
    facts = []
    projection = finite(row.get("projection"))
    if projection is not None:
        facts.append({"label": "Projection", "value": f"{projection:.1f} {LABELS.get(row['market_id'], '').lower()}",
                      "source": row.get("projection_source") or "Research model"})
    usage = row.get("usage") or {}
    for key, label in (("carries", "Carries / game"), ("targets", "Targets / game"), ("rz_targets", "Red-zone targets / game"), ("gl_carries", "Goal-line carries / game")):
        value = finite(usage.get(key))
        if value is not None:
            facts.append({"label": label, "value": f"{value:.1f}", "source": f"Last {usage.get('games', 0)} recorded games before kickoff"})
    matchup = f.get("matchup") or {}
    factor = finite(matchup.get("factor"))
    if factor is not None:
        facts.append({"label": "Matchup adjustment", "value": f"{(factor - 1) * 100:+.0f}%", "source": f"Model {matchup.get('kind', '')} adjustment; not an observed result"})
    risks = [REASONS.get(reason, reason.replace("_", " ").capitalize()) for reason in q["reasons"]]
    if not facts:
        risks.append("Detailed usage evidence is not available for this selection.")
    risks.append("This model is experimental; its probability is not a proven betting edge.")
    pick = LABELS[row["market_id"]] if row["market_id"] == "anytime_td" else f"{str(row.get('side', '')).title()} {row.get('line')} {LABELS[row['market_id']].lower()}"
    offers = []
    for offer in row.get("offers") or []:
        checked = annotate(with_offer(row, offer), now)["qualification"]
        offers.append({"book": offer.get("bookmaker"), "price": offer.get("book_price"),
                       "verified_at": checked["verified_at"], "valid_until": checked["valid_until"],
                       "current": checked["age_minutes"] is not None and "price_not_verified_recently" not in checked["reasons"]})
    return {
        "id": selection_key(row), "outcome_id": row.get("outcome_id"), "game_id": row["game_id"],
        "league": row["league_id"], "player": row.get("player_name") or "Player", "team": row.get("team"),
        "opponent": row.get("opponent"), "matchup": f"{row.get('away_abbr', '?')} at {row.get('home_abbr', '?')}",
        "kickoff": row["kickoff_utc"], "market": row["market_id"], "pick": pick,
        "book": row.get("bookmaker"), "price": int(price) if price is not None else None,
        "probability": p, "break_even": american_to_implied(int(price)) if price is not None and abs(price) >= 100 else None,
        "conservative_ev": finite(f.get("ev_after_haircut")), "qualification": q,
        "availability": f.get("availability_status") or "unknown", "model": f.get("model_version"),
        "prediction_at": f.get("prediction_as_of"), "data_cutoff": f.get("data_cutoff"),
        "evidence": facts[:5], "risks": risks,
        "offers": offers,
        "changes": [c for c in row.get("changes", []) if (not c.get("bookmaker") or c["bookmaker"] == row.get("bookmaker")) and as_utc(c.get("at")) and now - timedelta(hours=24) < as_utc(c["at"]) <= now],
    }


def build(rows, *, now=None, book=None, market=None, search=None, saved=None, offset=0, limit=30):
    now = as_utc(now) or datetime.now(UTC)
    available_books, cards = set(), []
    for original in rows:
        if original.get("market_id") not in LABELS or original.get("data_tier") == "demo":
            continue
        kick = as_utc(original.get("kickoff_utc"))
        if not kick or kick <= now or original.get("game_status") in {"final", "in_progress", "canceled", "cancelled", "postponed"}:
            continue
        offers = original.get("offers") or [{k: original.get(k) for k in ("bookmaker", "book_price", "price_freshness", "source_timestamp")}]
        available_books.update(o["bookmaker"] for o in offers if o.get("bookmaker"))
        matching = [o for o in offers if not book or o.get("bookmaker") == book]
        if not matching or (market and original.get("market_id") != market):
            continue
        if saved is not None and selection_key(original) not in saved:
            continue
        if search and search.casefold() not in " ".join(str(original.get(k) or "") for k in ("player_name", "team", "opponent", "away_abbr", "home_abbr")).casefold():
            continue
        candidates = [annotate(with_offer(original, o), now) for o in matching]
        selected = max(candidates, key=lambda r: (
            "price_not_verified_recently" not in r["qualification"]["reasons"],
            american_to_decimal(int(r["book_price"])) if finite(r.get("book_price")) is not None and abs(float(r["book_price"])) >= 100 else 0,
        ))
        cards.append(card(selected, now))
    cards.sort(key=lambda c: (not c["qualification"]["eligible"], "price_not_verified_recently" in c["qualification"]["reasons"], -(c["conservative_ev"] if c["conservative_ev"] is not None else -100), str(c["kickoff"]), c["id"]))
    unique = {c["id"]: c for c in reversed(cards)}
    cards = sorted(unique.values(), key=lambda c: (not c["qualification"]["eligible"], "price_not_verified_recently" in c["qualification"]["reasons"], -(c["conservative_ev"] if c["conservative_ev"] is not None else -100), str(c["kickoff"]), c["id"]))
    reasons = Counter(r for c in cards for r in c["qualification"]["reasons"])
    return {"rows": cards[offset:offset + limit], "total": len(cards), "offset": offset, "limit": limit,
            "books": sorted(available_books), "qualified": sum(c["qualification"]["eligible"] for c in cards),
            "changed": sum(bool(c["changes"]) for c in cards),
            "blockers": [{"reason": REASONS.get(r, r), "count": count} for r, count in reasons.most_common(4)]}
