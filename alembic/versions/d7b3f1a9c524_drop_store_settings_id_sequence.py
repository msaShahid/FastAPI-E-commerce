"""store_settings.id is a fixed singleton key: drop the sequence the original migration gave it

Revision ID: d7b3f1a9c524
Revises: c3f9b1d7a846
Create Date: 2026-10-23 16:00:00.000000

The table holds exactly one row (CHECK id = 1) and the application always inserts id = 1 explicitly. The first migration created
the column as a SERIAL anyway, while the model declares no default, so a migrated database and one built from the models
differed. Nothing reads the sequence; dropping it changes no data.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "d7b3f1a9c524"
down_revision: Union[str, Sequence[str], None] = "c3f9b1d7a846"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE store_settings ALTER COLUMN id DROP DEFAULT")
    op.execute("DROP SEQUENCE IF EXISTS store_settings_id_seq")


def downgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS store_settings_id_seq OWNED BY store_settings.id")
    op.execute("ALTER TABLE store_settings ALTER COLUMN id SET DEFAULT nextval('store_settings_id_seq')")
    op.execute("SELECT setval('store_settings_id_seq', coalesce((SELECT max(id) FROM store_settings), 1))")
