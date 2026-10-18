"""Structured request logging and a small in-memory write-endpoint rate limiter."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Deque

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.config import settings

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_RATE_LIMIT_BUCKETS: dict[tuple[str, str], Deque[float]] = defaultdict(deque)
_RATE_LIMIT_LOCK = threading.Lock()


class JsonRequestFormatter(logging.Formatter):
    """Emit a small allow-listed JSON record; never serialize request headers or bodies."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "method", "path", "status_code", "duration_ms", "route_group", "error_type"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_request_logger() -> logging.Logger:
    logger = logging.getLogger("support_agent.http")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonRequestFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


http_logger = configure_request_logger()


def reset_rate_limits_for_tests() -> None:
    with _RATE_LIMIT_LOCK:
        _RATE_LIMIT_BUCKETS.clear()


def _route_group(request: Request) -> str | None:
    if request.method != "POST":
        return None
    path = request.url.path
    if path == "/tickets":
        return "ticket-create"
    if re.fullmatch(r"/tickets/[^/]+/runs", path):
        return "workflow-start"
    if re.fullmatch(r"/runs/[^/]+/messages", path):
        return "customer-message"
    if re.fullmatch(r"/runs/[^/]+/resume", path):
        return "workflow-resume"
    if re.fullmatch(r"/runs/[^/]+/ticket-sync", path):
        return "ticket-sync"
    if re.fullmatch(r"/approvals/[^/]+/decision", path):
        return "approval-decision"
    return None


def _allow_request(key: tuple[str, str], now: float, limit: int, window: int) -> tuple[bool, int]:
    with _RATE_LIMIT_LOCK:
        bucket = _RATE_LIMIT_BUCKETS[key]
        while bucket and now - bucket[0] >= window:
            bucket.popleft()
        if limit > 0 and len(bucket) >= limit:
            retry_after = max(1, int(window - (now - bucket[0])))
            return False, retry_after
        if limit > 0:
            bucket.append(now)
        if not bucket:
            _RATE_LIMIT_BUCKETS.pop(key, None)
        return True, 0


async def request_observability_middleware(request: Request, call_next):
    incoming_id = request.headers.get("X-Request-ID", "")
    request_id = incoming_id if _REQUEST_ID_RE.fullmatch(incoming_id) else __import__("uuid").uuid4().hex
    start = time.perf_counter()
    route_group = _route_group(request)
    if route_group:
        client_host = request.client.host if request.client else "unknown"
        allowed, retry_after = _allow_request(
            (client_host, route_group), time.monotonic(),
            settings.rate_limit_requests, max(1, settings.rate_limit_window_seconds),
        )
        if not allowed:
            response = JSONResponse(status_code=429, content={"detail": "Too many requests; retry after the rate-limit window."})
            response.headers["Retry-After"] = str(retry_after)
            response.headers["X-Request-ID"] = request_id
            http_logger.warning("request_rate_limited", extra={
                "request_id": request_id, "method": request.method, "path": request.url.path,
                "status_code": 429, "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                "route_group": route_group,
            })
            return response
    try:
        response = await call_next(request)
    except Exception as exc:
        http_logger.error("request_failed", extra={
            "request_id": request_id, "method": request.method, "path": request.url.path,
            "status_code": 500, "duration_ms": round((time.perf_counter() - start) * 1000, 2),
            "error_type": type(exc).__name__,
        })
        raise
    response.headers["X-Request-ID"] = request_id
    http_logger.info("request_completed", extra={
        "request_id": request_id, "method": request.method, "path": request.url.path,
        "status_code": response.status_code,
        "duration_ms": round((time.perf_counter() - start) * 1000, 2),
        "route_group": route_group,
    })
    return response
