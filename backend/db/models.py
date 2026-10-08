"""SQLAlchemy 2.x ORM 模型。

表名刻意保持 `tasks`（而非改名 `research_tasks`）：表名不对外暴露，
改名只会给迁移增加风险而没有收益。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


class ResearchTask(Base):
    """研究任务。

    字段集合刻意与当前 API 契约（`backend/schemas/responses.py::TaskStatus`）
    保持一致，不要按"最小模型"原则删减：

        thread_id / status / stage / query
        draft_report / final_report / verification / error

    其中 `draft_report` 是 HITL 审查流程的必需字段——前端必须先拿到草稿
    预览才能提交 approve / revise。
    """

    __tablename__ = "tasks"

    # 业务主键保持 thread_id 命名：它已出现在全部 API 响应字段中
    # （TaskCreated / TaskStatus / TaskListItem），改名会强制同步修改前端契约。
    # 生成方式为 uuid.uuid4().hex[:12]，String(32) 足够。
    thread_id: Mapped[str] = mapped_column(String(32), primary_key=True)

    query: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    stage: Mapped[str] = mapped_column(String(50), nullable=False, default="")

    draft_report: Mapped[str] = mapped_column(Text, nullable=False, default="")
    final_report: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 可空 JSON —— verification=None 是合法业务状态。
    #
    # 历史包袱：旧 schema 为 `TEXT NOT NULL DEFAULT 'null'`，即用「字符串
    # 'null'」表示「无核查结果」，与 SQL NULL 混用。改为真正的 nullable
    # JSON 后，'null' 与 SQL NULL 都读作 None。
    #
    # `none_as_null=True` **必须显式指定**：SQLAlchemy 的 JSON 默认是
    # none_as_null=False，即把 Python None 序列化成 JSON 字面量 'null'
    # （一个字符串）而不是 SQL NULL。那会原样复制上面说的历史包袱。
    verification: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )

    error: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # ===== Phase G 运行时元数据 =====
    #
    # 这些字段**不承担互斥职责**——互斥由 Redis 的 Atomic Claim 负责
    # （见 docs/phase-g-runtime-design.md §2.5）。
    # 它们用于观测、重试判定与恢复决策。

    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 取消请求必须持久化（执行包 §8）：不能只存在 Python global 或
    # 纯 ephemeral Redis flag，否则 API 重启后取消请求就丢了。
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        """转成 API/服务层使用的普通 dict。"""
        return {
            "thread_id": self.thread_id,
            "query": self.query,
            "status": self.status,
            "stage": self.stage,
            "draft_report": self.draft_report,
            "final_report": self.final_report,
            "verification": self.verification,
            "error": self.error,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }

    def to_list_item(self) -> dict[str, Any]:
        """历史列表项——只含 `TaskListItem` 契约需要的字段。"""
        return {
            "thread_id": self.thread_id,
            "query": self.query,
            "status": self.status,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class TaskReview(Base):
    """HITL 审查决定。

    独立成表而非在 tasks 上加列，以便保留多次审查（revise 可能反复发生）
    的历史轨迹——见 docs/phase-g-runtime-design.md §6。

    **消费语义的关键**：`consumed_at` 仅用于审计与观测，
    **不参与恢复决策**。恢复判断依据是 LangGraph checkpoint 中
    human_review 的 interrupt 是否仍 pending——否则会出现
    「标记已消费 → resume 前崩溃 → 决定不再重放 → 任务永卡」的窗口。
    """

    __tablename__ = "task_reviews"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    action: Mapped[str] = mapped_column(String(20), nullable=False)
    feedback: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consumed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "action": self.action,
            "feedback": self.feedback,
            "created_at": _iso(self.created_at),
            "consumed_at": _iso(self.consumed_at) or None,
            "consumed_by": self.consumed_by,
        }


def _iso(value: datetime | None) -> str:
    """时间戳序列化。

    保持与改造前一致的 ISO-8601 带微秒格式，避免 API 响应出现格式漂移。
    """
    return value.isoformat() if value is not None else ""


class MemoryOutbox(Base):
    """Atomic with completed task; an at-least-once, fenced enrichment job."""

    __tablename__ = "memory_outbox"

    job_id: Mapped[str] = mapped_column(String(72), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    report_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Sanitized, bounded episode observations; NEVER a copy of the report.
    episode_payload: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    report_done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    episodes_done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    doc_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class MemoryTemporalDecision(Base):
    """Authoritative HUMAN decision; unreviewed graph candidates are not facts."""

    __tablename__ = "memory_temporal_decisions"

    relation_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    claim_a_id: Mapped[str] = mapped_column(String(80), nullable=False)
    claim_b_id: Mapped[str] = mapped_column(String(80), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    older_claim_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    newer_claim_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reviewer: Mapped[str] = mapped_column(String(150), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    reviewed_at: Mapped[float] = mapped_column(Float, nullable=False)


class MemoryTemporalAudit(Base):
    """Append-only review history (separate from the latest decision)."""

    __tablename__ = "memory_temporal_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    relation_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    prior_decision: Mapped[str | None] = mapped_column(String(24), nullable=True)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(150), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    reviewed_at: Mapped[float] = mapped_column(Float, nullable=False)
