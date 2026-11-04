"""Tells the storefront which cached reads are stale, AFTER a change is committed (contract: docs/REVALIDATE_HOOK.md).

Never raises and never makes a request wait: the data is already committed, and the storefront's own short cache lifetimes are the safety
net. The call is signed (HMAC-SHA256 over "<timestamp>.<raw body>") so the storefront accepts it from the backend only."""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

TAG_PATTERN = re.compile(r"^(site-config|catalogue|store-settings|product:\d+|category:\d+)$")
MAX_TAGS = 50
RETRY_DELAYS = (0.2, 0.6, 1.5)  # seconds between attempts: 4 attempts in all

_background: set[asyncio.Task] = set()


def clean_tags(tags) -> list[str]:
    """Only tags the storefront accepts, de-duplicated and capped. Over the cap, the broad tags win (a precise one would be dropped anyway)."""
    valid = sorted({t for t in tags if isinstance(t, str) and TAG_PATTERN.match(t)})
    if len(valid) <= MAX_TAGS:
        return valid
    broad = [t for t in valid if ":" not in t]
    return broad[:MAX_TAGS] or ["catalogue"]


def sign(secret: str, timestamp: str, body: str) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()


async def notify_frontend(tags, *, client: httpx.AsyncClient | None = None, delays=RETRY_DELAYS) -> bool:
    """One signed POST with retries. Returns whether the storefront acknowledged; never raises."""
    settings = get_settings()
    url, secret = settings.frontend_revalidate_url, settings.frontend_revalidate_secret
    tags = clean_tags(tags)
    if not url or secret is None or not tags:
        return False
    body = json.dumps({"tags": tags}, separators=(",", ":"))
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=settings.frontend_revalidate_timeout_seconds)
    try:
        for attempt in range(len(delays) + 1):
            timestamp = str(int(time.time()))  # fresh each attempt: the storefront refuses stale timestamps
            headers = {"content-type": "application/json", "x-nc-timestamp": timestamp, "x-nc-signature": sign(secret.get_secret_value(), timestamp, body)}
            try:
                response = await client.post(url, content=body, headers=headers)
                if response.status_code == 200:
                    return True
                if response.status_code in (400, 401):  # a refusal that retrying cannot fix (bad secret, bad tag): say so loudly, once
                    logger.error("Storefront refused the cache hook (HTTP %s): check FRONTEND_REVALIDATE_SECRET and the tag list", response.status_code)
                    return False
                logger.warning("Storefront cache hook answered HTTP %s (attempt %s)", response.status_code, attempt + 1)
            except httpx.HTTPError as exc:
                logger.warning("Storefront cache hook failed (attempt %s): %s", attempt + 1, type(exc).__name__)
            if attempt < len(delays):
                await asyncio.sleep(delays[attempt])
        logger.error("Storefront cache hook gave up for %s: its own cache lifetimes will catch up", tags)
        return False
    except Exception:  # noqa: BLE001  (a bug here must never reach the request that already committed)
        logger.exception("Storefront cache hook crashed")
        return False
    finally:
        if owns_client:
            await client.aclose()


def schedule_notify(tags) -> None:
    """Fire and forget from inside a running request: the write that caused it does not wait."""
    if not get_settings().frontend_revalidate_url or not clean_tags(tags):
        return
    try:
        task = asyncio.get_running_loop().create_task(notify_frontend(tags))
    except RuntimeError:  # no running loop (a script): nothing to schedule on
        return
    _background.add(task)
    task.add_done_callback(_background.discard)
