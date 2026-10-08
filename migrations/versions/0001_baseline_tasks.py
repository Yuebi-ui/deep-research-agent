"""baseline tasks table

这是本项目的**首个** Alembic 迁移，需要同时覆盖两条路径（执行包 §9）：

1. **空数据库** —— 直接建立当前 schema。
2. **已存在的旧 schema** —— 原地升级。旧的 `tasks` 表由
   `CREATE TABLE IF NOT EXISTS` 创建，且 `verification` 为
   `TEXT NOT NULL DEFAULT 'null'`，即用「字符串 'null'」表示「无核查结果」。

第 2 条是本迁移的实质内容：把 `verification` 从
「NOT NULL + 字符串 'null' 哨兵」改为「真正的 nullable JSON」，
并规范化历史数据（`'null'` → SQL NULL）。

实测数据（迁移前基线）：
    task_count        66
    verification      'null' 字符串 59 条，JSON 对象 7 条

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-30
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


# 旧 schema 中表示「无核查结果」的哨兵字符串
_LEGACY_NULL_SENTINEL = "null"


def _create_tasks_table() -> None:
    """空库路径：建立当前 schema。"""
    op.create_table(
        "tasks",
        sa.Column("thread_id", sa.String(32), primary_key=True),
        sa.Column("query", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("stage", sa.String(50), nullable=False, server_default=""),
        sa.Column("draft_report", sa.Text(), nullable=False, server_default=""),
        sa.Column("final_report", sa.Text(), nullable=False, server_default=""),
        # 可空 JSON —— verification=None 是合法业务状态
        sa.Column("verification", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def _upgrade_existing_tasks(bind) -> None:
    """已存在旧 schema 的路径：原地升级。"""
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("tasks")}

    required = {
        "thread_id", "query", "status", "stage",
        "draft_report", "final_report", "verification", "error",
        "created_at", "updated_at",
    }
    missing = required - columns
    if missing:
        # schema 与预期不符时明确失败，不做猜测性修补
        raise RuntimeError(
            f"现有 tasks 表缺少预期列: {sorted(missing)}；"
            "请先人工确认 schema，不要自动迁移。"
        )

    # 1) 解除 NOT NULL 与 'null' 默认值（SQLite 走 batch 模式，实际是重建表）
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.alter_column(
            "verification",
            existing_type=sa.Text(),
            type_=sa.JSON(),
            existing_nullable=False,
            nullable=True,
            server_default=None,
        )

    # 2) 规范化历史数据：字符串 'null' → 真正的 SQL NULL
    #    必须在解除 NOT NULL 之后执行
    result = bind.execute(
        sa.text(
            "UPDATE tasks SET verification = NULL "
            "WHERE verification = :sentinel"
        ),
        {"sentinel": _LEGACY_NULL_SENTINEL},
    )
    if result.rowcount:
        print(f"[migration] 规范化 {result.rowcount} 行 verification='null' → NULL")


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "tasks" not in tables:
        _create_tasks_table()
        return

    _upgrade_existing_tasks(bind)


def downgrade() -> None:
    """回退为旧的「NOT NULL + 'null' 哨兵」形态。

    注意：这会丢失「verification 为 SQL NULL」与「verification 为 JSON null」
    的区分——两者在旧 schema 下都只能表示为字符串 'null'。这是旧 schema
    本身的信息损失，不可完全逆转。
    """
    bind = op.get_bind()

    bind.execute(
        sa.text(
            "UPDATE tasks SET verification = :sentinel WHERE verification IS NULL"
        ),
        {"sentinel": _LEGACY_NULL_SENTINEL},
    )

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.alter_column(
            "verification",
            existing_type=sa.JSON(),
            type_=sa.Text(),
            existing_nullable=True,
            nullable=False,
            server_default=_LEGACY_NULL_SENTINEL,
        )
