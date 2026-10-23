"""the store currency is fixed once it has been set; payments.currency loses its USD default

Revision ID: c3f9b1d7a846
Revises: a7c1e4d9b352
Create Date: 2026-10-23 15:00:00.000000

Policy: a store chooses its currency once, at initial setup, and it never changes afterwards. Until now the database allowed
a change while no order existed; it now refuses ANY change to an existing row (the row can still be removed while no order
exists: a database-level reset of a store with no history, never an application operation).

payments.currency no longer defaults to 'usd': a payment must state its currency, so a code path that forgets it fails loudly
instead of silently recording US dollars. Existing rows are untouched; nothing is rewritten.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3f9b1d7a846"
down_revision: Union[str, Sequence[str], None] = "a7c1e4d9b352"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STORE_CURRENCY_GUARD_SQL = r'''CREATE OR REPLACE FUNCTION store_currency_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.currency IS DISTINCT FROM OLD.currency THEN
        RAISE EXCEPTION 'the store currency is fixed once it has been set'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' AND EXISTS (SELECT 1 FROM orders) THEN
        RAISE EXCEPTION 'the store currency cannot be removed once orders exist'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''

PREVIOUS_STORE_CURRENCY_GUARD_SQL = r'''CREATE OR REPLACE FUNCTION store_currency_guard() RETURNS trigger AS $$
BEGIN
    IF (TG_OP = 'DELETE' OR NEW.currency IS DISTINCT FROM OLD.currency)
       AND EXISTS (SELECT 1 FROM orders) THEN
        RAISE EXCEPTION 'store currency cannot be changed once orders exist'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''


def upgrade() -> None:
    op.execute(STORE_CURRENCY_GUARD_SQL)
    op.alter_column("payments", "currency", existing_type=sa.String(length=3), existing_nullable=False, server_default=None)


def downgrade() -> None:
    op.alter_column("payments", "currency", existing_type=sa.String(length=3), existing_nullable=False, server_default="usd")
    op.execute(PREVIOUS_STORE_CURRENCY_GUARD_SQL)
