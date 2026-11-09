"""E2E verification: 5 NFL + 5 FBS games through API + simulation + parlay."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections import Counter

API = "http://localhost:8000"


def get(path: str):
    with urllib.request.urlopen(f"{API}{path}", timeout=120) as res:
        return json.loads(res.read().decode())


def post(path: str, body: dict):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as res:
        return json.loads(res.read().decode())


def inspect_date(league: str, date: str, limit: int = 5) -> list[dict]:
    slate = get(f"/v1/slate?league={league}&date={date}")
    games = (slate.get("games") or [])[:limit]
    props = get(f"/v1/props?market=anytime_td&league={league}&date={date}")
    rows = props.get("rows") or []
    by_game = Counter(r.get("game_id") for r in rows)
    out = []
    for g in games:
        gid = g["game_id"]
        detail = get(f"/v1/games/{gid}")
        game_props = [r for r in rows if r["game_id"] == gid] or detail.get("props") or []
        freshness = Counter((r.get("price_freshness") or {}).get("status") for r in game_props)
        models = Counter((r.get("forecast") or {}).get("model_version") for r in game_props)
        off_team = [
            r
            for r in game_props
            if r.get("team") not in {g["home"]["abbreviation"], g["away"]["abbreviation"], "?"}
        ]
        out.append(
            {
                "game_id": gid,
                "matchup": f"{g['away']['abbreviation']}@{g['home']['abbreviation']}",
                "league": league,
                "kickoff": g.get("kickoff_utc"),
                "prop_count": len(game_props),
                "injury_count": len(detail.get("injuries") or []),
                "freshness": dict(freshness),
                "models": dict(models),
                "wrong_team_rows": len(off_team),
                "sample_players": [
                    f"{r.get('player_name')}({r.get('team')})" for r in game_props[:4]
                ],
            }
        )
    print(f"{league} {date} slate={len(slate.get('games') or [])} props={len(rows)} by_game_top={by_game.most_common(3)}")
    return out


def main() -> None:
    nfl_days = get("/v1/days?league=nfl")["dates"]
    fbs_days = get("/v1/days?league=fbs")["dates"]
    reports = []
    for date in nfl_days[:2]:
        reports.extend(inspect_date("nfl", date, 5))
        if len([r for r in reports if r["league"] == "nfl"]) >= 5:
            break
    for date in fbs_days[:2]:
        reports.extend(inspect_date("fbs", date, 5))
        if len([r for r in reports if r["league"] == "fbs"]) >= 5:
            break

    print("GAME_REPORTS", json.dumps(reports, indent=2, default=str))

    # Scenario + parlay on first NFL game with props, else FBS.
    candidates = [r for r in reports if r["prop_count"] >= 2]
    if not candidates:
        print("NO_CANDIDATES")
        return
    target = candidates[0]
    gid = target["game_id"]
    print("SIM_TARGET", gid, target["matchup"])
    try:
        created = post(
            "/v1/simulations",
            {"game_id": gid, "draw_count": 3000, "seed": 42, "scenario_overrides": {}},
        )
        print("SIM_CREATE", {k: created.get(k) for k in ("run_id", "status", "research_only", "note")})
        detail = get(f"/v1/simulations/{created['run_id']}")
        anytime = (detail.get("summary") or {}).get("anytime_td") or []
        print(
            "SIM_DETAIL",
            {
                "matchup": detail.get("matchup"),
                "draws": detail.get("draw_count"),
                "anytime_n": len(anytime),
                "top": anytime[:3],
                "joint_pairs": (detail.get("summary") or {}).get("joint_td_pairs"),
            },
        )
        legs = [row["player_id"] for row in anytime[:2] if row.get("player_id")]
        if len(legs) >= 2:
            parlay = post(
                "/v1/parlays/evaluate",
                {
                    "game_id": gid,
                    "book_american": 260,
                    "legs": [
                        {"player_id": legs[0], "market_id": "anytime_td", "side": "yes"},
                        {"player_id": legs[1], "market_id": "anytime_td", "side": "yes"},
                    ],
                },
            )
            print(
                "PARLAY",
                {
                    "method": parlay.get("method"),
                    "joint_available": parlay.get("joint_available"),
                    "joint_probability": parlay.get("joint_probability"),
                    "naive_independent": parlay.get("naive_independent"),
                    "warning": parlay.get("warning"),
                    "ev": parlay.get("ev"),
                },
            )
    except urllib.error.HTTPError as exc:
        print("SIM_HTTP_ERROR", exc.code, exc.read().decode()[:500])

    # Ops health alerts
    try:
        health = get("/v1/coverage")
    except Exception:
        health = None
    if health:
        print("COVERAGE_KEYS", sorted(health.keys())[:20])


if __name__ == "__main__":
    main()
