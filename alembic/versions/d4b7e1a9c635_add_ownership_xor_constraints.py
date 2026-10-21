"""add the order/cart ownership constraints that the models declared but no migration created

Revision ID: d4b7e1a9c635
Revises: c8e3a5f7b142
Create Date: 2026-10-19 10:00:00.000000

ck_orders_user_xor_guest : an order belongs to EITHER a registered user (guest_token and guest_email NULL) OR a guest
                           (user_id NULL, guest_token and guest_email set).
ck_carts_user_xor_guest  : a cart belongs to EITHER a registered user OR a guest token.
These are the invariants the application already maintains (orders.user_id is ON DELETE RESTRICT and users are
deactivated, never deleted; carts.user_id cascades), so valid data is unaffected.

SAFETY: existing rows are checked FIRST. If any row violates an invariant, this migration STOPS before changing
anything and lists the offending ids: it never deletes data and never invents an owner. Resolve them by hand (see
`python -m scripts.check_ownership_violations`), then rerun. The constraints are added NOT VALID and then VALIDATED, which
keeps the lock short on a live table. A database created from the models already has them: those are skipped.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4b7e1a9c635"
down_revision: Union[str, Sequence[str], None] = "c8e3a5f7b142"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ORDERS_CHECK = (
    "(user_id IS NOT NULL AND guest_token IS NULL AND guest_email IS NULL) OR "
    "(user_id IS NULL AND guest_token IS NOT NULL AND guest_email IS NOT NULL)"
)
CARTS_CHECK = "(user_id IS NOT NULL AND guest_token IS NULL) OR (user_id IS NULL AND guest_token IS NOT NULL)"


def _exists(conn, table: str, name: str) -> bool:
    return bool(conn.execute(sa.text(
        "SELECT 1 FROM pg_constraint WHERE conname = :n AND conrelid = CAST(:t AS regclass)"), {"n": name, "t": table}).first())


def _violations(conn, table: str, check: str) -> list:
    rows = conn.execute(sa.text(f"SELECT id FROM {table} WHERE NOT ({check}) LIMIT 11")).all()  # noqa: S608 (constants)
    return [str(r[0]) for r in rows]


def upgrade() -> None:
    conn = op.get_bind()
    problems = []
    for table, name, check in (("orders", "ck_orders_user_xor_guest", ORDERS_CHECK),
                               ("carts", "ck_carts_user_xor_guest", CARTS_CHECK)):
        if not _exists(conn, table, name):
            bad = _violations(conn, table, check)
            if bad:
                problems.append(f"{table}: {len(bad)}{'+' if len(bad) > 10 else ''} row(s) violate {name}, e.g. {bad[:10]}")
    if problems:
        raise RuntimeError(
            "Refusing to add the ownership constraints: existing rows violate them. NOTHING was changed. "
            + " | ".join(problems)
            + ". Review and fix these rows by hand (run `python -m scripts.check_ownership_violations` for the full list); "
            "this migration never deletes data or guesses an owner."
        )
    for table, name, check in (("orders", "ck_orders_user_xor_guest", ORDERS_CHECK),
                               ("carts", "ck_carts_user_xor_guest", CARTS_CHECK)):
        if not _exists(conn, table, name):
            op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({check}) NOT VALID")
            op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")


def downgrade() -> None:
    op.execute("ALTER TABLE carts DROP CONSTRAINT IF EXISTS ck_carts_user_xor_guest")
    op.execute("ALTER TABLE orders DROP CONSTRAINT IF EXISTS ck_orders_user_xor_guest")
