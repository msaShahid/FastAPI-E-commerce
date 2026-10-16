"""
Request id + access log, as one pure-ASGI middleware.

* Every request gets an id: the caller's ``X-Request-ID`` if it is a sane value,
  otherwise a fresh random one. It is returned in the ``X-Request-ID`` response
  header, put on every log line for the request, and included in error bodies,
  so "the id from the error" finds the logs.
* One access-log line per request, written when the response is finished, with
  status and duration. It names the route *template* (``/products/{product_id}``),
  not the raw path, so lines group sensibly.

Pure ASGI (not ``BaseHTTPMiddleware``) so the context variable set here is
visible to the endpoint and its dependencies, and nothing is buffered.
"""

import asyncio
import logging
import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging_config import (
    activate,
    bound_context,
    deactivate,
    new_context,
    snapshot,
)

REQUEST_ID_HEADER = "X-Request-ID"

# An inbound id goes into logs and response headers, so accept only a tame
# alphabet and length: no newlines (log injection), no megabyte values.
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{8,64}")

# Polled constantly by load balancers and uptime monitors; logging each one at
# INFO would bury everything else. Failures are still logged.
_QUIET_PATHS = frozenset({"/health", "/health/db", "/metrics"})

access_logger = logging.getLogger("app.access")


def choose_request_id(incoming: str | None) -> str:
    if incoming and _VALID_REQUEST_ID.fullmatch(incoming):
        return incoming
    return uuid.uuid4().hex


def get_request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def restore_context(request: Request):
    """
    Re-activate the request's log context. Needed by the catch-all 500 handler:
    Starlette runs it OUTSIDE this middleware, after the context was reset.
    """
    return bound_context(getattr(request.state, "log_context", None) or {})


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = choose_request_id(Headers(scope=scope).get(REQUEST_ID_HEADER))
        context = new_context(
            request_id=request_id, method=scope["method"], path=scope["path"], _scope=scope
        )
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["log_context"] = context
        token = activate(context)

        status_code = 500  # what it is if the app raises before answering
        response_started = False
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except asyncio.CancelledError:
            # The client went away mid-request (nginx's convention: 499).
            if not response_started:
                status_code = 499
            raise
        finally:
            self._log_access(status_code, (time.perf_counter() - started) * 1000)
            deactivate(token)

    @staticmethod
    def _log_access(status_code: int, duration_ms: float) -> None:
        fields = snapshot()
        target = fields.get("route") or fields.get("path", "")
        if status_code >= 500:
            level = logging.ERROR
        elif fields.get("path") in _QUIET_PATHS:
            level = logging.DEBUG
        else:
            level = logging.INFO
        access_logger.log(
            level,
            "%s %s -> %d",
            fields.get("method", ""),
            target,
            status_code,
            extra={"status": status_code, "duration_ms": round(duration_ms, 1)},
        )
