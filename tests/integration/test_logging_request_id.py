"""
Stage 5.1 / 5.2: structured logs and request ids.

Done when:
  * a single request's log lines can be pulled out of a shared stream by its
    request id alone, and
  * an error response carries a request id the customer can quote to support.
"""

import asyncio
import io
import json
import logging
import re
import uuid

import pytest
from fastapi import Depends
from httpx import ASGITransport, AsyncClient

from app.core.logging_config import (
    JsonFormatter,
    TextFormatter,
    build_handler,
    configure_logging,
)
from app.core.request_context import REQUEST_ID_HEADER, choose_request_id
from app.core.security import create_access_token
from app.main import app
from app.modules.auth.dependencies.auth import CurrentUser
from app.modules.auth.models.user import User
from app.shared.enums.roles import UserRole

API = "/api/v1"
probe_logger = logging.getLogger("tests.probe")


# --- test-only routes + a log sink ------------------------------------------------


@pytest.fixture
def log_lines():
    """Everything logged during the test, as parsed JSON objects."""
    stream = io.StringIO()
    handler = build_handler("json", stream)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)

    def lines() -> list[dict]:
        return [json.loads(line) for line in stream.getvalue().splitlines() if line]

    yield lines
    root.removeHandler(handler)
    root.setLevel(old_level)


boom_error = RuntimeError("kaboom")


@pytest.fixture
def probe_routes():
    async def log_once():
        probe_logger.info("hello from the handler")
        return {"ok": True}

    async def slow(name: str):
        probe_logger.info("start %s", name)
        await asyncio.sleep(0.05)  # lets the other request run in between
        probe_logger.info("end %s", name)
        return {"name": name}

    def sync_log():
        probe_logger.info("hello from a worker thread")
        return {"ok": True}

    async def boom():
        raise boom_error

    async def whoami(user: CurrentUser):
        probe_logger.info("who am i")
        return {"id": str(user.id)}

    async def needs_nothing(_=Depends(lambda: None)):
        return {}

    before = len(app.router.routes)
    app.add_api_route("/__probe/log", log_once)
    app.add_api_route("/__probe/slow/{name}", slow)
    app.add_api_route("/__probe/sync", sync_log)
    app.add_api_route("/__probe/boom", boom)
    app.add_api_route("/__probe/me", whoami)
    yield
    del app.router.routes[before:]


async def _user(db_session, role=UserRole.USER) -> User:
    unique = uuid.uuid4().hex[:8]
    user = User(
        id=uuid.uuid4(), username=f"l_{unique}", email=f"l_{unique}@example.com",
        password_hash="irrelevant", role=role, is_active=True,
    )  # fmt: skip
    db_session.add(user)
    await db_session.flush()
    await db_session.commit()
    return user


def _for(lines, request_id):
    return [line for line in lines if line.get("request_id") == request_id]


# --- the request id ---------------------------------------------------------------


async def test_every_response_has_a_request_id(client):
    response = await client.get("/health")
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers[REQUEST_ID_HEADER])


async def test_each_request_gets_a_different_id(client):
    ids = {(await client.get("/health")).headers[REQUEST_ID_HEADER] for _ in range(5)}
    assert len(ids) == 5


async def test_a_caller_supplied_id_is_forwarded(client):
    response = await client.get("/health", headers={REQUEST_ID_HEADER: "trace-from-gateway-123"})
    assert response.headers[REQUEST_ID_HEADER] == "trace-from-gateway-123"


@pytest.mark.parametrize(
    "bad",
    ["short", "x" * 65, "has space in it", "semi;colon;value", "tab\tseparated-value", "a/b/c/d/e/f/g"],
)
async def test_a_malformed_caller_id_is_replaced(client, bad):
    response = await client.get("/health", headers={REQUEST_ID_HEADER: bad})
    assert response.headers[REQUEST_ID_HEADER] != bad
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers[REQUEST_ID_HEADER])


def test_choose_request_id_rejects_log_injection():
    # A newline would let a caller forge a second log line in plain-text logs.
    assert choose_request_id("good-id-12345\nINFO fake line") != "good-id-12345\nINFO fake line"
    assert choose_request_id(None) and choose_request_id("")


async def test_cors_exposes_the_header_to_browsers(client):
    response = await client.get(
        "/health", headers={"Origin": "http://localhost:3000"}
    )
    assert REQUEST_ID_HEADER.lower() in response.headers.get(
        "access-control-expose-headers", ""
    ).lower()


# --- error bodies carry the id -------------------------------------------------------


async def test_not_found_error_includes_the_request_id(client):
    response = await client.get(f"{API}/products/987654")
    assert response.status_code == 404
    body = response.json()
    assert body["request_id"] == response.headers[REQUEST_ID_HEADER]
    assert "detail" in body


async def test_validation_error_includes_the_request_id_and_keeps_its_shape(client):
    response = await client.get(f"{API}/products", params={"page_size": 0})
    assert response.status_code == 422
    body = response.json()
    assert isinstance(body["detail"], list) and body["detail"][0]["loc"]
    assert body["request_id"] == response.headers[REQUEST_ID_HEADER]


async def test_unknown_route_and_wrong_method_include_the_request_id(client):
    for response in (await client.get("/nope"), await client.delete("/health")):
        assert response.status_code in (404, 405)
        assert response.json()["request_id"] == response.headers[REQUEST_ID_HEADER]


async def test_unauthorized_error_keeps_its_headers_and_gets_the_id(client):
    response = await client.get(
        f"{API}/orders", headers={"Authorization": "Bearer not-a-token"}
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["request_id"] == response.headers[REQUEST_ID_HEADER]


async def test_the_callers_own_id_comes_back_in_the_error_body(client):
    response = await client.get(
        f"{API}/products/987654", headers={REQUEST_ID_HEADER: "support-ticket-98765"}
    )
    assert response.json()["request_id"] == "support-ticket-98765"


async def test_unhandled_exception_returns_500_with_the_id_and_logs_the_traceback(
    probe_routes, log_lines
):
    # raise_app_exceptions=False: behave like a real server, which answers 500
    # instead of re-raising into the caller.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/__probe/boom", headers={REQUEST_ID_HEADER: "boom-request-0001"})

    assert response.status_code == 500
    assert response.json() == {"detail": "An unexpected error occurred", "request_id": "boom-request-0001"}
    assert response.headers[REQUEST_ID_HEADER] == "boom-request-0001"

    mine = _for(log_lines(), "boom-request-0001")
    errors = [line for line in mine if "exception" in line]
    assert errors, "the traceback was not logged with the request id"
    assert "RuntimeError: kaboom" in errors[0]["exception"]
    assert errors[0]["path"] == "/__probe/boom"
    # ...and the access line for it says 500
    access = [line for line in mine if line["logger"] == "app.access"]
    assert access and access[-1]["status"] == 500 and access[-1]["level"] == "ERROR"


# --- the request id on every log line ----------------------------------------------------


async def test_lines_logged_by_a_handler_carry_the_request_id(client, probe_routes, log_lines):
    response = await client.get("/__probe/log", headers={REQUEST_ID_HEADER: "grep-me-0000001"})
    assert response.status_code == 200

    mine = _for(log_lines(), "grep-me-0000001")
    messages = [line["message"] for line in mine]
    assert "hello from the handler" in messages
    # one handler line + one access line, nothing else needed to find them
    assert len(mine) == 2
    handler_line = next(line for line in mine if line["logger"] == "tests.probe")
    assert handler_line["method"] == "GET"
    assert handler_line["path"] == "/__probe/log"
    assert handler_line["route"] == "/__probe/log"


async def test_lines_logged_from_a_thread_pool_endpoint_carry_the_request_id(
    client, probe_routes, log_lines
):
    await client.get("/__probe/sync", headers={REQUEST_ID_HEADER: "thread-req-000001"})
    mine = _for(log_lines(), "thread-req-000001")
    assert "hello from a worker thread" in [line["message"] for line in mine]


async def test_concurrent_requests_do_not_mix_their_log_lines(client, probe_routes, log_lines):
    await asyncio.gather(
        client.get("/__probe/slow/alpha", headers={REQUEST_ID_HEADER: "request-alpha-0001"}),
        client.get("/__probe/slow/beta", headers={REQUEST_ID_HEADER: "request-beta-00001"}),
    )
    lines = log_lines()
    alpha = [line["message"] for line in _for(lines, "request-alpha-0001") if line["logger"] == "tests.probe"]
    beta = [line["message"] for line in _for(lines, "request-beta-00001") if line["logger"] == "tests.probe"]
    assert alpha == ["start alpha", "end alpha"]
    assert beta == ["start beta", "end beta"]


async def test_the_context_does_not_leak_into_logs_written_after_a_request(
    client, probe_routes, log_lines
):
    await client.get("/__probe/log", headers={REQUEST_ID_HEADER: "leaky-request-0001"})
    probe_logger.info("written between requests")
    line = next(line for line in log_lines() if line["message"] == "written between requests")
    assert "request_id" not in line and "user_id" not in line


async def test_the_user_id_appears_once_the_user_is_known(client, db_session, probe_routes, log_lines):
    user = await _user(db_session)
    headers = {
        "Authorization": f"Bearer {create_access_token(str(user.id))}",
        REQUEST_ID_HEADER: "with-user-00000001",
    }
    assert (await client.get("/__probe/me", headers=headers)).status_code == 200

    mine = _for(log_lines(), "with-user-00000001")
    assert mine and all(line["user_id"] == str(user.id) for line in mine if line["message"] == "who am i")
    access = next(line for line in mine if line["logger"] == "app.access")
    assert access["user_id"] == str(user.id)  # set mid-request, still on the access line


async def test_an_anonymous_request_has_no_user_id(client, log_lines):
    await client.get("/health", headers={REQUEST_ID_HEADER: "anonymous-req-0001"})
    assert all("user_id" not in line for line in _for(log_lines(), "anonymous-req-0001"))


# --- the access log -----------------------------------------------------------------------


async def test_one_access_line_per_request_with_status_and_duration(client, log_lines):
    await client.get(f"{API}/products/987654", headers={REQUEST_ID_HEADER: "access-line-00001"})
    access = [line for line in _for(log_lines(), "access-line-00001") if line["logger"] == "app.access"]
    assert len(access) == 1
    line = access[0]
    assert line["status"] == 404 and line["level"] == "INFO"  # a 404 is the client's doing
    assert isinstance(line["duration_ms"], int | float) and line["duration_ms"] >= 0
    assert line["method"] == "GET"


async def test_the_access_line_names_the_route_template_not_the_raw_path(client, log_lines):
    product_id = 987654
    await client.get(f"{API}/products/{product_id}", headers={REQUEST_ID_HEADER: "template-req-00001"})
    line = next(entry for entry in _for(log_lines(), "template-req-00001") if entry["logger"] == "app.access")
    assert line["route"] == f"{API}/products/{{product_id}}"
    assert str(product_id) in line["path"]
    assert line["message"] == f"GET {API}/products/{{product_id}} -> 404"


async def test_the_query_string_is_never_logged(client, log_lines):
    await client.get(
        f"{API}/products", params={"search": "SECRET-TOKEN-VALUE"}, headers={REQUEST_ID_HEADER: "no-query-000001"}
    )
    raw = json.dumps(_for(log_lines(), "no-query-000001"))
    assert "SECRET-TOKEN-VALUE" not in raw


async def test_the_authorization_header_is_never_logged(client, log_lines):
    await client.get(
        f"{API}/orders",
        headers={"Authorization": "Bearer super-secret-credential", REQUEST_ID_HEADER: "no-auth-0000001"},
    )
    assert "super-secret-credential" not in json.dumps(_for(log_lines(), "no-auth-0000001"))


async def test_health_checks_are_quiet(client, log_lines):
    await client.get("/health", headers={REQUEST_ID_HEADER: "health-poll-00001"})
    line = next(entry for entry in _for(log_lines(), "health-poll-00001") if entry["logger"] == "app.access")
    assert line["level"] == "DEBUG"  # polled every few seconds; not worth an INFO line each


async def test_a_5xx_is_logged_at_error_level(probe_routes, log_lines):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        await http.get("/__probe/boom", headers={REQUEST_ID_HEADER: "five-hundred-0001"})
    access = next(entry for entry in _for(log_lines(), "five-hundred-0001") if entry["logger"] == "app.access")
    assert access["level"] == "ERROR"


# --- the formatter ------------------------------------------------------------------------


def _record(**kwargs) -> logging.LogRecord:
    record = logging.LogRecord("some.logger", logging.WARNING, __file__, 1, "msg %s", ("arg",), None)
    for key, value in kwargs.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_one_valid_object_with_the_core_fields():
    out = JsonFormatter().format(_record())
    assert "\n" not in out
    data = json.loads(out)
    assert data["level"] == "WARNING"
    assert data["logger"] == "some.logger"
    assert data["message"] == "msg arg"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00", data["timestamp"])


def test_extra_fields_become_top_level_keys():
    data = json.loads(JsonFormatter().format(_record(order_id="o-1", amount_cents=500)))
    assert data["order_id"] == "o-1" and data["amount_cents"] == 500


def test_a_message_with_a_newline_stays_on_one_line():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "line1\nINFO forged line", (), None)
    out = JsonFormatter().format(record)
    assert "\n" not in out and json.loads(out)["message"] == "line1\nINFO forged line"


def test_an_unserialisable_extra_does_not_make_logging_raise():
    class Odd:
        def __str__(self):
            return "odd-value"

    data = json.loads(JsonFormatter().format(_record(thing=Odd(), when=uuid.UUID(int=1))))
    assert data["thing"] == "odd-value"


def test_exceptions_are_included_as_a_string():
    try:
        raise ValueError("bad value")
    except ValueError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    data = json.loads(JsonFormatter().format(record))
    assert "ValueError: bad value" in data["exception"] and "Traceback" in data["exception"]


def test_text_formatter_shows_the_request_id_before_any_traceback():
    out = TextFormatter().format(_record(request_id="abc-123-request"))
    assert "[request_id=abc-123-request]" in out and "msg arg" in out


def test_configure_logging_replaces_its_own_handler_instead_of_stacking():
    root = logging.getLogger()
    configure_logging("INFO", "json")
    configure_logging("INFO", "text")
    ours = [h for h in root.handlers if getattr(h, "_app_handler", False)]
    assert len(ours) == 1 and isinstance(ours[0].formatter, TextFormatter)
    configure_logging("INFO", "json")  # restore what the app configured
    assert isinstance(
        [h for h in root.handlers if getattr(h, "_app_handler", False)][0].formatter, JsonFormatter
    )


def test_uvicorns_own_access_log_is_switched_off_and_its_server_log_is_routed_through_ours():
    configure_logging("INFO", "json")
    assert logging.getLogger("uvicorn.access").disabled is True
    error_logger = logging.getLogger("uvicorn.error")
    assert error_logger.propagate is True and not error_logger.handlers


# --- background jobs ----------------------------------------------------------------------


async def test_log_lines_from_a_job_carry_its_id_and_type(log_lines):
    from app.core.logging_config import bound_context, new_context

    with bound_context(new_context(job_id="42", job_type="send_email")):
        probe_logger.info("inside a job")
    probe_logger.info("outside a job")

    inside = next(entry for entry in log_lines() if entry["message"] == "inside a job")
    outside = next(entry for entry in log_lines() if entry["message"] == "outside a job")
    assert inside["job_id"] == "42" and inside["job_type"] == "send_email"
    assert "job_id" not in outside


async def test_the_worker_stamps_the_job_on_every_line_a_handler_logs(
    db_session, test_session_factory, monkeypatch, log_lines
):
    from app.core.config import get_settings
    from app.core.jobs import JobQueue, registry
    from app.core.jobs.worker import run_one

    async def handler(payload, session_factory):
        probe_logger.info("handler running")

    monkeypatch.setitem(registry._handlers, "log_probe", handler)
    job_id = await JobQueue(db_session).enqueue("log_probe", {})
    await db_session.commit()

    assert await run_one(test_session_factory, get_settings()) is True

    handler_line = next(entry for entry in log_lines() if entry["message"] == "handler running")
    assert handler_line["job_id"] == str(job_id) and handler_line["job_type"] == "log_probe"
    done = next(entry for entry in log_lines() if "succeeded" in entry["message"])
    assert done["job_id"] == str(job_id)


def test_uvicorns_duplicate_copy_of_a_logged_exception_is_dropped():
    from app.core.logging_config import DropAlreadyLogged, mark_logged

    def record_for(exc):
        return logging.LogRecord(
            "uvicorn.error", logging.ERROR, __file__, 1, "Exception in ASGI application",
            (), (type(exc), exc, None),
        )  # fmt: skip

    logged, unlogged = RuntimeError("a"), RuntimeError("b")
    mark_logged(logged)
    assert DropAlreadyLogged().filter(record_for(logged)) is False
    assert DropAlreadyLogged().filter(record_for(unlogged)) is True  # never hide an unseen error
    assert DropAlreadyLogged().filter(logging.LogRecord("x", 20, "", 1, "plain", (), None)) is True


async def test_the_catch_all_handler_marks_the_exception_it_logged(probe_routes):
    boom_error.__dict__.pop("_already_logged", None)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        await http.get("/__probe/boom")
    assert boom_error._already_logged is True


def test_uvicorn_error_logger_has_the_filter_installed():
    from app.core.logging_config import DropAlreadyLogged

    configure_logging("INFO", "json")
    configure_logging("INFO", "json")  # idempotent: not stacked
    filters = [f for f in logging.getLogger("uvicorn.error").filters if isinstance(f, DropAlreadyLogged)]
    assert len(filters) == 1
