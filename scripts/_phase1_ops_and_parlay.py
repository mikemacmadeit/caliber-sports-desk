from __future__ import annotations

import json
import urllib.request

from spe.db.session import session_factory
from spe.ops.freshness import operations_health
from spe.shadow.jobs import audit_cycle_data_quality

API = "http://localhost:8000"


def get(path: str):
    with urllib.request.urlopen(f"{API}{path}", timeout=60) as res:
        return json.loads(res.read().decode())


def post(path: str, body: dict):
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as res:
        return json.loads(res.read().decode())


def main() -> None:
    s = session_factory()
    try:
        h = operations_health(s)
        a = audit_cycle_data_quality(s, [])
        print(
            json.dumps(
                {
                    "alerts": a["alerts"],
                    "odds": a["operations_health"]["odds_freshness"],
                    "credits": a["operations_health"]["credits"],
                    "job_alerts": h["job_execution"].get("alerts_visible"),
                    "empty_success": h["job_execution"].get("empty_success"),
                    "recent_failures": h["job_execution"].get("recent_failures")[:5],
                },
                default=str,
                indent=2,
            )
        )
    finally:
        s.close()

    props = get("/v1/props?market=anytime_td&league=nfl&date=2026-11-08")["rows"]
    by_game: dict[str, list] = {}
    for row in props:
        by_game.setdefault(row["game_id"], []).append(row)
    gid, rows = next(iter(sorted(by_game.items(), key=lambda kv: -len(kv[1]))))
    legs = [r for r in rows if r.get("player_id")][:2]
    print("PARLAY_GAME", gid, [r["player_name"] for r in legs])
    created = post(
        "/v1/simulations",
        {"game_id": gid, "draw_count": 4000, "seed": 7, "scenario_overrides": {}},
    )
    print("SIM", created.get("run_id"), created.get("status"))
    parlay = post(
        "/v1/parlays/evaluate",
        {
            "game_id": gid,
            "book_american": 260,
            "legs": [
                {"player_id": legs[0]["player_id"], "market_id": "anytime_td", "side": "yes"},
                {"player_id": legs[1]["player_id"], "market_id": "anytime_td", "side": "yes"},
            ],
        },
    )
    print(
        json.dumps(
            {
                "method": parlay.get("method"),
                "joint_available": parlay.get("joint_available"),
                "joint_probability": parlay.get("joint_probability"),
                "naive_independent": parlay.get("naive_independent"),
                "warning": parlay.get("warning"),
                "ev": parlay.get("ev"),
            },
            indent=2,
        )
    )

    # Proxy smoke
    with urllib.request.urlopen("http://localhost:3000/api/backend/v1/days?league=nfl", timeout=30) as res:
        print("PROXY_STATUS", res.status, json.loads(res.read().decode()).get("dates")[:3])


if __name__ == "__main__":
    main()
