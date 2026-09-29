import pytest

from app.core.security import decode_token
from app.modules.auth.exceptions.auth_exceptions import (
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    UsernameAlreadyExistsError,
)
from app.modules.auth.services.auth_service import AuthService


@pytest.fixture
def auth_service(fake_repository) -> AuthService:
    return AuthService(fake_repository)


async def test_register_creates_user_with_hashed_password(auth_service):
    user = await auth_service.register(
        username="shahid", email="shahid@example.com", password="supersecret123"
    )
    assert user.username == "shahid"
    assert user.email == "shahid@example.com"
    # The core guarantee: whatever got stored is NOT the plain password.
    assert user.password_hash != "supersecret123"


async def test_register_rejects_duplicate_email(auth_service):
    await auth_service.register(
        username="first", email="dup@example.com", password="password123"
    )
    with pytest.raises(EmailAlreadyExistsError):
        await auth_service.register(
            username="second", email="dup@example.com", password="password123"
        )


async def test_register_rejects_duplicate_username(auth_service):
    await auth_service.register(
        username="dupuser", email="a@example.com", password="password123"
    )
    with pytest.raises(UsernameAlreadyExistsError):
        await auth_service.register(
            username="dupuser", email="b@example.com", password="password123"
        )


async def test_login_succeeds_with_correct_credentials(auth_service):
    user = await auth_service.register(
        username="shahid", email="shahid@example.com", password="supersecret123"
    )
    tokens = await auth_service.login(
        email="shahid@example.com", password="supersecret123"
    )

    payload = decode_token(tokens.access_token)
    assert payload["sub"] == str(user.id)
    assert payload["type"] == "access"


async def test_login_persists_refresh_token(auth_service, fake_repository):
    await auth_service.register(
        username="shahid", email="shahid@example.com", password="supersecret123"
    )
    await auth_service.login(email="shahid@example.com", password="supersecret123")

    assert len(fake_repository.refresh_tokens) == 1


async def test_login_rejects_wrong_password(auth_service):
    await auth_service.register(
        username="shahid", email="shahid@example.com", password="correct-password"
    )
    with pytest.raises(InvalidCredentialsError):
        await auth_service.login(email="shahid@example.com", password="wrong-password")


async def test_login_rejects_nonexistent_email_with_same_error_as_wrong_password(
    auth_service,
):

    with pytest.raises(InvalidCredentialsError):
        await auth_service.login(email="nobody@example.com", password="whatever123")


async def test_refresh_issues_new_valid_access_token(auth_service):
    await auth_service.register(
        username="shahid", email="shahid@example.com", password="pass1234"
    )
    tokens = await auth_service.login(email="shahid@example.com", password="pass1234")

    new_tokens = await auth_service.refresh(refresh_token=tokens.refresh_token)

    payload = decode_token(new_tokens.access_token)
    assert payload["type"] == "access"


async def test_refresh_rotates_the_refresh_token(auth_service):

    await auth_service.register(
        username="shahid", email="shahid@example.com", password="pass1234"
    )
    tokens = await auth_service.login(email="shahid@example.com", password="pass1234")

    new_tokens = await auth_service.refresh(refresh_token=tokens.refresh_token)

    assert new_tokens.refresh_token != tokens.refresh_token


async def test_reusing_a_rotated_refresh_token_is_rejected(auth_service):

    await auth_service.register(
        username="shahid", email="shahid@example.com", password="pass1234"
    )
    tokens = await auth_service.login(email="shahid@example.com", password="pass1234")

    await auth_service.refresh(refresh_token=tokens.refresh_token)

    with pytest.raises(InvalidRefreshTokenError):
        await auth_service.refresh(refresh_token=tokens.refresh_token)


async def test_refresh_rejects_an_access_token(auth_service):
    """An access token presented where a refresh token belongs must be rejected."""
    await auth_service.register(
        username="shahid", email="shahid@example.com", password="pass1234"
    )
    tokens = await auth_service.login(email="shahid@example.com", password="pass1234")

    with pytest.raises(InvalidRefreshTokenError):
        await auth_service.refresh(refresh_token=tokens.access_token)


async def test_logout_revokes_the_refresh_token(auth_service):
    await auth_service.register(
        username="shahid", email="shahid@example.com", password="pass1234"
    )
    tokens = await auth_service.login(email="shahid@example.com", password="pass1234")

    await auth_service.logout(refresh_token=tokens.refresh_token)

    # A revoked token can no longer be used to refresh.
    with pytest.raises(InvalidRefreshTokenError):
        await auth_service.refresh(refresh_token=tokens.refresh_token)


async def test_logout_with_garbage_token_does_not_raise(auth_service):

    await auth_service.logout(refresh_token="not-a-real-token")


async def test_login_rejects_deactivated_user(auth_service):

    await auth_service.register(
        username="shahid", email="shahid@example.com", password="pass1234"
    )
    user = await auth_service.repository.get_user_by_email("shahid@example.com")
    user.is_active = False

    with pytest.raises(InvalidCredentialsError):
        await auth_service.login(email="shahid@example.com", password="pass1234")


async def test_refresh_rejects_token_for_a_user_deactivated_after_issuance(
    auth_service,
):

    await auth_service.register(
        username="shahid", email="shahid@example.com", password="pass1234"
    )
    tokens = await auth_service.login(email="shahid@example.com", password="pass1234")

    user = await auth_service.repository.get_user_by_email("shahid@example.com")
    user.is_active = False

    with pytest.raises(InvalidRefreshTokenError):
        await auth_service.refresh(refresh_token=tokens.refresh_token)
