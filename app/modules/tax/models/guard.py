"""
Database-level protection for the GST master (defence in depth).

The service layer already refuses to edit approved records, but tax records are
compliance-sensitive and other code paths (scripts, future features, direct SQL) must
not be able to rewrite them either. This trigger enforces, in the database:

  * records are created as drafts (they cannot be inserted already approved);
  * an approved record can only be retired; an approved RULE may also have its end
    date brought forward (never extended);
  * retired records are read-only; records cannot go back to draft;
  * non-draft records cannot be deleted;
  * creator/approver may be cleared (a deleted user) but never reassigned.

The same SQL lives in migration f2b6d8a4c917; tests assert the two copies are identical.
"""

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

GUARDED_TABLES = ("tax_classifications", "tax_rules", "gst_registrations")


def trigger_name(table: str) -> str:
    return f"trg_{table}_guard"
