"""staff roles and permissions (BE-P1)

Revision ID: c5e1a7f3b964
Revises: e7b2c5a9d041
Create Date: 2026-11-07 10:00:00.000000

Additive: two new tables and the system roles Owner and No access. Every existing active or inactive administrator is assigned the
Owner role, so every administrator keeps every permission. Nothing existing changes.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c5e1a7f3b964"
down_revision: Union[str, Sequence[str], None] = "e7b2c5a9d041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OWNER_ROLE_ID = "6f0c1e2a-0000-4000-8000-000000000001"  # app.modules.roles.permissions.OWNER_ROLE_ID
NO_ACCESS_ROLE_ID = "6f0c1e2a-0000-4000-8000-000000000002"  # app.modules.roles.permissions.NO_ACCESS_ROLE_ID


def upgrade() -> None:
    op.create_table(
        "staff_roles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("permissions", postgresql.ARRAY(sa.String(length=64)), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "staff_role_assignments",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.Column("assigned_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["assigned_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["role_id"], ["staff_roles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index(op.f("ix_staff_role_assignments_role_id"), "staff_role_assignments", ["role_id"], unique=False)
    op.execute(
        "INSERT INTO staff_roles (id, name, description, permissions, is_system) "
        f"VALUES ('{OWNER_ROLE_ID}', 'Owner', NULL, '{{}}', true), ('{NO_ACCESS_ROLE_ID}', 'No access', NULL, '{{}}', true)"
    )
    op.execute(
        "INSERT INTO staff_role_assignments (user_id, role_id) "
        f"SELECT id, '{OWNER_ROLE_ID}' FROM users WHERE role = 'admin'"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_staff_role_assignments_role_id"), table_name="staff_role_assignments")
    op.drop_table("staff_role_assignments")
    op.drop_table("staff_roles")
