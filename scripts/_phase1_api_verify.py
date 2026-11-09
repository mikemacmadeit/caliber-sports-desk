"""Verify Phase 1 API payloads for identity + freshness + demo separation."""

from __future__ import annotations

import json
import urllib.request
from collections import Counter

API = "http://localhost:8000"


def get(path: str):
    with urllib.request.urlopen(f"{API}{path}", timeout=60) as res:
        return json.loads(res.read().decode())


def main() -> None:
    days = get("/v1/days?league=all")
    date = (days.get("dates") or [days.get("today")])[0]
    print("slate_date", date)

    slate = get(f"/v1/slate?league=all&date={date}")
    games = slate.get("games") or []
    tiers = Counter(g.get("data_tier") for g in games)
    leagues = Counter(g.get("league_id") for g in games)
    print("slate_games", len(games), "tiers", dict(tiers), "leagues", dict(leagues))
    demo = [g["game_id"] for g in games if g.get("data_tier") == "demo"]
    print("demo_on_slate", demo)

    props = get(f"/v1/props?market=anytime_td&league=all&date={date}")
    rows = props.get("rows") or []
    print("prop_rows", len(rows))
    freshness = Counter((r.get("price_freshness") or {}).get("status") for r in rows)
    models = Counter((r.get("forecast") or {}).get("model_version") for r in rows)
    research = sum(1 for r in rows if (r.get("forecast") or {}).get("research_only"))
    opportunity = sum(1 for r in rows if (r.get("price_freshness") or {}).get("is_current_opportunity"))
    print("freshness", dict(freshness))
    print("models", dict(models))
    print("research_only_rows", research, "current_opportunities", opportunity)
    if rows:
        sample = rows[0]
        print(
            "sample",
            {
                "player": sample.get("player_name"),
                "team": sample.get("team"),
                "game": sample.get("game_id"),
                "book": sample.get("bookmaker"),
                "price": sample.get("book_price"),
                "freshness": sample.get("price_freshness"),
                "model": (sample.get("forecast") or {}).get("model_version"),
                "reasons": (sample.get("forecast") or {}).get("reason_codes"),
            },
        )

    nfl = [g for g in games if g.get("league_id") == "nfl"][:5]
    fbs = [g for g in games if g.get("league_id") == "fbs"][:5]
    print("verify_nfl", [g["game_id"] for g in nfl])
    print("verify_fbs", [g["game_id"] for g in fbs])
    for g in nfl + fbs:
        detail = get(f"/v1/games/{g['game_id']}")
        inj = detail.get("injuries") or []
        props_g = detail.get("props") or []
        print(
            g["game_id"],
            "injuries",
            len(inj),
            "props",
            len(props_g),
            "teams",
            f"{g['away']['abbreviation']}@{g['home']['abbreviation']}",
        )


if __name__ == "__main__":
    main()
