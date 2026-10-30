"""Context for the stock ledger. The database writes a ledger row for EVERY stock change (see variant_guard.py); this tells it WHY.

The context is transaction-local (it disappears at commit or rollback) and is cleared right after each use, so one change's reason can
never leak onto the next. A change made without context is still recorded, as kind 'unspecified': the application never produces one
(tests assert it), so an 'unspecified' row means someone changed stock outside the application."""

import uuid
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

KINDS = ("initial", "sale", "restock", "admin_set", "admin_delta", "conversion")


async def _set(db: AsyncSession, kind: str, actor: uuid.UUID | None, reason: str | None, order_id: uuid.UUID | None) -> None:
    await db.execute(
        text(
            "SELECT set_config('novacart.stock_kind', :kind, true), set_config('novacart.stock_actor', :actor, true), "
            "set_config('novacart.stock_reason', :reason, true), set_config('novacart.stock_order', :order, true)"
        ),
        {"kind": kind, "actor": str(actor) if actor else "", "reason": reason or "", "order": str(order_id) if order_id else ""},
    )


@asynccontextmanager
async def stock_change(
    db: AsyncSession, kind: str, *, actor_user_id: uuid.UUID | None = None, reason: str | None = None, order_id: uuid.UUID | None = None
):
    """Wrap the statement(s) that change stock: `async with stock_change(db, "sale", ...): await repo.adjust_stock_many(...)`.
    The ORM may defer the UPDATE, so callers must flush inside the block.

    `db` may be an AsyncSession or a repository that has one (`.db`). Anything without a database session (a unit-test fake) has no
    ledger to write to, so the block simply runs."""
    if kind not in KINDS:
        raise ValueError(f"unknown stock change kind {kind!r}")
    db = getattr(db, "db", db)
    if not hasattr(db, "execute"):  # no database session behind it (a unit-test fake): nothing to record
        yield
        return
    await _set(db, kind, actor_user_id, reason, order_id)
    try:
        yield
    finally:
        await _set(db, "", None, None, None)
