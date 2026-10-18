"""Persist trusted task owners and isolate temporal-review rows.

Revision ID: 0004_tenant_ownership
Revises: 0003_memory_reliability
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_tenant_ownership"
down_revision = "0003_memory_reliability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing task rows are assigned to the single local owner; no old task
    # gains access to the data of a newly provisioned account.
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("user_id", sa.String(128), nullable=False,
                                   server_default="local"))
        batch.create_index("ix_tasks_user_id", ["user_id"])
    with op.batch_alter_table("memory_temporal_decisions") as batch:
        batch.add_column(sa.Column("user_id", sa.String(128), nullable=False,
                                   server_default="local"))
    with op.batch_alter_table("memory_temporal_audit") as batch:
        batch.add_column(sa.Column("user_id", sa.String(128), nullable=False,
                                   server_default="local"))


def downgrade() -> None:
    with op.batch_alter_table("memory_temporal_audit") as batch:
        batch.drop_column("user_id")
    with op.batch_alter_table("memory_temporal_decisions") as batch:
        batch.drop_column("user_id")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_index("ix_tasks_user_id")
        batch.drop_column("user_id")
