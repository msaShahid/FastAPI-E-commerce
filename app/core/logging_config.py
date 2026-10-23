"""
Structured logging: one JSON object per line, with the request id, user id and
route on every line written while a request is being handled.

No call site had to change. Code keeps using ``logging.getLogger(__name__)``;
a handler filter looks up "what request is this?" in a context variable and
stamps it onto each record, and the formatter writes the record as JSON.

The context is a plain dict held in a ``ContextVar``. Because it is a shared
mutable dict, something that runs later in the same request (the auth
dependency learning the user id, routing learning the route) can fill it in and
every later log line sees it -- even code that runs in a thread pool.
"""

import contextlib
import contextvars
import json
import logging
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, TextIO

from app.core.redaction import redact

# Fields stamped on records from the request/job context, in output order.
CONTEXT_FIELDS = ("request_id", "user_id", "method", "route", "path", "job_id", "job_type")

_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "log_context", default=None
)

# Attributes every LogRecord has; anything else on a record came from `extra=`.
# uvicorn's `color_message` is the same text with terminal colour codes: noise in JSON.
_STANDARD_ATTRIBUTES = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "color_message"}

# Set on an exception once the catch-all handler has logged it with its request id.
ALREADY_LOGGED = "_already_logged"


# --- the per-request / per-job context --------------------------------------------


def new_context(**fields: Any) -> dict[str, Any]:
    return dict(fields)


def activate(context: dict[str, Any]) -> contextvars.Token:
    return _context.set(context)


def deactivate(token: contextvars.Token) -> None:
    _context.reset(token)


@contextlib.contextmanager
def bound_context(context: dict[str, Any]) -> Iterator[None]:
    token = activate(context)
    try:
        yield
    finally:
        deactivate(token)


def bind(**fields: Any) -> None:
    """Add fields (for example ``user_id``) to the current context. No-op outside one."""
    context = _context.get()
    if context is not None:
        context.update(fields)


def _route_template(scope: dict[str, Any] | None) -> str | None:
    """
    "/api/v1/products/{product_id}" for GET /api/v1/products/5, or None before the
    router has matched. Grouping by template keeps log lines (and later metrics)
    from having one distinct value per id.

    Newer FastAPI versions give an included router's routes paths relative to the
    include prefix, so the prefix is recovered from the full request path.
    """
    route = scope.get("route") if scope else None
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if not template:
        return None
    path = scope.get("path", "")
    try:
        concrete = template.format(**scope.get("path_params", {}))
    except (KeyError, IndexError, ValueError):
        return template
    if concrete and path.endswith(concrete):
        return path[: len(path) - len(concrete)] + template
    return template


def snapshot() -> dict[str, Any]:
    """The current context as plain values, with the route filled in lazily."""
    context = _context.get()
    if not context:
        return {}
    values = {k: v for k, v in context.items() if not k.startswith("_") and v is not None}
    template = _route_template(context.get("_scope"))
    if template:
        values["route"] = template
    return values


# --- stamping and formatting -----------------------------------------------------


class ContextFilter(logging.Filter):
    """Copies the current context onto each record (never overriding `extra=`)."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in snapshot().items():
            if key not in record.__dict__:
                setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key in CONTEXT_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRIBUTES and key not in payload and not key.startswith("_"):
                payload[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = redact(self.formatStack(record.stack_info))
        # default=str: a log call must never raise because of an odd value.
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Readable single-line format for local development."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = redact(super().format(record))
        tags = [
            f"{key}={getattr(record, key)}"
            for key in ("request_id", "user_id", "job_id")
            if getattr(record, key, None)
        ]
        if tags:
            # Put the tags before any traceback, which super().format appended.
            head, sep, tail = line.partition("\n")
            line = f"{head} [{' '.join(tags)}]{sep}{tail}"
        return line


class DropAlreadyLogged(logging.Filter):
    """
    For the "uvicorn.error" logger. After our catch-all handler answers a 500,
    Starlette re-raises the exception and uvicorn logs it AGAIN as "Exception in
    ASGI application" -- from outside the request, so with no request id. Drop
    that copy when we already logged the exception with its id.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not getattr(exc, ALREADY_LOGGED, False)


def mark_logged(exc: BaseException) -> None:
    with contextlib.suppress(Exception):  # an exception type that refuses attributes
        setattr(exc, ALREADY_LOGGED, True)


def build_handler(log_format: str = "json", stream: TextIO | None = None) -> logging.Handler:
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if log_format == "json" else TextFormatter())
    handler._app_handler = True  # type: ignore[attr-defined]
    return handler


def configure_logging(level: str = "INFO", log_format: str = "json") -> None:
    """Send every log line, from our code and from libraries, through one handler."""
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_app_handler", False):
            root.removeHandler(existing)
    root.addHandler(build_handler(log_format))
    root.setLevel(level)

    # uvicorn installs its own handlers and plain-text format; route its server
    # messages through ours, and drop its access log (RequestContextMiddleware
    # writes a richer one, with the request id and the route template).
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    server_errors = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, DropAlreadyLogged) for f in server_errors.filters):
        server_errors.addFilter(DropAlreadyLogged())
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
