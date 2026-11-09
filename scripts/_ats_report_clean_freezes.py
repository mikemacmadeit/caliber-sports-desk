"""Summarize clean live-book ATS freezes."""
from __future__ import annotations

import json
from collections import Counter

from sqlalchemy import text

from spe.db.session import init_db, session_factory
from spe.ingest.odds_budget import budget_status


def main() -> None:
    init_db()
    with session_factory() as s:
        rows = s.execute(
            text(
                """
                SELECT a.ats_id, a.game_id, a.league_id, a.bookmaker_id, a.side,
                       a.spread_line, a.price_american, a.p_cover, a.expected_value,
                       a.predicted_margin, a.model_version_id, a.predicted_at,
                       a.components, th.name AS home, ta.name AS away, g.kickoff_utc
                FROM ats_predictions a
                JOIN games g ON g.game_id = a.game_id
                JOIN teams th ON th.team_id = a.home_team_id
                JOIN teams ta ON ta.team_id = a.away_team_id
                ORDER BY a.predicted_at DESC
                """
            )
        ).mappings().all()
        elig = Counter()
        clean = []
        for r in rows:
            comps = r["components"]
            if isinstance(comps, str):
                comps = json.loads(comps)
            e = (comps or {}).get("eligibility") or "unknown"
            elig[e] += 1
            if e == "clean_prospective":
                clean.append(
                    {
                        "game_id": r["game_id"],
                        "matchup": f"{r['away']} @ {r['home']}",
                        "kickoff_utc": str(r["kickoff_utc"]),
                        "book": r["bookmaker_id"],
                        "side": r["side"],
                        "line": r["spread_line"],
                        "price": r["price_american"],
                        "p_cover": r["p_cover"],
                        "ev": r["expected_value"],
                        "pred_margin": r["predicted_margin"],
                        "quote_class": (comps or {}).get("quote_source_class"),
                        "home_handicap": (comps or {}).get("home_handicap"),
                        "bundle": (comps or {}).get("research_bundle"),
                        "predicted_at": str(r["predicted_at"]),
                    }
                )
        out = {
            "budget": budget_status(s),
            "eligibility_counts": dict(elig),
            "clean_prospective_n": len(clean),
            "clean_prospective": clean,
        }
        path = "data/research/ats_audit/clean_live_book_refresh.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, default=str)
        print(json.dumps({"eligibility_counts": dict(elig), "clean_n": len(clean), "path": path}, indent=2))
        for row in clean[:20]:
            print(
                f"{row['matchup']}: {row['side']} {row['line']:+g} @ {row['book']} "
                f"px={row['price']} p_cover={row['p_cover']:.3f} ev={row['ev']}"
            )


if __name__ == "__main__":
    main()
