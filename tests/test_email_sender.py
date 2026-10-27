"""The SMTP sender, its settings validation, and the account email texts (no network: smtplib is replaced by a recorder)."""

import smtplib

import pytest

from app.core.config import ConfigurationError, Settings
from app.core.email import interface
from app.core.email.interface import LoggingEmailSender, SmtpEmailSender, get_email_sender

BASE = dict(database_url="postgresql+asyncpg://u:p@db/x", jwt_secret="x", stripe_secret_key="sk_test_x", stripe_webhook_secret="whsec_x")
SMTP = dict(BASE, email_backend="smtp", smtp_host="smtp.example.com", email_from="orders@example.com", smtp_username="u", smtp_password="p")


class FakeSmtp:
    log = []

    def __init__(self, host, port, timeout=None, context=None):
        self.log.append(("connect", host, port, timeout, type(self).__name__))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.log.append(("starttls",))

    def login(self, user, password):
        self.log.append(("login", user))

    def send_message(self, message):
        self.log.append(("send", message))


@pytest.fixture(autouse=True)
def fake_smtplib(monkeypatch):
    FakeSmtp.log = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", type("FakeSmtpSsl", (FakeSmtp,), {}))
    return FakeSmtp


async def test_a_message_goes_out_over_starttls_with_login_and_the_right_headers(fake_smtplib):
    sender = SmtpEmailSender(Settings(_env_file=None, **SMTP))
    await sender.send(to="jane@example.com", subject="Hello", body="Line one\nLine two")
    kinds = [e[0] for e in fake_smtplib.log]
    assert kinds == ["connect", "starttls", "login", "send"]
    message = fake_smtplib.log[-1][1]
    assert message["To"] == "jane@example.com" and message["Subject"] == "Hello"
    assert "orders@example.com" in message["From"] and "NovaCart Store" in message["From"]
    assert "Line one" in message.get_content()


async def test_ssl_mode_uses_a_direct_tls_connection_and_none_skips_starttls(fake_smtplib):
    await SmtpEmailSender(Settings(_env_file=None, **dict(SMTP, smtp_security="ssl", smtp_port=465))).send(to="a@example.com", subject="s", body="b")
    assert fake_smtplib.log[0][4] == "FakeSmtpSsl" and ("starttls",) not in fake_smtplib.log
    fake_smtplib.log.clear()
    await SmtpEmailSender(Settings(_env_file=None, **dict(SMTP, smtp_security="none", smtp_username=None, smtp_password=None))).send(
        to="a@example.com", subject="s", body="b")
    assert [e[0] for e in fake_smtplib.log] == ["connect", "send"]


async def test_a_newline_in_a_header_is_refused_before_anything_is_sent(fake_smtplib):
    sender = SmtpEmailSender(Settings(_env_file=None, **SMTP))
    for to, subject in (("a@example.com", "Hi\r\nBcc: attacker@example.com"), ("a@example.com\r\nBcc: x@example.com", "Hi")):
        with pytest.raises(ValueError):
            await sender.send(to=to, subject=subject, body="b")
    assert fake_smtplib.log == []


async def test_a_delivery_failure_raises_so_the_job_is_retried(fake_smtplib, monkeypatch):
    def boom(self, message):
        raise smtplib.SMTPServerDisconnected("down")

    monkeypatch.setattr(FakeSmtp, "send_message", boom)
    with pytest.raises(smtplib.SMTPServerDisconnected):
        await SmtpEmailSender(Settings(_env_file=None, **SMTP)).send(to="a@example.com", subject="s", body="b")


def test_the_sender_follows_the_configuration(monkeypatch):
    get_email_sender.cache_clear()
    monkeypatch.setattr(interface, "get_settings", lambda: Settings(_env_file=None, **SMTP))
    assert isinstance(get_email_sender(), SmtpEmailSender)
    get_email_sender.cache_clear()
    monkeypatch.setattr(interface, "get_settings", lambda: Settings(_env_file=None, **BASE))
    assert isinstance(get_email_sender(), LoggingEmailSender)
    get_email_sender.cache_clear()


@pytest.mark.parametrize("bad", [
    dict(email_backend="smtp"),                                           # no host / from
    dict(email_backend="smtp", smtp_host="h"),                            # no from
    dict(email_backend="pigeon"),
    dict(smtp_security="plain"),
    dict(smtp_username="u"),                                              # username without a password
])
def test_incoherent_email_settings_are_refused_at_startup(bad):
    with pytest.raises(ConfigurationError):
        Settings(_env_file=None, **dict(BASE, **bad))


@pytest.mark.parametrize("url", ["http://localhost:3000", "http://shop.example.com", "https://localhost", "https://127.0.0.1:3000"])
def test_staging_and_production_need_the_storefronts_public_https_address(url):
    real = dict(BASE, jwt_secret="9f2c4a7e1b8d3c6a5e0f7b2d4c9a1e8f6b3d5c7a9e1f2b4d6c8a0e3f5b7d9c1a",
                stripe_secret_key="sk_test_51Nabcdefghijklmnopqrstuv", stripe_webhook_secret="whsec_8d3f1a6b9c2e5d7f0a4b8c1e3d6f9a2b")
    for env in ("staging", "production"):
        with pytest.raises(ConfigurationError):
            Settings(_env_file=None, environment=env, frontend_base_url=url, **real)
        ok = Settings(_env_file=None, environment=env, frontend_base_url="https://shop.example.com/", **real)
        assert ok.frontend_base_url == "https://shop.example.com"  # trailing slash removed
    assert Settings(_env_file=None, environment="development", frontend_base_url=url, **BASE).frontend_base_url == url


def test_the_logging_sender_hides_message_bodies_where_they_could_leak_a_link(monkeypatch, caplog):
    import logging

    for env, shows in (("development", True), ("testing", True)):
        monkeypatch.setattr(interface, "get_settings", lambda e=env: Settings(_env_file=None, environment=e, **BASE))
        caplog.clear()
        import asyncio

        with caplog.at_level(logging.INFO, logger="app.core.email.interface"):
            asyncio.run(LoggingEmailSender().send(to="jane.doe@example.com", subject="s", body="LINK-TOKEN"))
        assert ("LINK-TOKEN" in caplog.text) is shows and "jane.doe" not in caplog.text
    real = dict(BASE, jwt_secret="9f2c4a7e1b8d3c6a5e0f7b2d4c9a1e8f6b3d5c7a9e1f2b4d6c8a0e3f5b7d9c1a",
                stripe_secret_key="sk_test_51Nabcdefghijklmnopqrstuv", stripe_webhook_secret="whsec_8d3f1a6b9c2e5d7f0a4b8c1e3d6f9a2b",
                frontend_base_url="https://shop.example.com")
    monkeypatch.setattr(interface, "get_settings", lambda: Settings(_env_file=None, environment="production", **real))
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.core.email.interface"):
        asyncio.run(LoggingEmailSender().send(to="jane.doe@example.com", subject="s", body="LINK-TOKEN"))
    assert "LINK-TOKEN" not in caplog.text


@pytest.mark.parametrize(("exc", "code"), [
    (smtplib.SMTPAuthenticationError(535, b"x"), "auth_failed"),
    (smtplib.SMTPSenderRefused(550, b"x", "a@b.c"), "sender_refused"),
    (smtplib.SMTPRecipientsRefused({"a@b.c": (550, b"x")}), "recipient_refused"),
    (smtplib.SMTPNotSupportedError("no starttls"), "tls_unsupported"),
    (smtplib.SMTPServerDisconnected("x"), "connection_failed"),
    (TimeoutError(), "connection_failed"),
    (ConnectionRefusedError(), "connection_failed"),
    (__import__("ssl").SSLError("handshake"), "tls_failed"),
    (ValueError("bad header"), "invalid_message"),
    (smtplib.SMTPDataError(554, b"x"), "smtp_error"),
    (RuntimeError("?"), "unknown"),
])
def test_every_failure_maps_to_a_fixed_safe_code(exc, code):
    from app.core.email.interface import SMTP_ERROR_MESSAGES, smtp_error_code

    assert smtp_error_code(exc) == code and code in SMTP_ERROR_MESSAGES


def test_the_smtp_config_never_prints_its_password():
    from app.core.email.interface import SmtpConfig

    cfg = SmtpConfig("h", 587, "starttls", "user@example.com", "TOP-SECRET", "a@example.com", "Name")
    assert "TOP-SECRET" not in repr(cfg) and "TOP-SECRET" not in str(cfg)
