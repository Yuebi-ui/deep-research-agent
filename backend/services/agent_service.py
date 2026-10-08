"""Agent 生命周期管理：任务注册、状态查询、HITL 审查、最终结果提取。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from backend.db import get_repository
from backend.domain.task import (
    InvalidReviewState,
    TaskStatus,
    UnknownTaskStatus,
    coerce_status,
    ensure_transition,
    status_for_stage,
)
from deep_research.agent_builder import _create_builder
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)

# ===== Checkpointer =====
#
# 具体后端由 deep_research.checkpoint 工厂按配置决定
# （memory / sqlite / redis，见执行包 §0.5.2）。
# 这里只负责持有工厂产出的实例，并在 lifespan 中完成初始化。
#
# 已移除原有的"Redis 连不上就降级 InMemorySaver"逻辑：静默降级会让
# 跨进程恢复的验证变成假阳性——测试看似通过，实际用的是进程内内存。


async def init_checkpointer() -> None:
    """初始化 checkpointer（由 FastAPI lifespan / worker 启动流程调用）。"""
    from deep_research import checkpoint as checkpoint_factory

    managed = await checkpoint_factory.init_checkpointer()
    logger.info("checkpointer 就绪：backend=%s", managed.backend)


def _get_checkpointer() -> Any:
    """获取已初始化的 checkpointer。

    未初始化时抛错，不回退到 memory——静默回退会让"跨进程恢复"看起来
    通过，实际用的是进程内内存。
    """
    from deep_research import checkpoint as checkpoint_factory

    return checkpoint_factory.get_checkpointer()


def _checkpointer_backend() -> str:
    """当前 checkpointer 后端名（memory / sqlite / redis）。"""
    from deep_research import checkpoint as checkpoint_factory

    return checkpoint_factory.get_backend()


_agent_cache: Any = None


def _build_agent():
    """构建 agent 实例（缓存复用，避免每次请求重编译 3-8 秒）。"""
    global _agent_cache
    if _agent_cache is not None:
        return _agent_cache
    builder = _create_builder(with_hitl=True)
    _agent_cache = builder.compile(checkpointer=_get_checkpointer())
    return _agent_cache


# ===== 任务 CRUD =====

# 用于区分「未传该字段」与「显式传 None」。
# 必要性：mark_task_completed(..., verification=None) 是一次**显式赋值**
# （把核查结果置空），与「不修改 verification」语义完全不同。
_UNSET: Any = object()


def create_task(query: str) -> dict:
    """创建新研究任务。"""
    thread_id = uuid.uuid4().hex[:12]
    task = get_repository().create(thread_id, query)
    return {
        "thread_id": task.thread_id,
        "status": task.status,
        "created_at": task.created_at.isoformat(),
    }


def get_task_status(thread_id: str) -> dict:
    """获取任务状态快照。"""
    task = get_repository().get(thread_id)
    if task is None:
        return {"thread_id": thread_id, "status": "unknown"}
    return {
        "thread_id": task.thread_id,
        "status": task.status,
        "stage": task.stage,
        "query": task.query,
        "draft_report": task.draft_report,
        "final_report": task.final_report,
        "verification": task.verification,
        "error": task.error,
    }


def transition_task(
    thread_id: str,
    target: TaskStatus,
    *,
    stage: str | None = None,
    draft_report: str | None = None,
    final_report: str | None = None,
    verification: Any = _UNSET,
    error: str | None = None,
) -> None:
    """经状态机校验后写入任务状态。

    参数刻意全部为**显式具名**，不接受 ``**kwargs``——通用字段 setter 会
    允许调用方写入任意列并绕过状态机，那正是本次改造要关掉的口子。

    任务不存在时保持既有的静默 no-op 语义（原先是 UPDATE 影响 0 行），
    以免改变现有 API 行为；这种情况会记 warning 以便排查。
    """
    repo = get_repository()
    task = repo.get(thread_id)
    if task is None:
        logger.warning("任务 %s 不存在，跳过状态迁移至 %s", thread_id, target.value)
        return

    current = coerce_status(task.status)
    ensure_transition(current, target)

    task.status = target.value
    if stage is not None:
        task.stage = stage
    if draft_report is not None:
        task.draft_report = draft_report
    if final_report is not None:
        task.final_report = final_report
    if verification is not _UNSET:
        task.verification = verification
    if error is not None:
        task.error = error
    task.updated_at = datetime.now()

    repo.save(task)


def set_task_stage(
    thread_id: str,
    stage: str,
    *,
    draft_report: str | None = None,
) -> None:
    """更新当前阶段，并按 :data:`STAGE_TO_STATUS` 推导状态。"""
    transition_task(
        thread_id,
        status_for_stage(stage),
        stage=stage,
        draft_report=draft_report,
    )


def ensure_reviewable(thread_id: str) -> None:
    """校验任务处于可审查状态，否则抛 :class:`InvalidReviewState`。

    仅 ``waiting_review`` 合法。任务不存在时保持既有语义（由调用方处理
    404），此处静默返回。
    """
    task = get_repository().get(thread_id)
    if task is None:
        return

    current = coerce_status(task.status)
    if current is not TaskStatus.WAITING_REVIEW:
        raise InvalidReviewState(current)


def mark_task_failed(thread_id: str, error: str) -> None:
    transition_task(thread_id, TaskStatus.FAILED, error=error)


def mark_task_deleted(thread_id: str) -> None:
    """软删除：状态置为 deleted，列表查询会自动过滤。"""
    transition_task(thread_id, TaskStatus.DELETED)


def fail_stale_tasks(reason: str) -> int:
    """把残留的 running/pending 任务标记为失败，返回处理条数。

    用于进程启动时清理无法恢复的任务（内存 checkpointer 模式）。
    """
    repo = get_repository()
    count = 0
    for task in repo.list_tasks():
        try:
            current = coerce_status(task.status)
        except UnknownTaskStatus:
            logger.warning("任务 %s 状态未知，跳过: %r", task.thread_id, task.status)
            continue
        if current not in (TaskStatus.RUNNING, TaskStatus.PENDING):
            continue
        ensure_transition(current, TaskStatus.FAILED)
        task.status = TaskStatus.FAILED.value
        task.error = reason
        task.updated_at = datetime.now()
        repo.save(task)
        count += 1
    return count


def mark_task_completed(
    thread_id: str,
    final_report: str,
    verification: dict | None,
) -> None:
    transition_task(
        thread_id,
        TaskStatus.COMPLETED,
        stage="final_report_generation",
        final_report=final_report,
        # 显式赋值：verification=None 表示「完成但无核查结果」，是合法状态
        verification=verification,
    )


def list_tasks() -> list[dict]:
    """列出所有历史任务（按创建时间倒序，不含软删除）。"""
    return [task.to_list_item() for task in get_repository().list_tasks()]


# ===== Agent 输入/输出工具 =====

def get_thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def build_input(query: str) -> dict:
    return {"messages": [HumanMessage(content=query)]}


def build_resume_command(action: str, feedback: str = "") -> Command:
    if action == "revise":
        return Command(resume={"action": "revise", "feedback": feedback})
    return Command(resume={"action": "approve"})


async def extract_final_state(agent, thread_config: dict) -> dict:
    """从 agent checkpointer 中提取最终状态。"""
    try:
        state = await agent.aget_state(thread_config)
        if state and state.values:
            return {
                "final_report": state.values.get("final_report", ""),
                "verification": state.values.get("verification_report"),
                "draft_report": state.values.get("draft_report", ""),
                "research_brief": state.values.get("research_brief", ""),
            }
    except Exception:
        pass
    return {}


async def get_report(thread_id: str) -> dict:
    """获取最终报告（优先 agent state，回退到持久化记录）。"""
    agent = _build_agent()
    config = get_thread_config(thread_id)
    state_values = await extract_final_state(agent, config)

    task = get_repository().get(thread_id)

    final_report = state_values.get("final_report") or (task.final_report if task else "")
    verification = state_values.get("verification") or (task.verification if task else None)

    return {
        "thread_id": thread_id,
        "query": task.query if task else "",
        "final_report": final_report,
        "verification": verification,
        "draft_report": state_values.get("draft_report") or (task.draft_report if task else ""),
    }
