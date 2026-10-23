import logging
import os
from collections.abc import Mapping

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import HARDENED_ENVIRONMENTS, get_settings

logger = logging.getLogger(__name__)

settings = get_settings()


def build_limiter(redis_url: str, fallback: str) -> Limiter:
    """Keyed by client IP address (`request.client.host`).

    Behind a reverse proxy that address is the PROXY's unless uvicorn is told to trust the proxy's X-Forwarded-For
    (FORWARDED_ALLOW_IPS=<the proxy's address>; see warn_if_forwarded_headers_untrusted). Left untrusted, every client shares one
    bucket, so one noisy client locks everyone out. Trusted too widely (FORWARDED_ALLOW_IPS=*), any client can claim any address.

    storage_uri points slowapi at Redis so the counters are shared by every worker process and replica. If Redis is unreachable
    the limiter does NOT turn requests into 500s: it falls back to per-process in-memory counters with the `fallback` limit
    (applied to every limited route) until Redis is back. The short socket timeouts bound how long a dead Redis can stall a request.
    """
    return Limiter(
        key_func=get_remote_address,
        storage_uri=redis_url,
        storage_options={"socket_connect_timeout": 1, "socket_timeout": 1},
        in_memory_fallback_enabled=True,
        in_memory_fallback=[fallback],
    )


limiter = build_limiter(settings.redis_url, settings.rate_limit_fallback)


def checkout_limit() -> str:
    return get_settings().rate_limit_checkout


def payment_limit() -> str:
    return get_settings().rate_limit_payment


def cart_limit() -> str:
    return get_settings().rate_limit_cart


def warn_if_forwarded_headers_untrusted(environ: Mapping[str, str] | None = None) -> bool:
    """True (and a warning) when a staging/production process starts with no FORWARDED_ALLOW_IPS: uvicorn then trusts
    X-Forwarded-For only from 127.0.0.1, so behind a proxy on another address every client looks like the proxy."""
    env = os.environ if environ is None else environ
    if get_settings().environment not in HARDENED_ENVIRONMENTS or env.get("FORWARDED_ALLOW_IPS"):
        return False
    logger.warning(
        "FORWARDED_ALLOW_IPS is not set. Behind a reverse proxy on another address every client will share one rate-limit bucket. "
        "Set it to the proxy's address (never '*' unless the API is reachable ONLY through the proxy)."
    )
    return True
