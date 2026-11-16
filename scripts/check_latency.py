"""Bounded read-only load check. Localhost by default; explicit staging host allowlist."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import time
from urllib.parse import urlparse

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("--path", default="/health")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--p95-ms", type=float, default=500)
    args = parser.parse_args()
    host = urlparse(args.base)
    allowed = {"localhost", "127.0.0.1", *os.environ.get("CALIBER_LOAD_TEST_HOSTS", "").split(",")}
    if host.hostname not in allowed or host.username or host.password:
        parser.error("Use localhost or explicitly allow a staging host in CALIBER_LOAD_TEST_HOSTS.")
    if not 1 <= args.requests <= 2000 or not 1 <= args.concurrency <= 16:
        parser.error("Use 1–2000 requests and 1–16 concurrent clients.")
    safe = {"/health", "/ready", "/v1/days", "/v1/published/history", "/v1/published/day", "/v1/anytime-td/replay", "/v1/watchlist"}
    if args.path.split("?")[0] not in safe:
        parser.error("Choose a read-only saved-data endpoint.")
    token = os.environ.get("CALIBER_LOAD_TEST_TOKEN")
    with httpx.Client(base_url=args.base, timeout=10, headers={"Authorization": f"Bearer {token}"} if token else {}) as client:
        def one(_):
            start = time.perf_counter()
            try:
                response = client.get(args.path)
                return (time.perf_counter()-start)*1000, response.status_code, len(response.content)
            except httpx.HTTPError:
                return (time.perf_counter()-start)*1000, 599, 0
        # Warm imports/connections separately, then report every measured request.
        one(0)
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            results = list(pool.map(one, range(args.requests)))
    elapsed = sorted(r[0] for r in results)
    p95 = elapsed[math.ceil(.95*len(elapsed))-1]
    failures = sum(code >= 400 for _, code, _ in results)
    print(json.dumps({"path": args.path, "requests": len(results), "concurrency": args.concurrency,
        "p50_ms": round(elapsed[len(elapsed)//2], 2), "p95_ms": round(p95, 2),
        "failures": failures, "max_response_bytes": max(r[2] for r in results),
        "passes": failures == 0 and p95 <= args.p95_ms}))
    return int(failures > 0 or p95 > args.p95_ms)


if __name__ == "__main__":
    raise SystemExit(main())
