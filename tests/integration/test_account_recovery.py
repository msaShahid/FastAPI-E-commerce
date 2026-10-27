# ruff: noqa: F811  (fixtures imported from the lifecycle suite are re-declared as test parameters)
"""Gap B: password reset, password change, email verification and email change, end to end through the real jobs."""

import re
import uuid

import pytest
from sqlalchemy import text
from test_order_payment_lifecycle import jobs

import app.modules.auth.jobs as auth_jobs
from app.core.config import get_settings

API = "/api/v1"
PASSWORD = "CorrectHorse1!"


class Outbox:
    def __init__(self):
        self.sent = []  # (to, subject, body)

    async def send(self, *, to, subject, body):
        self.sent.append((to, subject, body))

    def to(self, address):
        return [m for m in self.sent if m[0] == address]

    def link_token(self, address, path_fragment):
        mails = [m for m in self.to(address) if path_fragment in m[2]]
        assert mails, f"no email with {path_fragment} was sent to {address}: {[m[1] for m in self.sent]}"
        return re.search(r"token=([A-Za-z0-9_\-%]+)", mails[-1][2]).group(1)


@pytest.fixture
def outbox(monkeypatch):
    box = Outbox()
    monkeypatch.setattr(auth_jobs, "get_email_sender", lambda: box)
    return box


async def register(client, outbox, test_session_factory, name=None):
    name = name or f"u{uuid.uuid4().hex[:8]}"
    email = f"{name}@example.com"
    r = await client.post(f"{API}/auth/register", json={"username": name, "email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    await jobs(test_session_factory)  # the verification email
    return name, email, r.json()


async def login(client, email, password=PASSWORD):
    r = await client.post(f"{API}/auth/login", json={"email": email, "password": password})
    return r


async def me(client, tokens):
    return (await client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})).json()


def auth(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# ============================================================================================== verification
async def test_registering_sends_a_verification_link_that_works_once_and_never_blocks_signing_in(client, outbox, test_session_factory):
    _, email, created = await register(client, outbox, test_session_factory)
    assert created["email_verified"] is False  # sign-in and buying do not wait for it
    tokens = (await login(client, email)).json()
    assert (await me(client, tokens))["email_verified"] is False

    link = outbox.link_token(email, "/verify-email")
    assert outbox.to(email)[0][2].count(get_settings().frontend_base_url + "/verify-email?token=") == 1
    assert (await client.post(f"{API}/auth/email/verify", json={"token": link})).status_code == 204
    assert (await me(client, tokens))["email_verified"] is True
    again = await client.post(f"{API}/auth/email/verify", json={"token": link})
    assert again.status_code == 400 and again.json()["code"] == "invalid_or_expired_token"  # a link works once


async def test_verification_links_expire_and_are_bound_to_one_purpose_and_the_address(client, outbox, test_session_factory, db_session):
    _, email, _ = await register(client, outbox, test_session_factory)
    token = outbox.link_token(email, "/verify-email")
    # wrong purpose: a verification link cannot reset a password
    wrong = await client.post(f"{API}/auth/password/reset", json={"token": token, "new_password": "Another1234!"})
    assert wrong.status_code == 400
    # expired
    await db_session.execute(text("UPDATE auth_tokens SET expires_at = now() - interval '1 minute'"))
    await db_session.commit()
    assert (await client.post(f"{API}/auth/email/verify", json={"token": token})).status_code == 400
    # garbage
    assert (await client.post(f"{API}/auth/email/verify", json={"token": "x" * 40})).status_code == 400


async def test_only_a_hash_of_the_secret_is_stored_and_the_secret_is_never_in_the_job_queue(client, outbox, test_session_factory, db_session):
    _, email, _ = await register(client, outbox, test_session_factory)
    token = outbox.link_token(email, "/verify-email")
    await db_session.rollback()
    stored = (await db_session.execute(text("SELECT token_hash FROM auth_tokens"))).scalar_one()
    assert stored != token and len(stored) == 64
    assert token not in (await db_session.execute(text("SELECT coalesce(string_agg(payload::text, ' '), '') FROM background_jobs"))).scalar_one()


# ============================================================================================== forgot / reset
async def test_forgot_password_answers_identically_for_known_and_unknown_addresses_and_only_emails_real_accounts(
    client, outbox, test_session_factory
):
    _, email, _ = await register(client, outbox, test_session_factory)
    known = await client.post(f"{API}/auth/password/forgot", json={"email": email})
    unknown = await client.post(f"{API}/auth/password/forgot", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 202 and known.json() == unknown.json()
    await jobs(test_session_factory)
    assert [m for m in outbox.to(email) if "/reset-password" in m[2]]
    assert outbox.to("nobody@example.com") == []  # nothing is sent to a stranger's address


async def test_an_inactive_account_gets_no_reset_email(client, outbox, test_session_factory, db_session):
    _, email, _ = await register(client, outbox, test_session_factory)
    await db_session.execute(text("UPDATE users SET is_active = false WHERE email = :e"), {"e": email})
    await db_session.commit()
    await client.post(f"{API}/auth/password/forgot", json={"email": email})
    await jobs(test_session_factory)
    assert not [m for m in outbox.to(email) if "/reset-password" in m[2]]


async def test_the_number_of_reset_emails_per_account_per_hour_is_capped(client, outbox, test_session_factory, db_session):
    _, email, _ = await register(client, outbox, test_session_factory)
    for _ in range(get_settings().password_reset_emails_per_hour):
        await db_session.execute(text(
            "INSERT INTO auth_tokens (id, user_id, purpose, token_hash, email, expires_at) "
            "SELECT gen_random_uuid(), id, 'password_reset', md5(random()::text) || md5(random()::text), email, now() + interval '1 hour' "
            "FROM users WHERE email = :e"), {"e": email})
    await db_session.commit()
    await client.post(f"{API}/auth/password/forgot", json={"email": email})
    await jobs(test_session_factory)
    assert not [m for m in outbox.to(email) if "/reset-password" in m[2]]


async def test_a_reset_sets_the_password_signs_everything_out_notifies_and_works_once(client, outbox, test_session_factory):
    _, email, _ = await register(client, outbox, test_session_factory)
    session_a = (await login(client, email)).json()
    session_b = (await login(client, email)).json()
    await client.post(f"{API}/auth/password/forgot", json={"email": email})
    await jobs(test_session_factory)
    token = outbox.link_token(email, "/reset-password")

    done = await client.post(f"{API}/auth/password/reset", json={"token": token, "new_password": "BrandNew-Pass9"})
    assert done.status_code == 204
    assert (await login(client, email, "BrandNew-Pass9")).status_code == 200
    assert (await login(client, email, PASSWORD)).status_code == 401  # the old password is gone
    for old in (session_a, session_b):  # every existing session is signed out
        assert (await client.post(f"{API}/auth/refresh", json={"refresh_token": old["refresh_token"]})).status_code == 401
    assert (await client.post(f"{API}/auth/password/reset", json={"token": token, "new_password": "Third-Pass-123"})).status_code == 400
    await jobs(test_session_factory)
    assert any("password was changed" in m[1] for m in outbox.to(email))  # the owner is told
    tokens = (await login(client, email, "BrandNew-Pass9")).json()
    assert (await me(client, tokens))["email_verified"] is True  # opening a link sent to the address proves it is theirs


async def test_a_reset_link_dies_if_the_account_email_changes_after_it_was_sent(client, outbox, test_session_factory, db_session):
    _, email, _ = await register(client, outbox, test_session_factory)
    await client.post(f"{API}/auth/password/forgot", json={"email": email})
    await jobs(test_session_factory)
    token = outbox.link_token(email, "/reset-password")
    await db_session.execute(text("UPDATE users SET email = 'moved@example.com' WHERE email = :e"), {"e": email})
    await db_session.commit()
    assert (await client.post(f"{API}/auth/password/reset", json={"token": token, "new_password": "BrandNew-Pass9"})).status_code == 400


async def test_a_weak_new_password_is_refused_and_does_not_use_up_the_link(client, outbox, test_session_factory):
    _, email, _ = await register(client, outbox, test_session_factory)
    await client.post(f"{API}/auth/password/forgot", json={"email": email})
    await jobs(test_session_factory)
    token = outbox.link_token(email, "/reset-password")
    assert (await client.post(f"{API}/auth/password/reset", json={"token": token, "new_password": "short"})).status_code == 422
    assert (await client.post(f"{API}/auth/password/reset", json={"token": token, "new_password": "BrandNew-Pass9"})).status_code == 204


async def test_forgot_password_is_rate_limited(client):
    codes = [(await client.post(f"{API}/auth/password/forgot", json={"email": "x@example.com"})).status_code for _ in range(6)]
    assert codes[:5] == [202] * 5 and codes[5] == 429


# ============================================================================================== change password
async def test_changing_the_password_needs_the_current_one_signs_out_other_devices_and_keeps_this_one(
    client, outbox, test_session_factory
):
    _, email, _ = await register(client, outbox, test_session_factory)
    here = (await login(client, email)).json()
    elsewhere = (await login(client, email)).json()

    wrong = await client.post(f"{API}/auth/password/change", headers=auth(here), json={"current_password": "nope-nope-no", "new_password": "BrandNew-Pass9"})
    assert wrong.status_code == 400 and wrong.json()["code"] == "current_password_incorrect"  # 400, not a 401 that would loop a client
    same = await client.post(f"{API}/auth/password/change", headers=auth(here), json={"current_password": PASSWORD, "new_password": PASSWORD})
    assert same.status_code == 400 and same.json()["code"] == "password_unchanged"

    ok = await client.post(f"{API}/auth/password/change", headers=auth(here), json={"current_password": PASSWORD, "new_password": "BrandNew-Pass9"})
    assert ok.status_code == 200
    fresh = ok.json()
    assert (await client.post(f"{API}/auth/refresh", json={"refresh_token": fresh["refresh_token"]})).status_code == 200  # this device carries on
    for old in (here, elsewhere):
        assert (await client.post(f"{API}/auth/refresh", json={"refresh_token": old["refresh_token"]})).status_code == 401
    assert (await login(client, email, "BrandNew-Pass9")).status_code == 200
    await jobs(test_session_factory)
    assert any("password was changed" in m[1] for m in outbox.to(email))


async def test_password_change_requires_being_signed_in(client):
    r = await client.post(f"{API}/auth/password/change", json={"current_password": "a", "new_password": "BrandNew-Pass9"})
    assert r.status_code in (401, 403)


# ============================================================================================== change email
async def test_an_email_change_needs_the_password_is_confirmed_from_the_new_address_and_notifies_the_old(
    client, outbox, test_session_factory
):
    _, email, _ = await register(client, outbox, test_session_factory)
    tokens = (await login(client, email)).json()
    new = f"new{uuid.uuid4().hex[:6]}@example.com"

    bad = await client.post(f"{API}/auth/email/change", headers=auth(tokens), json={"new_email": new, "password": "wrong-wrong-1"})
    assert bad.status_code == 400
    ok = await client.post(f"{API}/auth/email/change", headers=auth(tokens), json={"new_email": new, "password": PASSWORD})
    assert ok.status_code == 202
    await jobs(test_session_factory)
    assert (await me(client, tokens))["email"] == email  # nothing changed yet
    assert any("Confirm your new email" in m[1] for m in outbox.to(new))  # the link goes to the NEW address ...
    assert any("Email change requested" in m[1] for m in outbox.to(email))  # ... and the old one is warned

    token = outbox.link_token(new, "/confirm-email-change")
    assert (await client.post(f"{API}/auth/email/change/confirm", json={"token": token})).status_code == 204
    profile = await me(client, tokens)
    assert profile["email"] == new and profile["email_verified"] is True
    assert (await login(client, new)).status_code == 200 and (await login(client, email)).status_code == 401
    await jobs(test_session_factory)
    assert any("was changed" in m[1] for m in outbox.to(email))  # the old address is told it happened
    assert (await client.post(f"{API}/auth/email/change/confirm", json={"token": token})).status_code == 400  # once


async def test_an_email_change_to_an_address_already_in_use_reveals_nothing_and_sends_nothing(client, outbox, test_session_factory):
    _, email, _ = await register(client, outbox, test_session_factory)
    _, other, _ = await register(client, outbox, test_session_factory)
    tokens = (await login(client, email)).json()
    taken = await client.post(f"{API}/auth/email/change", headers=auth(tokens), json={"new_email": other, "password": PASSWORD})
    free = await client.post(f"{API}/auth/email/change", headers=auth(tokens), json={"new_email": "free@example.com", "password": PASSWORD})
    assert taken.status_code == free.status_code == 202 and taken.json() == free.json()
    sent_before = len(outbox.sent)
    await jobs(test_session_factory)
    assert not [m for m in outbox.sent[sent_before:] if m[0] == other and "Confirm your new email" in m[1]]


async def test_confirming_loses_to_an_account_that_took_the_address_in_the_meantime(client, outbox, test_session_factory, db_session):
    _, email, _ = await register(client, outbox, test_session_factory)
    tokens = (await login(client, email)).json()
    await client.post(f"{API}/auth/email/change", headers=auth(tokens), json={"new_email": "race@example.com", "password": PASSWORD})
    await jobs(test_session_factory)
    token = outbox.link_token("race@example.com", "/confirm-email-change")
    await register(client, outbox, test_session_factory, name="race")  # race@example.com registers first
    r = await client.post(f"{API}/auth/email/change/confirm", json={"token": token})
    assert r.status_code == 409
    assert (await me(client, tokens))["email"] == email


async def test_profile_edits_can_no_longer_change_your_own_email_but_an_admin_can_correct_someone_elses(
    client, outbox, test_session_factory, admin_headers
):
    _, email, created = await register(client, outbox, test_session_factory)
    tokens = (await login(client, email)).json()
    own = await client.patch(f"{API}/users/{created['id']}", headers=auth(tokens), json={"email": "sneaky@example.com"})
    assert own.status_code == 403 and "POST /auth/email/change" in own.json()["detail"]
    assert (await client.patch(f"{API}/users/{created['id']}", headers=auth(tokens), json={"username": "renamed_ok"})).status_code == 200

    corrected = await client.patch(f"{API}/users/{created['id']}", headers=admin_headers, json={"email": "corrected@example.com"})
    assert corrected.status_code == 200
    assert (await login(client, "corrected@example.com")).status_code == 200
    t2 = (await login(client, "corrected@example.com")).json()
    assert (await me(client, t2))["email_verified"] is False  # an address nobody has confirmed is not verified


async def test_account_actions_are_audited(client, outbox, test_session_factory, db_session):
    _, email, _ = await register(client, outbox, test_session_factory)
    tokens = (await login(client, email)).json()
    await client.post(f"{API}/auth/password/change", headers=auth(tokens), json={"current_password": PASSWORD, "new_password": "BrandNew-Pass9"})
    await db_session.rollback()
    actions = {r[0] for r in (await db_session.execute(text("SELECT action FROM audit_logs"))).all()}
    assert "auth.password_changed" in actions
