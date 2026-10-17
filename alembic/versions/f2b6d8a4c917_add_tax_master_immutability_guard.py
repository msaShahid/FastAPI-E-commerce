"""add database-level immutability guard for the GST master tables

Revision ID: f2b6d8a4c917
Revises: e7a3c9b1d845
Create Date: 2026-10-17 18:30:00.000000

Adds a trigger to tax_classifications, tax_rules and gst_registrations so approved
records cannot be rewritten or deleted by any code path (see
app/modules/tax/models/guard.py for the rules). The three tables were created empty by
the previous revision, so no existing data is evaluated by the trigger.
The function text below must stay identical to guard.GUARD_FUNCTION_SQL (a test checks).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f2b6d8a4c917"
down_revision: Union[str, Sequence[str], None] = "e7a3c9b1d845"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("tax_classifications", "tax_rules", "gst_registrations")

GUARD_FUNCTION_SQL = r'''CREATE OR REPLACE FUNCTION tax_master_guard() RETURNS trigger AS $$
DECLARE
    skip text[] := ARRAY['updated_at', 'created_by_user_id', 'approved_by_user_id'];
    old_j jsonb;
    new_j jsonb;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN
            RAISE EXCEPTION 'tax master: a % record must start as a draft', TG_TABLE_NAME
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'tax master: % records cannot be deleted once approved', TG_TABLE_NAME
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;

    -- UPDATE. Who created/approved a non-draft record may be cleared (a deleted user)
    -- but never reassigned.
    IF OLD.status <> 'draft' THEN
        IF (NEW.created_by_user_id IS NOT NULL
                AND NEW.created_by_user_id IS DISTINCT FROM OLD.created_by_user_id)
           OR (NEW.approved_by_user_id IS NOT NULL
                AND NEW.approved_by_user_id IS DISTINCT FROM OLD.approved_by_user_id) THEN
            RAISE EXCEPTION 'tax master: creator/approver of a % record cannot be reassigned', TG_TABLE_NAME
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;

    old_j := to_jsonb(OLD) - skip;
    new_j := to_jsonb(NEW) - skip;

    IF OLD.status = 'retired' THEN
        IF new_j <> old_j THEN
            RAISE EXCEPTION 'tax master: a retired % record is read-only', TG_TABLE_NAME
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    ELSIF OLD.status = 'approved' THEN
        IF NEW.status = 'retired' THEN
            IF (new_j - 'status') <> (old_j - 'status') THEN
                RAISE EXCEPTION 'tax master: an approved % record can only be retired, not edited', TG_TABLE_NAME
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        ELSIF NEW.status = 'approved' THEN
            IF TG_TABLE_NAME = 'tax_rules' THEN
                -- the only permitted edit: bring the end date forward (never extend it)
                IF (new_j - 'effective_to') <> (old_j - 'effective_to') THEN
                    RAISE EXCEPTION 'tax master: an approved rule cannot be edited'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF OLD.effective_to IS NOT NULL
                   AND (NEW.effective_to IS NULL OR NEW.effective_to > OLD.effective_to) THEN
                    RAISE EXCEPTION 'tax master: a rule end date can be brought forward, never extended'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            ELSIF new_j <> old_j THEN
                RAISE EXCEPTION 'tax master: an approved % record cannot be edited', TG_TABLE_NAME
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        ELSE
            RAISE EXCEPTION 'tax master: an approved % record cannot return to %', TG_TABLE_NAME, NEW.status
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''


def upgrade() -> None:
    op.execute(GUARD_FUNCTION_SQL)
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_guard BEFORE INSERT OR UPDATE OR DELETE "
            f"ON {table} FOR EACH ROW EXECUTE FUNCTION tax_master_guard()"
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_guard ON {table}")
    op.execute("DROP FUNCTION IF EXISTS tax_master_guard()")
