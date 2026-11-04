# ruff: noqa: F811  (fixtures imported from other suites are re-declared as test parameters)
"""The refresh-token reuse grace window: a replay of a just-rotated token (two tabs, a retry, two instances) is a benign race and gets
its own sibling token; reuse outside the window, too many replays, or reuse after the family was shut down is still theft."""

from datetime import UTC, datetime, timedelta

import pytest
from test_auth_service import auth_service  # noqa: F401  (fixture)

from app.core.config import get_settings
from app.core.security import decode_token
from app.modules.auth.exceptions.auth_exceptions import InvalidRefreshTokenError


@pytest.fixture
async def session(auth_service):  # noqa: F811
    await auth_service.register(username="shahid", email="shahid@example.com", password="pass1234")
    return await auth_service.login(email="shahid@example.com", password="pass1234")


def _stored(repo, token):
    return repo.refresh_tokens[decode_token(token)["jti"]]


class RecordingAudit:
    def __init__(self):
        self.entries = []

    async def log(self, **entry):
        self.entries.append(entry)


async def test_a_replay_inside_the_window_gets_its_own_new_token_and_the_family_survives(auth_service, fake_repository, session):
    first = await auth_service.refresh(refresh_token=session.refresh_token)
    second = await auth_service.refresh(refresh_token=session.refresh_token)  # the same old token, a moment later (another tab)
    assert second.refresh_token not in (first.refresh_token, session.refresh_token)
    assert _stored(fake_repository, first.refresh_token).family_id == _stored(fake_repository, second.refresh_token).family_id
    # both children keep working: nobody was signed out
    await auth_service.refresh(refresh_token=first.refresh_token)
    await auth_service.refresh(refresh_token=second.refresh_token)


async def test_a_replay_is_audited(auth_service, fake_repository, session):
    audit = RecordingAudit()
    await auth_service.refresh(refresh_token=session.refresh_token, audit=audit)
    assert audit.entries == []  # an ordinary rotation is not an event
    await auth_service.refresh(refresh_token=session.refresh_token, audit=audit)
    (entry,) = audit.entries
    assert entry["action"] == "auth.refresh_replay_accepted" and entry["target_type"] == "refresh_token"
    assert entry["details"]["replays"] == 1


async def test_after_the_window_a_replay_is_theft_again(auth_service, fake_repository, session):
    first = await auth_service.refresh(refresh_token=session.refresh_token)
    _stored(fake_repository, session.refresh_token).rotated_at = datetime.now(UTC) - timedelta(seconds=get_settings().refresh_reuse_grace_seconds + 5)
    with pytest.raises(InvalidRefreshTokenError):
        await auth_service.refresh(refresh_token=session.refresh_token)
    with pytest.raises(InvalidRefreshTokenError):  # and the whole family is shut down
        await auth_service.refresh(refresh_token=first.refresh_token)


async def test_only_a_limited_number_of_replays_are_forgiven(auth_service, session):
    limit = get_settings().refresh_reuse_grace_max_replays
    await auth_service.refresh(refresh_token=session.refresh_token)
    for _ in range(limit):
        await auth_service.refresh(refresh_token=session.refresh_token)
    with pytest.raises(InvalidRefreshTokenError):
        await auth_service.refresh(refresh_token=session.refresh_token)


async def test_a_token_revoked_by_logout_is_never_a_benign_replay(auth_service, session):
    await auth_service.logout(refresh_token=session.refresh_token)
    with pytest.raises(InvalidRefreshTokenError):
        await auth_service.refresh(refresh_token=session.refresh_token)


async def test_replaying_after_the_family_was_shut_down_does_not_revive_it(auth_service, fake_repository, session):
    first = await auth_service.refresh(refresh_token=session.refresh_token)
    await fake_repository.revoke_family(_stored(fake_repository, first.refresh_token).family_id)  # e.g. a password change elsewhere
    with pytest.raises(InvalidRefreshTokenError):
        await auth_service.refresh(refresh_token=session.refresh_token)


async def test_the_window_can_be_switched_off(auth_service, session, strict_refresh):
    await auth_service.refresh(refresh_token=session.refresh_token)
    with pytest.raises(InvalidRefreshTokenError):
        await auth_service.refresh(refresh_token=session.refresh_token)
