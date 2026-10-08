"""worker runtime metadata

Phase G 所需的两项 schema 变更：

1. `tasks` 表新增运行时元数据（观测 / 重试判定 / 取消请求持久化）
2. 新建 `task_reviews` 表（HITL 审查决定，独立成表以保留多次审查轨迹）

**绝不修改 `0001_baseline_tasks.py`** —— 它已应用到真实数据库
（执行包 §31 明令）。

新增列全部可空或带 server_default，因此对现有 66 条真实任务是安全的：
不需要数据回填，也不会因 NOT NULL 约束失败。

Revision ID: 0002_runtime_metadata
Revises: 0001_baseline
Create Date: 2026-09-30
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0002_runtime_metadata"
down_revision: str | None = "0001_baseline"
branch_labels: str | None = None
depends_on: str | None = None


_TASK_COLUMNS = (
    # (列名, 类型, server_default, comment)
    ("attempt", sa.Integer(), "0", "已尝试次数，用于重试判定"),
    ("claimed_by", sa.String(200), None, "最后一次成功 claim 的 worker_id"),
    ("claimed_at", sa.DateTime(), None, None),
    ("heartbeat_at", sa.DateTime(), None, None),
    ("cancel_requested", sa.Boolean(), "0", "取消请求（必须持久化，见 §8）"),
    ("cancel_requested_at", sa.DateTime(), None, None),
)


# 非空列 —— 它们带 server_default，故现有行无需回填
_NOT_NULLABLE = {"attempt", "cancel_requested"}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # --- tasks 表：新增运行时列 ---
    existing = {c["name"] for c in inspector.get_columns("tasks")}
    for name, coltype, default, _comment in _TASK_COLUMNS:
        if name in existing:
            # 幂等：允许重复执行（例如被手工修补过的库）
            continue

        nullable = name not in _NOT_NULLABLE
        # server_default 必须在构造时传入 —— 构造后再赋值 server_default
        # 不会反映到 DDL 上（实测会生成 "ADD COLUMN attempt INTEGER NOT NULL"
        # 并因缺少默认值而失败）。
        op.add_column(
            "tasks",
            sa.Column(
                name,
                coltype,
                nullable=nullable,
                server_default=sa.text(default) if default is not None else None,
            ),
        )

    # --- task_reviews 表 ---
    if "task_reviews" not in inspector.get_table_names():
        op.create_table(
            "task_reviews",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("thread_id", sa.String(32), nullable=False),
            sa.Column("action", sa.String(20), nullable=False),
            sa.Column("feedback", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            # consumed_* 仅用于审计，不参与恢复决策（见设计文档 §6.2）
            sa.Column("consumed_at", sa.DateTime(), nullable=True),
            sa.Column("consumed_by", sa.String(200), nullable=True),
        )
        op.create_index("ix_task_reviews_thread_id", "task_reviews", ["thread_id"])


def downgrade() -> None:
    """回退本迁移。

    注意：`cancel_requested` 与非空的 `attempt` 一旦有数据，回退会丢失
    取消请求与重试计数。这是本迁移本身的信息损失。
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "task_reviews" in inspector.get_table_names():
        op.drop_index("ix_task_reviews_thread_id", table_name="task_reviews")
        op.drop_table("task_reviews")

    existing = {c["name"] for c in inspector.get_columns("tasks")}
    with op.batch_alter_table("tasks") as batch_op:
        for name, _t, _d, _c in reversed(_TASK_COLUMNS):
            if name in existing:
                batch_op.drop_column(name)
