"""An issued invoice or credit note is a legal record: the database refuses any change to it (installed with the tables)."""

from sqlalchemy import DDL, event

from app.core.database import Base

FUNCTION = r'''CREATE OR REPLACE FUNCTION invoices_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'invoices: an issued invoice or credit note cannot be changed or deleted (issue a credit note)'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;'''

TRIGGER_NAME = "trg_invoices_immutable"
TRIGGER = "CREATE TRIGGER trg_invoices_immutable BEFORE UPDATE OR DELETE ON invoices FOR EACH ROW EXECUTE FUNCTION invoices_immutable()"

for _sql in (FUNCTION, TRIGGER):
    event.listen(Base.metadata, "after_create", DDL(_sql.replace("%", "%%")).execute_if(dialect="postgresql"))
