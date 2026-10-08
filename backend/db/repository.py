"""任务仓储。

边界约定：**仓储只负责持久化，不做业务校验。** 状态迁移的合法性由
`backend.domain.task.ensure_transition` 在 service 层保证。

刻意不提供 `update(thread_id, **kwargs)` 这类通用 setter——那正是本次
改造要关掉的口子（执行包 §7）：它允许调用方写入任意数据库列，从而绕过
状态机。此处所有写路径都必须经过 ``ResearchTask`` 实体的固定字段。
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Protocol

from sqlalchemy import select

from backend.db.engine import reset_engine, session_scope
from backend.db.models import MemoryOutbox, ResearchTask, TaskReview
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)


class TaskRepository(Protocol):
    """任务仓储契约。

    保留这层抽象是为了给未来重新引入数据库后端留出边界——但**不要**为
    尚未存在也未测试的后端提前编写实现（执行包 §0.5.4 第 11 条）。
    """

    def create(self, thread_id: str, query: str) -> ResearchTask: ...

    def get(self, thread_id: str) -> ResearchTask | None: ...

    def list_tasks(self, *, include_deleted: bool = False) -> list[ResearchTask]: ...

    def save(self, task: ResearchTask) -> ResearchTask: ...


class SqlAlchemyTaskRepository:
    """基于 SQLAlchemy 2.x 的实现（SQLite）。"""

    def create(self, thread_id: str, query: str) -> ResearchTask:
        now = datetime.now()
        task = ResearchTask(
            thread_id=thread_id,
            query=query,
            status="pending",
            stage="",
            draft_report="",
            final_report="",
            verification=None,
            error="",
            created_at=now,
            updated_at=now,
        )
        with session_scope() as session:
            session.add(task)
            session.flush()
            session.expunge(task)
        logger.debug("任务已创建: %s", thread_id)
        return task

    def get(self, thread_id: str) -> ResearchTask | None:
        with session_scope() as session:
            task = session.get(ResearchTask, thread_id)
            if task is None:
                return None
            session.expunge(task)
            return task

    def list_tasks(self, *, include_deleted: bool = False) -> list[ResearchTask]:
        stmt = select(ResearchTask)
        if not include_deleted:
            # 与改造前一致：历史列表默认过滤软删除的任务
            stmt = stmt.where(ResearchTask.status != "deleted")
        stmt = stmt.order_by(ResearchTask.created_at.desc())

        with session_scope() as session:
            tasks = list(session.scalars(stmt))
            for task in tasks:
                session.expunge(task)
            return tasks

    def save(self, task: ResearchTask) -> ResearchTask:
        """持久化一个已受校验的实体。

        `task` 可能是脱离 session 的对象（如由 `get()` 返回），因此用
        merge 而非 add。
        """
        with session_scope() as session:
            merged = session.merge(task)
            session.flush()
            session.expunge(merged)
            return merged

    def save_completed_with_outbox(
        self, task: ResearchTask, *, user_query: str = "",
        research_trace: list[dict] | None = None, research_generation: int = 0,
    ) -> ResearchTask:
        """Atomically commit the final report AND its durable enrichment request.

        If the outbox insert fails, neither task completion nor the outbox
        survives. The runtime can retry the normal task settlement safely.
        """
        if task.status != "completed" or not task.final_report:
            raise ValueError("outbox enqueue requires a completed task and report")
        from deep_research.memory.episodes import build_completed_episodes

        digest = hashlib.sha256(task.final_report.encode("utf-8")).hexdigest()
        job_id = "mem-" + hashlib.sha256(
            f"{task.thread_id}\x00{digest}".encode("utf-8")
        ).hexdigest()
        # Pre-sanitize and cap the observed trace, keeping no raw web pages or
        # chain-of-thought in the durable payload. A rejected speculative
        # generation will already have been filtered by the episode builder.
        try:
            payload = [asdict(episode) for episode in build_completed_episodes(
                task.thread_id, research_trace or [], int(research_generation or 0),
            )]
        except (ValueError, TypeError, OverflowError) as exc:
            # Bad optional research traces must not invalidate the final report.
            logger.warning("optional episodic trace invalid, ignoring: %s", type(exc).__name__)
            payload = []
        now = time.time()
        with session_scope() as session:
            merged = session.merge(task)
            if session.get(MemoryOutbox, job_id) is None:
                session.add(MemoryOutbox(
                    job_id=job_id, task_id=task.thread_id, report_sha256=digest,
                    query=(user_query or task.query or "")[:8000],
                    episode_payload=payload, status="pending", attempts=0,
                    next_attempt_at=0.0, created_at=now, updated_at=now,
                    report_done=False, episodes_done=False,
                ))
            session.flush()
            session.expunge(merged)
            return merged

    def enqueue_existing_completed(self, thread_id: str) -> bool:
        """Backfill a legacy COMPLETED task with a missing outbox record.

        Returns False if the task is missing/incomplete or already enqueued.
        Designed for explicit operator reconciliation, NOT an API endpoint.
        """
        with session_scope() as session:
            task = session.get(ResearchTask, thread_id)
            if task is None or task.status != "completed" or not task.final_report:
                return False
            digest = hashlib.sha256(task.final_report.encode("utf-8")).hexdigest()
            job_id = "mem-" + hashlib.sha256(
                f"{task.thread_id}\x00{digest}".encode("utf-8")
            ).hexdigest()
            if session.get(MemoryOutbox, job_id) is not None:
                return False
            now = time.time()
            session.add(MemoryOutbox(
                job_id=job_id, task_id=thread_id, report_sha256=digest,
                query=task.query or "", episode_payload=[], status="pending",
                attempts=0, next_attempt_at=0.0, created_at=now,
                updated_at=now, report_done=False, episodes_done=False,
            ))
            return True

    # ===== HITL 审查决定 =====

    def create_review(self, thread_id: str, action: str, feedback: str = "") -> TaskReview:
        """记录一次审查决定。

        **必须落库**：审查决定若只存在内存或短 TTL 的 Redis 键上，
        API 重启或 TTL 过期就会丢失，任务将永卡 WAITING_REVIEW
        （见设计文档 §6）。
        """
        review = TaskReview(
            id=uuid.uuid4().hex[:32],
            thread_id=thread_id,
            action=action,
            feedback=feedback,
            created_at=datetime.now(),
        )
        with session_scope() as session:
            session.add(review)
            session.flush()
            session.expunge(review)
        return review

    def latest_review(self, thread_id: str) -> TaskReview | None:
        """最近一次审查决定（无论是否已消费）。"""
        stmt = (
            select(TaskReview)
            .where(TaskReview.thread_id == thread_id)
            .order_by(TaskReview.created_at.desc())
            .limit(1)
        )
        with session_scope() as session:
            review = session.scalars(stmt).first()
            if review is not None:
                session.expunge(review)
            return review

    def mark_review_consumed(self, review_id: str, worker_id: str) -> None:
        """记录消费。

        **仅用于审计与观测，不参与恢复决策**（设计文档 §6.2）——
        恢复依据是 checkpoint 里 human_review 的 interrupt 是否仍 pending。
        """
        with session_scope() as session:
            review = session.get(TaskReview, review_id)
            if review is not None:
                review.consumed_at = datetime.now()
                review.consumed_by = worker_id


_repository: TaskRepository | None = None
_lock = threading.Lock()


def get_repository() -> TaskRepository:
    """获取进程内缓存的仓储实例。"""
    global _repository
    if _repository is None:
        with _lock:
            if _repository is None:
                _repository = SqlAlchemyTaskRepository()
    return _repository


def reset_repository() -> None:
    """丢弃缓存的仓储与底层 engine，供测试隔离使用。"""
    global _repository
    with _lock:
        _repository = None
    reset_engine()
