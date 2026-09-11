import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings

EXACT_LOG_FIELDS = (
    "timestamp",
    "level",
    "service",
    "release_sha",
    "event",
    "request_id",
    "route",
    "method",
    "status_code",
    "duration_ms",
    "trace_id",
    "error_code",
    "outbox_id",
    "attempt",
)

UUID_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

REDACT_PATTERNS = [
    (
        re.compile(r'(?i)(["\']?authorization["\']?\s*[:=]\s*["\']?(?:bearer\s+)?)[^"\'\s,}]+'),
        r"\g<1>[REDACTED]",
    ),
    (re.compile(r'(?i)(["\']?cookie["\']?\s*[:=]\s*["\']?)[^"\'\r\n,}]+'), r"\g<1>[REDACTED]"),
    (re.compile(r'(?i)(["\']?password["\']?\s*[:=]\s*["\']?)[^"\'\s,}]+'), r"\g<1>[REDACTED]"),
    (
        re.compile(r'(?i)(["\']?invitation_token["\']?\s*[:=]\s*["\']?)[^"\'\s,}]+'),
        r"\g<1>[REDACTED]",
    ),
    (re.compile(r'(?i)(["\']?smtp_password["\']?\s*[:=]\s*["\']?)[^"\'\s,}]+'), r"\g<1>[REDACTED]"),
    (re.compile(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)"), r"[REDACTED_EMAIL]"),
]


def redact_sensitive_text(text: str) -> str:
    for pattern, repl in REDACT_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class StructuredJsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON adhering strictly to the required schema."""

    def __init__(self, service: str = "api") -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        settings = get_settings()
        now_utc = datetime.now(timezone.utc).isoformat()

        log_data: dict[str, Any] = {
            "timestamp": now_utc,
            "level": record.levelname,
            "service": getattr(record, "service", self.service),
            "release_sha": getattr(record, "release_sha", settings.RELEASE_SHA),
            "event": getattr(record, "event", record.getMessage()),
            "request_id": getattr(record, "request_id", None),
            "route": getattr(record, "route", None),
            "method": getattr(record, "method", None),
            "status_code": getattr(record, "status_code", None),
            "duration_ms": getattr(record, "duration_ms", None),
            "trace_id": getattr(record, "trace_id", None),
            "error_code": getattr(record, "error_code", None),
            "outbox_id": getattr(record, "outbox_id", None),
            "attempt": getattr(record, "attempt", None),
        }

        # Validate exactly the 14 required fields
        ordered_data = {field: log_data.get(field) for field in EXACT_LOG_FIELDS}

        raw_json = json.dumps(ordered_data)
        return redact_sensitive_text(raw_json)


def is_valid_uuid(val: str | None) -> bool:
    if not val or len(val) != 36:
        return False
    try:
        parsed = uuid.UUID(val)
        return str(parsed) == val.lower()
    except (ValueError, TypeError, AttributeError):
        return False


def get_structured_logger(name: str = "app", service: str = "api") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not any(
        isinstance(h, logging.StreamHandler) and isinstance(h.formatter, StructuredJsonFormatter)
        for h in logger.handlers
    ):
        handler = logging.StreamHandler()
        handler.setFormatter(StructuredJsonFormatter(service=service))
        logger.addHandler(handler)
        logger.propagate = False
    return logger


logger = get_structured_logger("app.observability", service="api")


def log_worker_event(
    *,
    event: str,
    outbox_id: str | uuid.UUID | None = None,
    attempt: int | None = None,
    level: int = logging.INFO,
    error_code: str | None = None,
    duration_ms: float | None = None,
    service: str = "worker",
) -> None:
    """Emit a structured JSON log for worker events using a new correlation UUID and outbox ID."""
    correlation_id = str(uuid.uuid4())
    worker_logger = get_structured_logger("worker.observability", service=service)

    extra = {
        "service": service,
        "event": event,
        "request_id": correlation_id,
        "route": None,
        "method": None,
        "status_code": None,
        "duration_ms": duration_ms,
        "trace_id": None,
        "error_code": error_code,
        "outbox_id": str(outbox_id) if outbox_id is not None else None,
        "attempt": attempt,
    }
    worker_logger.log(level, event, extra=extra)


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    """Logs incoming HTTP requests in structured JSON with strict UUID request ID handling."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        inbound_id = request.headers.get("X-Request-ID")
        if inbound_id and is_valid_uuid(inbound_id):
            request_id = str(uuid.UUID(inbound_id))
        else:
            request_id = str(uuid.uuid4())

        request.state.request_id = request_id

        start_time = time.perf_counter()
        response: Response | None = None
        error_code: str | None = None

        try:
            response = await call_next(request)
            error_code = getattr(request.state, "error_code", None)
            return response
        except Exception as exc:
            error_code = getattr(exc, "code", "INTERNAL_ERROR")
            raise
        finally:
            duration_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            status_code = response.status_code if response else 500

            route_obj = request.scope.get("route")
            if route_obj and hasattr(route_obj, "path"):
                route_template = route_obj.path
            else:
                route_template = "unmatched"

            extra = {
                "service": "api",
                "event": "http_request",
                "request_id": request_id,
                "route": route_template,
                "method": request.method,
                "status_code": status_code,
                "duration_ms": duration_ms,
                "trace_id": getattr(request.state, "trace_id", None),
                "error_code": error_code,
                "outbox_id": None,
                "attempt": None,
            }

            logger.info("http_request", extra=extra)
            if response is not None:
                response.headers["X-Request-ID"] = request_id
