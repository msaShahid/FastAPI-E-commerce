"""
Redis response cache for the public category and product lists.

How it stays correct
--------------------
Every cached "namespace" (``categories``, ``products``) has a version number
in Redis. A cache entry is stored as ``"<version>|<payload>"`` and is only
served while its version equals the namespace's current version. Invalidating
a namespace is therefore ONE ``INCR`` -- no key scanning, no deleting -- and
every entry written under the old version is dead from that moment.

The order of operations is what makes this race-free:

* A reader reads the version BEFORE it queries the database, and tags what it
  stores with that version. If a writer commits and bumps the version while
  the reader is still working, the reader's (possibly stale) result is stored
  under the old version and is never served.
* Writers bump the version AFTER their transaction commits (see
  ``flush_pending`` and ``get_db``). Bumping before the commit would let a
  reader re-fill the cache from the not-yet-committed old data.

The cache is fail-open: any Redis error (down, slow, refused) makes the
request fall through to the database, and after a failure Redis is skipped
for a short cooldown so a dead Redis does not add a timeout to every request.
"""

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

CATEGORIES = "categories"
PRODUCTS = "products"

HIT = "HIT"
MISS = "MISS"
BYPASS = "BYPASS"  # cache not used for this request (down, or not cacheable)
DISABLED = "DISABLED"  # caching is switched off; no X-Cache header is sent

_FAILURES = (RedisError, OSError, TimeoutError)


def _initial_version() -> int:
    # Microseconds since the epoch. A version only ever grows by one per write,
    # so a clock-based start stays ahead of any earlier counter unless writes
    # exceeded a million per second.
    return time.time_ns() // 1000


class Cache:
    def __init__(
        self,
        client: Redis | None,
        *,
        prefix: str = "shop",
        failure_cooldown: float = 5.0,
    ) -> None:
        # client=None means caching is switched off.
        self._client = client
        self._prefix = prefix
        self._failure_cooldown = failure_cooldown
        self._down_until = 0.0
        # Invalidations that could not reach Redis; replayed once it is back.
        self._missed: set[str] = set()
        # Concurrent identical misses share one database query.
        self._inflight: dict[str, asyncio.Future[str | None]] = {}

    @classmethod
    def from_settings(cls, config: Settings) -> "Cache":
        if not config.cache_enabled:
            return cls.disabled()
        client = Redis.from_url(
            config.redis_url,
            decode_responses=True,
            socket_timeout=config.cache_redis_timeout_seconds,
            socket_connect_timeout=config.cache_redis_timeout_seconds,
        )
        return cls(
            client,
            prefix=config.cache_key_prefix,
            failure_cooldown=config.cache_failure_cooldown_seconds,
        )

    @classmethod
    def disabled(cls) -> "Cache":
        return cls(None)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    # --- keys -------------------------------------------------------------

    def _version_key(self, namespace: str) -> str:
        return f"{self._prefix}:cache:ver:{namespace}"

    def _entry_key(self, namespace: str, params: dict[str, Any]) -> str:
        digest = hashlib.sha256(
            json.dumps(params, sort_keys=True, default=str).encode()
        ).hexdigest()[:32]
        return f"{self._prefix}:cache:{namespace}:{digest}"

    # --- failure handling -------------------------------------------------

    def _available(self) -> bool:
        return self._client is not None and time.monotonic() >= self._down_until

    def _record_failure(self, exc: BaseException) -> None:
        if self._down_until <= time.monotonic():
            logger.warning("Cache unavailable, falling back to the database: %r", exc)
        self._down_until = time.monotonic() + self._failure_cooldown

    # --- invalidation -----------------------------------------------------

    async def invalidate(self, *namespaces: str) -> None:
        """Make every cached entry in these namespaces unusable. Never raises."""
        if self._client is None or not namespaces:
            return
        self._missed.update(namespaces)
        if not self._available():
            return  # replayed by _replay_missed once Redis answers again
        await self._replay_missed()

    async def _replay_missed(self) -> None:
        assert self._client is not None
        for namespace in list(self._missed):
            key = self._version_key(namespace)
            try:
                # SET NX first: INCR on a missing key would restart the
                # counter at 1, and an old entry tagged "1" could match again.
                async with self._client.pipeline(transaction=False) as pipe:
                    pipe.set(key, _initial_version(), nx=True)
                    pipe.incr(key)
                    await pipe.execute()
            except _FAILURES as exc:
                self._record_failure(exc)
                return
            self._missed.discard(namespace)

    # --- reads ------------------------------------------------------------

    async def _read(self, version_key: str, entry_key: str) -> tuple[int, str | None]:
        """Returns (current version, payload if a valid entry exists)."""
        assert self._client is not None
        raw_version, raw_entry = await self._client.mget(version_key, entry_key)
        if raw_version is None:
            # Version key missing (first use, flush, or eviction). Start it at
            # the current time in microseconds, which is larger than any version
            # it could have had before, so entries from before the loss can
            # never match.
            await self._client.set(version_key, _initial_version(), nx=True)
            return int(await self._client.get(version_key)), None
        version = int(raw_version)
        prefix = f"{version}|"
        if raw_entry is not None and raw_entry.startswith(prefix):
            return version, raw_entry[len(prefix) :]
        return version, None

    async def get_or_set(
        self,
        namespace: str,
        params: dict[str, Any],
        ttl_seconds: int,
        loader: Callable[[], Awaitable[str]],
        *,
        cacheable: bool = True,
    ) -> tuple[str, str]:
        """
        Returns (payload, status). ``loader`` produces the payload (a JSON
        string) from the database; it is called on a miss, when Redis is
        unavailable, or when the request is not cacheable.
        """
        if self._client is None:
            return await loader(), DISABLED
        if not cacheable or not self._available():
            return await loader(), BYPASS

        version_key = self._version_key(namespace)
        entry_key = self._entry_key(namespace, params)

        try:
            if self._missed:
                await self._replay_missed()
                if not self._available():
                    return await loader(), BYPASS
            version, payload = await self._read(version_key, entry_key)
        except _FAILURES as exc:
            self._record_failure(exc)
            return await loader(), BYPASS

        if payload is not None:
            return payload, HIT

        # Single flight, keyed by version: a request that arrives after a
        # write has bumped the version must not share a result that was
        # read before that write.
        flight_key = f"{entry_key}:{version}"
        pending = self._inflight.get(flight_key)
        if pending is not None:
            shared = await asyncio.shield(pending)
            if shared is not None:
                return shared, MISS
            return await loader(), MISS  # the leader failed; do our own query

        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        self._inflight[flight_key] = future
        payload: str | None = None
        try:
            payload = await loader()
        finally:
            # On failure (including cancellation) followers get None and run
            # their own query instead of inheriting the error.
            future.set_result(payload)
            self._inflight.pop(flight_key, None)

        try:
            assert self._client is not None
            await self._client.set(
                entry_key, f"{version}|{payload}", ex=ttl_seconds
            )
        except _FAILURES as exc:
            self._record_failure(exc)
        return payload, MISS

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def cache_headers(cache_status: str) -> dict[str, str]:
    """Response headers for a get_or_set status (X-Cache says what happened)."""
    return {} if cache_status == DISABLED else {"X-Cache": cache_status}


# --- process-wide instance ----------------------------------------------------

_cache: Cache | None = None


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache.from_settings(get_settings())
    return _cache


def set_cache(cache: Cache | None) -> None:
    """Replace the process-wide cache (tests, and shutdown)."""
    global _cache
    _cache = cache


async def close_cache() -> None:
    cache = _cache
    set_cache(None)
    if cache is not None:
        await cache.close()


# --- post-commit invalidation ---------------------------------------------------
#
# Writers do not call the cache. Instead a SQLAlchemy hook notices, on every
# flush, which tables changed and remembers the affected namespaces on the
# session. Whoever COMMITS the session then calls flush_pending() (get_db does
# this for every request; scripts that commit themselves do it explicitly).
# This covers every write path -- REST, GraphQL, checkout stock changes, the
# stale-order job -- without each one having to remember.

_PENDING_KEY = "cache_pending_namespaces"

# table name -> namespaces whose cached entries embed rows of that table.
# A product list embeds its category, so category edits stale both.
_TABLE_NAMESPACES: dict[str, tuple[str, ...]] = {
    "products": (PRODUCTS,),
    # A product page embeds its variants' prices and availability, so they stale the same cached lists.
    "product_variants": (PRODUCTS,),
    "product_options": (PRODUCTS,),
    "product_option_values": (PRODUCTS,),
    "categories": (CATEGORIES, PRODUCTS),
}


@event.listens_for(Session, "after_flush")
def _collect_changed_namespaces(session: Session, flush_context) -> None:
    # In after_flush the new/dirty/deleted collections still describe what
    # was just flushed.
    pending: set[str] = session.info.setdefault(_PENDING_KEY, set())
    for obj in (*session.new, *session.dirty, *session.deleted):
        namespaces = _TABLE_NAMESPACES.get(getattr(obj, "__tablename__", ""))
        if namespaces:
            pending.update(namespaces)


@event.listens_for(Session, "do_orm_execute")
def _collect_bulk_writes(state) -> None:
    # A bulk `update(Product)...` / `delete(...)` / `insert(...)` statement never
    # goes through flush, so the hook above cannot see it. Without this, a bulk
    # stock update would leave stale product lists in the cache.
    if state.is_update or state.is_delete or state.is_insert:
        table = getattr(state.statement, "table", None)
        namespaces = _TABLE_NAMESPACES.get(getattr(table, "name", ""))
        if namespaces:
            state.session.info.setdefault(_PENDING_KEY, set()).update(namespaces)


@event.listens_for(Session, "after_soft_rollback")
def _forget_on_rollback(session: Session, previous_transaction) -> None:
    # Only a rollback of the OUTERMOST transaction discards the writes. A
    # savepoint rollback leaves earlier flushes in place, so keep those marks
    # (invalidating too much is harmless; too little would serve stale data).
    if previous_transaction.parent is None:
        session.info.pop(_PENDING_KEY, None)


async def flush_pending(session: AsyncSession) -> None:
    """
    Call right AFTER committing ``session``. Invalidates what the
    transaction changed. Never raises: the data is already committed, so a
    cache problem must not turn a successful request into an error.
    """
    pending = session.info.pop(_PENDING_KEY, None)
    if not pending:
        return
    try:
        await get_cache().invalidate(*pending)
    except Exception:  # noqa: BLE001
        logger.exception("Cache invalidation failed for %s", sorted(pending))
