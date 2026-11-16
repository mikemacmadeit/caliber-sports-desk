"""Read-only release/monitoring probe. No refreshes, payments, email, or writes to the API."""
import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx


def check(api_url):
    checks = []
    with httpx.Client(base_url=api_url.rstrip('/'), timeout=15) as client:
        for path, expected in (("/health", 200), ("/ready", 200), ("/v1/auth/me", 200), ("/docs", 404)):
            try:
                response = client.get(path)
                ok = response.status_code == expected
                if path == "/v1/auth/me":
                    ok = ok and response.json().get("user") is None and "no-store" in response.headers.get("cache-control", "")
                item = {"path": path, "ok": ok, "status": response.status_code, "expected": expected}
                if path == "/ready" and response.headers.get("content-type", "").startswith("application/json"):
                    item["readiness"] = response.json()
                checks.append(item)
            except Exception as exc:
                checks.append({"path": path, "ok": False, "error": type(exc).__name__})
    return {"checked_at": datetime.now(UTC).isoformat(), "ok": all(c['ok'] for c in checks), "checks": checks}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-url', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    url = urlparse(args.api_url)
    if url.scheme not in {'http', 'https'} or url.username or url.password:
        parser.error('Use an HTTP(S) API URL without embedded credentials.')
    report = check(args.api_url)
    body = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body, encoding='utf-8')
    print(body)
    raise SystemExit(0 if report['ok'] else 1)
