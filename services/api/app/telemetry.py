"""Bounded, process-local request metrics. No query strings, payloads, or credentials."""
from collections import defaultdict, deque
from threading import Lock
import json
import logging
import math
import time
import uuid

from fastapi import Response
from pydantic import BaseModel, Field, FiniteFloat
from typing import Literal

_lock = Lock()
_samples = defaultdict(lambda: deque(maxlen=2000))
_client_samples = defaultdict(lambda: deque(maxlen=2000))
_client_received = deque(maxlen=600)


class ClientMetric(BaseModel):
    route: Literal["/", "/anytime-td", "/props", "/ats", "/schedule", "/track-record", "/account", "/games/:id", "/login", "/signup"]
    kind: Literal["error", "paint_ms", "interaction_max_ms", "layout_shift_total"]
    value: FiniteFloat = Field(ge=0, le=120000)



def install(app):
    @app.post("/v1/client-metrics", status_code=202)
    def receive_client_metric(metric: ClientMetric):
        now = time.monotonic()
        with _lock:
            if len(_client_received) < 600 or now-_client_received[0] > 60:
                _client_received.append(now)
                _client_samples[(metric.route, metric.kind)].append(metric.value)
        return Response(status_code=202)

    @app.middleware("http")
    async def measured(request, call_next):
        request_id = uuid.uuid4().hex
        started = time.perf_counter()
        code = 500
        try:
            response = await call_next(request)
            code = response.status_code
            response.headers["X-Request-ID"] = request_id
            response.headers["Server-Timing"] = f"api;dur={(time.perf_counter()-started)*1000:.1f}"
            return response
        finally:
            route = getattr(request.scope.get("route"), "path", "unmatched")
            elapsed = (time.perf_counter()-started)*1000
            with _lock:
                method = request.method if request.method in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"} else "OTHER"
                _samples[(method, route)].append((elapsed, code))
            logging.getLogger("uvicorn.error").info(json.dumps({"event": "http_request", "request_id": request_id,
                "method": request.method, "route": route, "status": code, "duration_ms": round(elapsed, 2)}))


def snapshot():
    with _lock:
        groups = {key: list(value) for key, value in _samples.items()}
        client = [{"route": route, "kind": kind, "samples": len(values), "p75": sorted(values)[math.ceil(.75*len(values))-1]} for (route, kind), values in _client_samples.items()]
    rows = []
    for (method, route), values in groups.items():
        times = sorted(v[0] for v in values)
        rows.append({"method": method, "route": route, "requests": len(values),
            "errors": sum(code >= 500 for _, code in values),
            "p95_ms": round(times[max(0, math.ceil(.95*len(times))-1)], 2)})
    return {"scope": "this_process_last_2000_requests_per_route", "routes": rows, "client_measurements": client, "client_note": "Anonymous client-reported diagnostics; these are not audited Core Web Vitals or trusted performance evidence."}
