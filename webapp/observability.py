"""Request ids, structured JSON logs with redaction, error reporting and the
safe 500 response (Bundle 7 spec §20.6)."""
from __future__ import annotations

import json
import logging
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

REDACTED = "[REDACTED]"
SECRET_KEYS = frozenset({
    "authorization", "cookie", "set-cookie", "password", "token", "code", "refresh_token", "access_token",
    "secret", "csrf_token", "x-csrf-token",
})
_STANDARD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}
request_logger = logging.getLogger("webapp.request")
error_logger = logging.getLogger("webapp.errors")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (REDACTED if isinstance(k, str) and k.lower() in SECRET_KEYS else redact(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = "".join(traceback.format_exception(*record.exc_info)).strip()
        return json.dumps(redact(payload), default=str, ensure_ascii=False)


def configure_logging() -> None:
    """One JSON stream handler on the ``webapp`` logger (idempotent)."""
    logger = logging.getLogger("webapp")
    if not any(getattr(h, "_jobsearch_json", False) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        handler._jobsearch_json = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)


class ErrorReporter(Protocol):
    def report(self, exc: BaseException, context: dict[str, Any]) -> None: ...


class LogErrorReporter:
    def report(self, exc: BaseException, context: dict[str, Any]) -> None:
        error_logger.error("unhandled error request_id=%s", context.get("request_id"),
                           exc_info=(type(exc), exc, exc.__traceback__), extra={"context": context})


def _valid_request_id(raw: str | None) -> str | None:
    try:
        return str(uuid.UUID(raw)) if raw else None
    except ValueError:
        return None


class RequestContextMiddleware:
    """Assigns ``request.state.request_id`` (a valid inbound X-Request-ID or a
    new UUID), echoes it, and logs one JSON line per request."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        request_id = _valid_request_id(headers.get("x-request-id")) or str(uuid.uuid4())
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        started = time.monotonic()
        status = {"code": 500}

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", [])
                message["headers"] = [h for h in message["headers"] if h[0].lower() != b"x-request-id"]
                message["headers"].append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            route = scope.get("route")
            extra = {
                "request_id": request_id, "method": scope.get("method"),
                "route": getattr(route, "path", None) or scope.get("path"), "status": status["code"],
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            }
            if state.get("account_id"):
                extra["account_id"] = state["account_id"]
            request_logger.info("request", extra=extra)
            template = getattr(route, "path", None) or "unmatched"  # route templates only: bounded labels
            METRICS.inc("http_requests_total", method=scope.get("method"), route=template, status=status["code"])
            METRICS.observe("http_request_duration_seconds", time.monotonic() - started, route=template)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or str(uuid.uuid4())


async def unhandled_error_handler(request: Request, exc: Exception):
    request_id = _request_id(request)
    reporter = getattr(request.app.state, "error_reporter", None) or LogErrorReporter()
    try:
        reporter.report(exc, {"request_id": request_id, "path": request.url.path, "method": request.method})
    except Exception:  # the reporter must never mask the original failure
        error_logger.exception("error reporter failed")
    headers = {"x-request-id": request_id}
    wants_json = request.url.path.startswith("/api") or "application/json" in request.headers.get("accept", "")
    if wants_json:
        return JSONResponse({"error": "INTERNAL_ERROR", "message": "Something went wrong.",
                             "request_id": request_id}, status_code=500, headers=headers)
    return HTMLResponse(
        "<!doctype html><title>Something went wrong</title><h1>Something went wrong</h1>"
        f"<p>Please try again. If it keeps happening, quote reference <code>{request_id}</code>.</p>",
        status_code=500, headers=headers)


# ---- metrics (Bundle 7 spec §20.6): an in-process registry rendered as Prometheus text ----------

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class MetricsRegistry:
    """Counters and histograms keyed by (name, sorted labels). Thread-safe."""

    def __init__(self) -> None:
        import threading
        self._lock = threading.Lock()
        self.counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self.histograms: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = {}  # buckets..., sum, count

    @staticmethod
    def _key(name: str, labels: dict[str, Any]) -> tuple[str, tuple[tuple[str, str], ...]]:
        return name, tuple(sorted((k, str(v)) for k, v in labels.items()))

    def inc(self, name: str, amount: float = 1.0, **labels: Any) -> None:
        key = self._key(name, labels)
        with self._lock:
            self.counters[key] = self.counters.get(key, 0.0) + amount

    def observe(self, name: str, value: float, **labels: Any) -> None:
        key = self._key(name, labels)
        with self._lock:
            series = self.histograms.setdefault(key, [0.0] * (len(LATENCY_BUCKETS) + 2))
            for i, bound in enumerate(LATENCY_BUCKETS):
                if value <= bound:
                    series[i] += 1
            series[-2] += value
            series[-1] += 1

    def reset(self) -> None:
        with self._lock:
            self.counters.clear()
            self.histograms.clear()

    def render(self) -> list[str]:
        lines: list[str] = []
        with self._lock:
            counters = sorted(self.counters.items())
            histograms = sorted(self.histograms.items())
        for (name, labels), value in counters:
            lines.append(f"{name}{_labels(labels)} {_number(value)}")
        for (name, labels), series in histograms:
            for i, bound in enumerate(LATENCY_BUCKETS):
                lines.append(f"{name}_bucket{_labels(labels + (('le', str(bound)),))} {_number(series[i])}")
            lines.append(f"{name}_bucket{_labels(labels + (('le', '+Inf'),))} {_number(series[-1])}")
            lines.append(f"{name}_sum{_labels(labels)} {_number(series[-2])}")
            lines.append(f"{name}_count{_labels(labels)} {_number(series[-1])}")
        return lines


def _labels(pairs: tuple[tuple[str, str], ...]) -> str:
    if not pairs:
        return ""
    escaped = (f'{k}="{v.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34)).replace(chr(10), " ")}"'
               for k, v in pairs)
    return "{" + ",".join(escaped) + "}"


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


METRICS = MetricsRegistry()
