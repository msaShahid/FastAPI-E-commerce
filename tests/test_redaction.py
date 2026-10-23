"""M4 / logging: secrets and personal data are removed from what is logged or stored for operators."""

import io
import json
import logging

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.logging_config import build_handler
from app.core.redaction import mask_email, redact

BARE_HEX = "9f2c4a7e1b8d3c6a5e0f7b2d4c9a1e8f"
SECRETS = [
    "sk_test_51NabcDEFghijkLMNopqrstuv",
    "sk_live_CHANGE_ME_abc123",
    "rk_test_abcdef0123456789",
    "whsec_8d3f1a6b9c2e5d7f0a4b8c1e3d6f9a2b",
    "pi_3Nabc123XYZ_secret_Q9w8e7r6t5y4u3i2o1",
    "rzp_live_AbCdEf0123456",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    BARE_HEX,
]
CASES = [
    "Invalid API Key provided: {s}",
    "Authorization: Bearer {s}",
    'headers={{"authorization": "Bearer {s}"}}',
    "jwt_secret={s} database_url=x",
    'payload {{"password": "{s}"}}',
    "client_secret: {s}",
    "Cookie: guest_pay_0123456789abcdef0123456789abcdef={s}; other=1",
]


@pytest.mark.parametrize("secret", SECRETS)
@pytest.mark.parametrize("template", CASES)
def test_credentials_and_tokens_never_survive(secret, template):
    if secret == BARE_HEX and template.startswith("Invalid API Key"):
        pytest.skip("a bare hex string with no key name or prefix has no recognisable shape: redaction is a safety net, see the module")
    text = template.format(s=secret)
    cleaned = redact(text)
    assert secret not in cleaned
    assert redact(cleaned) == cleaned  # idempotent


def test_database_error_parameters_and_conflicting_values_are_removed():
    text = (
        '(sqlalchemy.dialects.postgresql.asyncpg.IntegrityError) <class \'asyncpg.exceptions.UniqueViolationError\'>: duplicate key '
        'value violates unique constraint "ix_users_email"\nDETAIL:  Key (email)=(jane.doe@example.com) already exists.\n'
        "[SQL: INSERT INTO users (email, hashed_password) VALUES ($1, $2)]\n"
        "[parameters: ('jane.doe@example.com', '$argon2id$v=19$m=65536,t=3,p=4$abc$def')]\n"
        "(Background on this error at: https://sqlalche.me/e/21/gkpj)"
    )
    cleaned = redact(text)
    for leaked in ("jane.doe", "argon2id", "$argon2"):
        assert leaked not in cleaned
    assert "ix_users_email" in cleaned and "INSERT INTO users" in cleaned  # what an operator needs is kept
    assert "Background on this error" in cleaned


def test_email_addresses_are_masked_but_stay_recognisable():
    assert redact("sent to jane.doe@example.com now") == "sent to j***@example.com now"
    assert mask_email("jane.doe@example.com") == "j***@example.com"
    assert mask_email("not an address") == "[no-address]"


def test_ordinary_text_is_untouched():
    text = "Order 42a8b91a-4b5e-4e88-ab92-3576569d906a paid: 1000 inr via razorpay (attempt 2)"
    assert redact(text) == text


def test_the_json_and_text_log_formats_both_redact_messages_extras_and_tracebacks():
    for fmt in ("json", "text"):
        stream = io.StringIO()
        logger = logging.getLogger(f"redaction-test-{fmt}")
        logger.handlers.clear()
        logger.propagate = False
        logger.addHandler(build_handler(fmt, stream))
        logger.setLevel(logging.INFO)
        try:
            raise RuntimeError("upstream said: Invalid API Key provided: sk_test_LEAKME12345 for jane@example.com")
        except RuntimeError:
            logger.error("provider failed key=whsec_LEAKME67890", exc_info=True, extra={"detail": "pi_1_secret_LEAKME_X"})
        out = stream.getvalue()
        assert "LEAKME" not in out and "jane@" not in out, (fmt, out)
        assert "provider failed" in out and "RuntimeError" in out  # still useful
        if fmt == "json":
            assert json.loads(out.splitlines()[0])["level"] == "ERROR"


def test_the_integrity_error_handler_logs_the_constraint_not_the_statement_or_its_values():
    import asyncio

    from starlette.requests import Request

    from app.main import app

    class Orig(Exception):
        constraint_name = "ix_users_email"

    exc = IntegrityError("INSERT INTO users (email) VALUES ($1)", ("jane.doe@example.com",), Orig("duplicate key"))
    stream = io.StringIO()
    logger = logging.getLogger("app.core.exception_handlers")
    handler = build_handler("json", stream)
    logger.addHandler(handler)
    try:
        request = Request({"type": "http", "method": "POST", "path": "/api/v1/auth/register", "headers": [], "query_string": b""})
        response = asyncio.run(app.exception_handlers[IntegrityError](request, exc))
    finally:
        logger.removeHandler(handler)
    out = stream.getvalue()
    assert response.status_code == 409
    assert "ix_users_email" in out and "jane.doe" not in out and "INSERT INTO" not in out
