"""Durable memory outbox and append-only temporal review ledger.

Revision ID: 0003_memory_reliability
Revises: 0002_runtime_metadata
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_memory_reliability"
down_revision = "0002_runtime_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_outbox",
        sa.Column("job_id", sa.String(72), primary_key=True),
        sa.Column("task_id", sa.String(32), nullable=False),
        sa.Column("report_sha256", sa.String(64), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("episode_payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.Float(), nullable=False),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_owner", sa.String(200)),
        sa.Column("lease_until", sa.Float()),
        sa.Column("report_done", sa.Boolean(), nullable=False),
        sa.Column("episodes_done", sa.Boolean(), nullable=False),
        sa.Column("doc_id", sa.String(80)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float()),
    )
    op.create_index("ix_memory_outbox_task_id", "memory_outbox", ["task_id"])
    op.create_index("ix_memory_outbox_status", "memory_outbox", ["status"])
    op.create_index("ix_memory_outbox_due", "memory_outbox", ["status", "next_attempt_at"])
    op.create_table(
        "memory_temporal_decisions",
        sa.Column("relation_id", sa.String(80), primary_key=True),
        sa.Column("claim_a_id", sa.String(80), nullable=False),
        sa.Column("claim_b_id", sa.String(80), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("older_claim_id", sa.String(80)),
        sa.Column("newer_claim_id", sa.String(80)),
        sa.Column("reviewer", sa.String(150), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("reviewed_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "memory_temporal_audit",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("relation_id", sa.String(80), nullable=False),
        sa.Column("prior_decision", sa.String(24)),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("reviewer", sa.String(150), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("reviewed_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_memory_temporal_audit_relation_id", "memory_temporal_audit", ["relation_id"])


def downgrade() -> None:
    # Explicit downgrade is destructive to queue state and review history!
    op.drop_index("ix_memory_temporal_audit_relation_id", table_name="memory_temporal_audit")
    op.drop_table("memory_temporal_audit")
    op.drop_table("memory_temporal_decisions")
    op.drop_index("ix_memory_outbox_due", table_name="memory_outbox")
    op.drop_index("ix_memory_outbox_status", table_name="memory_outbox")
    op.drop_index("ix_memory_outbox_task_id", table_name="memory_outbox")
    op.drop_table("memory_outbox")
