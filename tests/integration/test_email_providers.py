# ruff: noqa: F811  (fixtures imported from the lifecycle suite are re-declared as test parameters)
"""Gap: the administrator chooses and configures the email service (Google, Zoho, Mailtrap) and can switch at any time."""

import asyncio
import logging
import smtplib
import uuid

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import text
from test_order_payment_lifecycle import jobs

import app.modules.auth.jobs  # noqa: F401  (registers the email jobs)
from app.core.config import get_settings

API = "/api/v1"
BASE = f"{API}/admin/email-providers"
SECRET_PASSWORD = "app-password-ABCD1234efgh"
SECRET_USERNAME = "shopmailer@gmail.com"


class FakeSmtp:
    log: list = []
    fail_login = None
    fail_send = None
    fail_connect = None

    def __init__(self, host, port, timeout=None, context=None):
        if type(self).fail_connect:
            raise type(self).fail_connect
        self.log.append(("connect", host, port, type(self).__name__))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.log.append(("starttls",))

    def login(self, user, password):
        if type(self).fail_login:
            raise type(self).fail_login
        self.log.append(("login", user, password))

    def send_message(self, message):
        if type(self).fail_send:
            raise type(self).fail_send
        self.log.append(("send", message))


@pytest.fixture
def smtp(monkeypatch):
    FakeSmtp.log, FakeSmtp.fail_login, FakeSmtp.fail_send, FakeSmtp.fail_connect = [], None, None, None
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", type("FakeSmtpSsl", (FakeSmtp,), {}))
    return FakeSmtp


@pytest.fixture(autouse=True)
def secrets_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "provider_secrets_key", SecretStr(Fernet.generate_key().decode()))


def google(**over):
    return {"username": SECRET_USERNAME, "password": SECRET_PASSWORD, "from_email": SECRET_USERNAME, "from_name": "Aipower Store", **over}


async def put(client, headers, provider, body):
    return await client.put(f"{BASE}/{provider}", json=body, headers=headers)


async def effective(client, headers):
    return (await client.get(BASE, headers=headers)).json()["effective"]


async def active_count(db):
    await db.rollback()
    return (await db.execute(text("SELECT count(*) FROM email_provider_configs WHERE is_active"))).scalar_one()


async def register(client, test_session_factory):
    name = f"u{uuid.uuid4().hex[:8]}"
    r = await client.post(f"{API}/auth/register", json={"username": name, "email": f"{name}@example.com", "password": "CorrectHorse1!"})
    assert r.status_code == 201, r.text
    await jobs(test_session_factory)
    return f"{name}@example.com"


# ================================================================================================== access
async def test_only_administrators_can_see_or_change_email_settings(client, admin_headers):
    assert (await client.get(BASE)).status_code in (401, 403)
    await client.post(f"{API}/auth/register", json={"username": "plain", "email": "plain@example.com", "password": "CorrectHorse1!"})
    tok = (await client.post(f"{API}/auth/login", json={"email": "plain@example.com", "password": "CorrectHorse1!"})).json()["access_token"]
    user = {"Authorization": f"Bearer {tok}"}
    for call in (client.get(BASE, headers=user), client.put(f"{BASE}/google", json=google(), headers=user),
                 client.post(f"{BASE}/google/activate", headers=user), client.delete(f"{BASE}/google", headers=user),
                 client.post(f"{BASE}/google/test-email", headers=user)):
        assert (await call).status_code == 403
    assert (await client.get(BASE, headers=admin_headers)).status_code == 200


async def test_the_catalog_lists_the_three_services_with_fixed_hosts_and_setup_notes(client, admin_headers):
    cat = {c["provider"]: c for c in (await client.get(f"{BASE}/catalog", headers=admin_headers)).json()}
    assert set(cat) == {"google", "zoho", "mailtrap"}
    assert cat["google"]["host_by_mode"] == {"default": "smtp.gmail.com"} and cat["google"]["modes"] is None
    assert {"in", "com", "eu"} <= set(cat["zoho"]["modes"]) and cat["zoho"]["host_by_mode"]["in"] == "smtp.zoho.in"
    assert set(cat["mailtrap"]["modes"]) == {"sandbox", "live"}
    assert any("App password" in n for n in cat["google"]["notes"])
    assert [f["name"] for f in cat["google"]["fields"] if f["secret"]] == ["password"]


async def test_nothing_is_configured_at_first_and_the_environment_applies(client, admin_headers):
    body = (await client.get(BASE, headers=admin_headers)).json()
    assert body["effective"]["source"] == "environment" and body["effective"]["provider"] is None
    assert [p["configured"] for p in body["providers"]] == [False, False, False]


# ================================================================================================== saving credentials
async def test_credentials_are_encrypted_write_only_and_never_appear_anywhere(client, admin_headers, db_session, caplog):
    with caplog.at_level(logging.DEBUG):
        r = await put(client, admin_headers, "google", google(from_email="orders@aipower.in"))  # the public From differs from the login
        listing = await client.get(BASE, headers=admin_headers)
    assert r.status_code == 200, r.text
    for text_ in (r.text, listing.text):
        assert SECRET_PASSWORD not in text_ and SECRET_USERNAME not in text_ and "ciphertext" not in text_
    body = r.json()
    assert body["password_set"] is True and body["from_email"] == "orders@aipower.in"
    assert body["password_hint"].startswith("...") and body["username_hint"].startswith("s***@")
    await db_session.rollback()
    cipher, hint = (await db_session.execute(text("SELECT credentials_ciphertext, password_hint FROM email_provider_configs"))).one()
    assert SECRET_PASSWORD not in cipher and SECRET_USERNAME not in cipher and cipher.startswith("k1:")
    assert SECRET_PASSWORD not in caplog.text
    audit = (await db_session.execute(text("SELECT coalesce(string_agg(details::text, ' '), '') FROM audit_logs WHERE action LIKE 'email_provider.%'"))).scalar_one()
    assert SECRET_PASSWORD not in audit and SECRET_USERNAME not in audit and "email_provider.saved" in (
        await db_session.execute(text("SELECT string_agg(action, ' ') FROM audit_logs"))).scalar_one()


async def test_saving_needs_the_credential_storage_key_instead_of_storing_anything_in_the_clear(client, admin_headers, monkeypatch, db_session):
    monkeypatch.setattr(get_settings(), "provider_secrets_key", None)
    r = await put(client, admin_headers, "google", google())
    assert r.status_code == 503 and r.json()["code"] == "secret_storage_not_configured"
    await db_session.rollback()
    assert (await db_session.execute(text("SELECT count(*) FROM email_provider_configs"))).scalar_one() == 0


async def test_settings_are_validated_and_the_host_can_never_be_supplied(client, admin_headers):
    assert (await put(client, admin_headers, "google", google(host="internal.example"))).status_code == 422    # no host/port field exists
    assert (await put(client, admin_headers, "google", google(port=25))).status_code == 422
    assert (await put(client, admin_headers, "google", google(mode="eu"))).status_code == 422                  # google has no modes
    assert (await put(client, admin_headers, "zoho", google())).status_code == 422                             # zoho needs a data centre
    assert (await put(client, admin_headers, "zoho", google(mode="mars"))).status_code == 422
    assert (await put(client, admin_headers, "google", google(from_name="Evil\r\nBcc: x@example.com"))).status_code == 422
    assert (await put(client, admin_headers, "google", google(from_email="not-an-email"))).status_code == 422
    assert (await put(client, admin_headers, "google", {"from_email": SECRET_USERNAME, "from_name": "S"})).status_code == 422  # first save needs both
    assert (await put(client, admin_headers, "yahoo", google())).status_code == 422                            # not a provider


async def test_mailtrap_live_defaults_its_username_and_sandbox_is_flagged_as_not_delivering(client, admin_headers):
    live = await put(client, admin_headers, "mailtrap", {"mode": "live", "password": "token-1234567890", "from_email": "orders@example.com", "from_name": "S"})
    assert live.status_code == 200 and live.json()["username_hint"].startswith("a") and live.json()["delivers_to_real_recipients"] is True
    sandbox = await put(client, admin_headers, "mailtrap", {"mode": "sandbox", "username": "abc123", "password": "pw-1234567890", "from_email": "orders@example.com", "from_name": "S"})
    body = sandbox.json()
    assert body["delivers_to_real_recipients"] is False and any("NOT delivered" in w for w in body["warnings"])


async def test_updating_without_a_password_keeps_the_stored_one(client, admin_headers, smtp):
    await put(client, admin_headers, "google", google())
    again = await put(client, admin_headers, "google", {"from_email": SECRET_USERNAME, "from_name": "Renamed Store"})
    assert again.status_code == 200 and again.json()["from_name"] == "Renamed Store"
    await client.post(f"{BASE}/google/test", headers=admin_headers)
    assert ("login", SECRET_USERNAME, SECRET_PASSWORD) in smtp.log


# ================================================================================================== testing a service
@pytest.mark.parametrize(("provider", "extra", "host", "port"), [
    ("google", {}, "smtp.gmail.com", 587),
    ("zoho", {"mode": "in"}, "smtp.zoho.in", 587),
    ("zoho", {"mode": "com"}, "smtp.zoho.com", 587),
    ("mailtrap", {"mode": "sandbox"}, "sandbox.smtp.mailtrap.io", 2525),
    ("mailtrap", {"mode": "live"}, "live.smtp.mailtrap.io", 587),
])
async def test_each_service_connects_to_its_own_fixed_host_and_port_over_starttls(client, admin_headers, smtp, provider, extra, host, port):
    assert (await put(client, admin_headers, provider, google(**extra))).status_code == 200
    r = await client.post(f"{BASE}/{provider}/test", headers=admin_headers)
    assert r.json()["ok"] is True, r.text
    assert smtp.log[0][:3] == ("connect", host, port) and ("starttls",) in smtp.log
    assert not any(e[0] == "send" for e in smtp.log)  # a connection test sends nothing


@pytest.mark.parametrize(("failure", "code"), [
    (smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted for shopmailer@gmail.com"), "auth_failed"),
    (smtplib.SMTPServerDisconnected("down"), "connection_failed"),
])
async def test_a_failed_test_reports_a_fixed_code_and_never_the_servers_own_words(client, admin_headers, smtp, db_session, failure, code):
    await put(client, admin_headers, "google", google())
    smtp.fail_login = failure
    r = (await client.post(f"{BASE}/google/test", headers=admin_headers)).json()
    assert r["ok"] is False and r["error_code"] == code and SECRET_USERNAME not in r["message"] and "5.7.8" not in r["message"]
    state = (await client.get(BASE, headers=admin_headers)).json()["providers"][0]
    assert state["last_test_ok"] is False and state["last_test_error"] == code and state["last_test_at"]


async def test_the_test_email_goes_to_the_administrators_own_address_only(client, admin_headers, smtp):
    await put(client, admin_headers, "google", google())
    me = (await client.get(f"{API}/auth/me", headers=admin_headers)).json()["email"]
    r = (await client.post(f"{BASE}/google/test-email", headers=admin_headers, json={"to": "victim@example.com"})).json()
    assert r["ok"] is True
    sent = [e[1] for e in smtp.log if e[0] == "send"]
    assert len(sent) == 1 and sent[0]["To"] == me  # a recipient in the body is ignored
    assert SECRET_PASSWORD not in r["message"]


# ================================================================================================== activating and switching
async def test_a_service_cannot_be_activated_until_it_passes_a_live_test(client, admin_headers, smtp, db_session):
    await put(client, admin_headers, "google", google())
    smtp.fail_login = smtplib.SMTPAuthenticationError(535, b"nope")
    r = await client.post(f"{BASE}/google/activate", headers=admin_headers)
    assert r.status_code == 422 and r.json()["code"] == "email_provider_test_failed" and "rejected the username or password" in r.json()["detail"]
    assert await active_count(db_session) == 0 and (await effective(client, admin_headers))["source"] == "environment"
    smtp.fail_login = None
    ok = await client.post(f"{BASE}/google/activate", headers=admin_headers)
    assert ok.status_code == 200 and ok.json()["is_active"] is True and ok.json()["last_test_ok"] is True
    assert (await effective(client, admin_headers))["provider"] == "google"


async def test_switching_service_replaces_the_active_one_and_the_next_email_uses_the_new_one(client, admin_headers, smtp, test_session_factory, db_session):
    await put(client, admin_headers, "google", google())
    await put(client, admin_headers, "zoho", google(mode="in", username="mailer@aipower.in", from_email="mailer@aipower.in"))
    await client.post(f"{BASE}/google/activate", headers=admin_headers)
    first = await register(client, test_session_factory)
    hosts = [e[1] for e in smtp.log if e[0] == "connect"]
    assert hosts[-1] == "smtp.gmail.com"
    assert [e[1]["To"] for e in smtp.log if e[0] == "send"][-1] == first and "Aipower Store" in [e[1]["From"] for e in smtp.log if e[0] == "send"][-1]

    r = await client.post(f"{BASE}/zoho/activate", headers=admin_headers)
    assert r.status_code == 200 and await active_count(db_session) == 1
    state = {p["provider"]: p["is_active"] for p in (await client.get(BASE, headers=admin_headers)).json()["providers"]}
    assert state == {"google": False, "zoho": True, "mailtrap": False}
    second = await register(client, test_session_factory)  # switching takes effect immediately, no restart
    assert [e[1] for e in smtp.log if e[0] == "connect"][-1] == "smtp.zoho.in"
    assert [e[1]["To"] for e in smtp.log if e[0] == "send"][-1] == second

    back = await client.post(f"{BASE}/google/activate", headers=admin_headers)  # and back again
    assert back.status_code == 200 and (await effective(client, admin_headers))["provider"] == "google"


async def test_two_administrators_switching_at_once_still_leave_exactly_one_active(client, admin_headers, smtp, db_session):
    await put(client, admin_headers, "google", google())
    await put(client, admin_headers, "mailtrap", google(mode="live"))
    results = await asyncio.gather(client.post(f"{BASE}/google/activate", headers=admin_headers),
                                   client.post(f"{BASE}/mailtrap/activate", headers=admin_headers))
    assert all(r.status_code in (200, 409) for r in results) and any(r.status_code == 200 for r in results)
    assert await active_count(db_session) == 1


async def test_deactivating_returns_to_the_environment_settings(client, admin_headers, smtp):
    await put(client, admin_headers, "google", google())
    await client.post(f"{BASE}/google/activate", headers=admin_headers)
    body = (await client.post(f"{BASE}/deactivate", headers=admin_headers)).json()
    assert body["effective"]["source"] == "environment" and all(p["is_active"] is False for p in body["providers"])
    assert body["providers"][0]["configured"] is True  # the saved settings are kept


async def test_editing_the_active_services_credentials_is_tested_first_and_refused_if_it_fails(client, admin_headers, smtp, db_session):
    await put(client, admin_headers, "google", google())
    await client.post(f"{BASE}/google/activate", headers=admin_headers)
    await db_session.rollback()
    before = (await db_session.execute(text("SELECT credentials_ciphertext FROM email_provider_configs"))).scalar_one()
    smtp.fail_login = smtplib.SMTPAuthenticationError(535, b"bad")
    r = await put(client, admin_headers, "google", {"password": "a-typo-password-1", "from_email": SECRET_USERNAME, "from_name": "S"})
    assert r.status_code == 422 and r.json()["code"] == "email_provider_test_failed"
    await db_session.rollback()
    assert (await db_session.execute(text("SELECT credentials_ciphertext FROM email_provider_configs"))).scalar_one() == before  # untouched
    assert (await effective(client, admin_headers))["provider"] == "google"
    smtp.fail_login = None
    assert (await put(client, admin_headers, "google", {"password": "the-right-password-2", "from_email": SECRET_USERNAME, "from_name": "S"})).status_code == 200


async def test_the_active_service_cannot_be_deleted_but_an_inactive_one_can(client, admin_headers, smtp):
    await put(client, admin_headers, "google", google())
    await put(client, admin_headers, "mailtrap", google(mode="sandbox"))
    await client.post(f"{BASE}/google/activate", headers=admin_headers)
    blocked = await client.delete(f"{BASE}/google", headers=admin_headers)
    assert blocked.status_code == 409 and blocked.json()["code"] == "email_provider_active"
    assert (await client.delete(f"{BASE}/mailtrap", headers=admin_headers)).status_code == 204
    assert (await client.delete(f"{BASE}/mailtrap", headers=admin_headers)).status_code == 404


# ================================================================================================== what the worker records
async def test_a_failing_service_is_visible_to_the_administrator_without_reading_logs(client, admin_headers, smtp, test_session_factory):
    await put(client, admin_headers, "google", google())
    await client.post(f"{BASE}/google/activate", headers=admin_headers)
    smtp.fail_send = smtplib.SMTPServerDisconnected("connection lost")
    await register(client, test_session_factory)  # the email job fails (and will retry)
    state = (await client.get(BASE, headers=admin_headers)).json()["providers"][0]
    assert state["last_error_code"] == "connection_failed" and state["last_error_at"] and state["last_sent_at"] is None
    smtp.fail_send = None
    await register(client, test_session_factory)
    state = (await client.get(BASE, headers=admin_headers)).json()["providers"][0]
    assert state["last_sent_at"] and state["last_error_code"] is None  # a success clears the failure


async def test_with_no_active_service_the_environment_sender_is_still_used(client, admin_headers, test_session_factory, monkeypatch):
    sent = []

    class Box:
        async def send(self, *, to, subject, body):
            sent.append(to)

    import app.modules.auth.jobs as auth_jobs

    monkeypatch.setattr(auth_jobs, "get_email_sender", lambda: Box())
    await put(client, admin_headers, "google", google())  # configured, but not activated
    email = await register(client, test_session_factory)
    assert email in sent  # (the admin fixture's own verification email may be in the list too)


async def test_an_unreadable_stored_credential_fails_the_job_loudly_instead_of_using_another_account(client, admin_headers, smtp, test_session_factory, monkeypatch):
    await put(client, admin_headers, "google", google())
    await client.post(f"{BASE}/google/activate", headers=admin_headers)
    monkeypatch.setattr(get_settings(), "provider_secrets_key", SecretStr(Fernet.generate_key().decode()))  # the key was lost/changed
    smtp.log.clear()
    await register(client, test_session_factory)
    assert not [e for e in smtp.log if e[0] == "send"]  # nothing went out through any account
