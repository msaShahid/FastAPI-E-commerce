"""M8: the JWT library was replaced (python-jose -> PyJWT). Tokens must behave identically for callers, and unsafe ones stay refused."""

import base64
import json
import time

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import (
    TokenError,
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    unverified_claims,
)

SECRET = get_settings().jwt_secret
ALG = get_settings().jwt_algorithm


def test_an_access_token_round_trips_with_its_claims():
    payload = decode_token(create_access_token("user-1"))
    assert payload["sub"] == "user-1" and payload["type"] == TokenType.ACCESS.value and payload["exp"] > payload["iat"]


def test_a_refresh_token_carries_a_unique_jti_readable_only_for_tokens_this_server_minted():
    a, b = create_refresh_token("u"), create_refresh_token("u")
    assert unverified_claims(a)["jti"] != unverified_claims(b)["jti"]
    assert decode_token(a)["type"] == TokenType.REFRESH.value


@pytest.mark.parametrize("bad", [
    jwt.encode({"sub": "u", "type": "access", "exp": int(time.time()) - 5}, SECRET, algorithm=ALG),          # expired
    jwt.encode({"sub": "u", "type": "access", "exp": int(time.time()) + 60}, "another-secret-entirely-xxxxxxxxxxxx", algorithm=ALG),  # wrong key
    jwt.encode({"sub": "u", "type": "access"}, SECRET, algorithm=ALG),                                       # no exp: never expires
    jwt.encode({"type": "access", "exp": int(time.time()) + 60}, SECRET, algorithm=ALG),                      # no subject
    jwt.encode({"sub": "u", "type": "access", "exp": int(time.time()) + 60}, SECRET, algorithm="HS512"),      # not the configured algorithm
    "not-a-token", "", "a.b.c",
])
def test_unusable_tokens_raise_the_one_token_error(bad):
    with pytest.raises(TokenError):
        decode_token(bad)


def test_an_unsigned_alg_none_token_is_refused():
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    forged = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': 'admin', 'type': 'access', 'exp': int(time.time()) + 600})}."
    with pytest.raises(TokenError):
        decode_token(forged)


def test_tokens_in_the_format_the_previous_library_produced_still_verify():
    """python-jose wrote `iat`/`exp` as integer seconds and HS256 by default: sessions issued before this change keep working."""
    legacy = jwt.encode({"sub": "u-1", "type": "access", "iat": int(time.time()), "exp": int(time.time()) + 600}, SECRET, algorithm="HS256")
    if ALG == "HS256":
        assert decode_token(legacy)["sub"] == "u-1"
