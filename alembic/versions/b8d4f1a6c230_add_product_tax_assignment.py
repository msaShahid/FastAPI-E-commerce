"""add product tax-classification assignment and unit

Revision ID: b8d4f1a6c230
Revises: f2b6d8a4c917
Create Date: 2026-10-17 19:30:00.000000

Adds products.tax_classification_id (nullable FK, RESTRICT) and products.unit (nullable).
Existing products keep NULL for both: they are "unassigned" and are never treated as
tax-ready. Two triggers keep assignments valid: a product can only be assigned an APPROVED
classification, and a classification still assigned to products cannot be retired.
The function texts must stay identical to guard.PRODUCT_TAX_FUNCTIONS (a test checks).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8d4f1a6c230"
down_revision: Union[str, Sequence[str], None] = "f2b6d8a4c917"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PRODUCT_ASSIGNMENT_GUARD_SQL = r'''CREATE OR REPLACE FUNCTION products_tax_assignment_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.tax_classification_id IS NOT NULL
       AND (TG_OP = 'INSERT' OR NEW.tax_classification_id IS DISTINCT FROM OLD.tax_classification_id) THEN
        -- FOR SHARE: a concurrent retirement of this classification must wait for us and
        -- then sees this product, so a product can never end up on a retired classification.
        PERFORM 1 FROM tax_classifications
            WHERE id = NEW.tax_classification_id AND status = 'approved' FOR SHARE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'tax master: a product can only be assigned an approved classification'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''

CLASSIFICATION_RETIRE_GUARD_SQL = r'''CREATE OR REPLACE FUNCTION tax_classification_retire_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.status = 'retired' AND OLD.status <> 'retired'
       AND EXISTS (SELECT 1 FROM products WHERE tax_classification_id = OLD.id) THEN
        RAISE EXCEPTION 'tax master: a classification still assigned to products cannot be retired'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''


def upgrade() -> None:
    op.add_column("products", sa.Column("tax_classification_id", sa.Integer(), nullable=True))
    op.add_column("products", sa.Column("unit", sa.String(length=8), nullable=True))
    op.create_foreign_key(
        "fk_products_tax_classification_id",
        "products",
        "tax_classifications",
        ["tax_classification_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(op.f("ix_products_tax_classification_id"), "products", ["tax_classification_id"])
    op.create_check_constraint("ck_products_unit_format", "products", "unit IS NULL OR unit ~ '^[A-Z]{2,8}$'")

    op.execute(PRODUCT_ASSIGNMENT_GUARD_SQL)
    op.execute(CLASSIFICATION_RETIRE_GUARD_SQL)
    op.execute(
        "CREATE TRIGGER trg_products_tax_assignment_guard BEFORE INSERT OR UPDATE OF tax_classification_id "
        "ON products FOR EACH ROW EXECUTE FUNCTION products_tax_assignment_guard()"
    )
    op.execute(
        "CREATE TRIGGER trg_tax_classification_retire_guard BEFORE UPDATE OF status "
        "ON tax_classifications FOR EACH ROW EXECUTE FUNCTION tax_classification_retire_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_tax_classification_retire_guard ON tax_classifications")
    op.execute("DROP TRIGGER IF EXISTS trg_products_tax_assignment_guard ON products")
    op.execute("DROP FUNCTION IF EXISTS tax_classification_retire_guard()")
    op.execute("DROP FUNCTION IF EXISTS products_tax_assignment_guard()")
    op.drop_constraint("ck_products_unit_format", "products", type_="check")
    op.drop_index(op.f("ix_products_tax_classification_id"), table_name="products")
    op.drop_constraint("fk_products_tax_classification_id", "products", type_="foreignkey")
    op.drop_column("products", "unit")
    op.drop_column("products", "tax_classification_id")
