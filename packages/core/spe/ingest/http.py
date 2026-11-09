from __future__ import annotations

import time
from pathlib import Path

import httpx

from spe.adapters.base import AdapterError

USER_AGENT = "CaliberResearch/0.2 (private; nflverse-attribution)"


def request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict | None = None,
    timeout: float = 120.0,
    retries: int = 5,
    client: httpx.Client | None = None,
) -> httpx.Response:
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    owned = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    delay = 1.5
    last_exc: Exception | None = None
    try:
        for attempt in range(retries):
            try:
                res = client.request(method, url, headers=hdrs, params=params)
                if res.status_code in {429, 503, 502} and attempt < retries - 1:
                    wait = float(res.headers.get("Retry-After") or delay)
                    time.sleep(wait)
                    delay *= 2
                    continue
                return res
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt >= retries - 1:
                    break
                time.sleep(delay)
                delay *= 2
        if last_exc:
            raise AdapterError(f"{method} {url} failed: {last_exc}") from last_exc
        raise AdapterError(f"{method} {url} failed after retries")
    finally:
        if owned:
            client.close()


def download_file(url: str, dest: Path, *, refresh: bool = False) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0 and not refresh:
        return dest
    res = request_with_retry("GET", url, timeout=180.0)
    if res.status_code >= 400:
        raise AdapterError(f"download {url} -> HTTP {res.status_code}")
    dest.write_bytes(res.content)
    return dest
