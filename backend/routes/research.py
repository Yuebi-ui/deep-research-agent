"""研究任务 API 端点：命令、查询、事件投影。

Phase G 之后本模块**不再拥有执行状态**：

```text
POST /start    创建任务并入队（立即返回，不等研究完成）
POST /resume   校验状态 + 落库审查决定 + 入队
POST /cancel   置取消标志（协作式取消）
GET  /stream   只读 Redis Stream + DB 投影（**不构建 graph**）
GET  /status   查询
DELETE /{id}   软删除
GET  /report   读 DB（不再经 graph 读 checkpoint）
```

**本文件不得 import `_create_builder`，不得持有 checkpointer。**
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from backend.db import get_repository
from backend.domain.task import InvalidReviewState, TaskStatus, UnknownTaskStatus, coerce_status
from backend.runtime import queue as job_queue
from backend.runtime import redis as rt_redis
from backend.runtime import sse as sse_projection
from backend.runtime.events import EventType, publish
from backend.runtime.queue import EnqueueRejected
from backend.core.api_errors import (
    CODE_CANCEL_NOT_ALLOWED,
    CODE_QUEUE_UNAVAILABLE,
    CODE_TASK_NOT_FOUND,
    ApiError,
)
from backend.schemas.requests import ResearchRequest, ReviewAction
from backend.schemas.responses import (
    CancelResponse,
    DeleteResponse,
    ReportResponse,
    ReviewResponse,
    TaskCreatedResponse,
    TaskListItem,
    TaskSummaryResponse,
)
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)

router = APIRouter(prefix="/api/research", tags=["research"])


# ===== 内部工具 =====


def _get_task_or_404(thread_id: str) -> Any:
    task = get_repository().get(thread_id)
    if task is None:
        raise ApiError(CODE_TASK_NOT_FOUND, "任务不存在", status_code=404)
    return task


async def _enqueue(thread_id: str) -> None:
    """入队。

    Redis 不可达时返回 503，且**不改变任务状态**——任务保持 PENDING，
    用户可以重试。绝不静默丢任务，也不把任务标成 FAILED（设计文档 §8.4）。
    """
    try:
        redis = rt_redis.get_redis()
        await publish(
            redis, thread_id, EventType.TASK_QUEUED, {},
            maxlen=_settings().event_retention_maxlen,
        )
        await job_queue.enqueue(
            redis, thread_id,
            dedupe_ttl_ms=int(_settings().job_dedupe_ttl_seconds * 1000),
        )
    except EnqueueRejected as exc:
        raise ApiError(CODE_QUEUE_UNAVAILABLE, str(exc), status_code=503) from exc
    except Exception as exc:
        raise ApiError(
            CODE_QUEUE_UNAVAILABLE, f"无法入队: {exc}", status_code=503
        ) from exc


def _settings():
    from backend.core.settings import get_settings

    return get_settings()


async def _stamp_run_id(thread_id: str, run_id: str) -> None:
    """把观测用 run_id 写入 Redis（baseline 归因）。

    旁路操作：失败不影响任务创建/入队；worker 读不到时会回退 auto-<thread_id>。
    """
    try:
        redis = rt_redis.get_redis()
        await redis.set(rt_redis.runid_key(thread_id), run_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("run_id 写入失败（观测降级，不影响任务）: %s", exc)


# ===== 命令 =====


@router.post("/start", response_model=TaskCreatedResponse)
async def start_research(req: ResearchRequest):
    """创建任务并入队，**立即返回**，不等待研究完成。

    前端流程不变：拿到 thread_id 后再去订阅 /stream 获取进度。
    """
    import uuid

    thread_id = uuid.uuid4().hex[:12]
    task = get_repository().create(thread_id, req.query)

    if req.run_id:
        # 先写 run_id 再入队：保证 worker 认领前就能读到（观测用）
        await _stamp_run_id(thread_id, req.run_id)

    await _enqueue(thread_id)

    return {
        "thread_id": task.thread_id,
        "status": task.status,
        "created_at": task.created_at.isoformat(),
    }


@router.post("/{thread_id}/resume", response_model=ReviewResponse)
async def resume_research(thread_id: str, review: ReviewAction):
    """提交 HITL 审查决定。

    审查决定**落库**（不再用短 TTL 的 Redis 键或内存 dict）——否则 API 重启
    或 TTL 过期就会丢失，任务将永卡 WAITING_REVIEW（设计文档 §6）。

    API **不**驱动 graph resume；它只入队，由 worker 执行。
    """
    task = _get_task_or_404(thread_id)

    # 只有 WAITING_REVIEW 可以接受审查决定（Phase D 已建立，保留）
    if coerce_status(task.status) is not TaskStatus.WAITING_REVIEW:
        raise InvalidReviewState(coerce_status(task.status))

    get_repository().create_review(thread_id, review.action, review.feedback)

    # 状态先回到 RUNNING，表示"已受理、待执行"
    task.status = TaskStatus.RUNNING.value
    task.stage = "supervisor_subgraph"
    from datetime import datetime

    task.updated_at = datetime.now()
    get_repository().save(task)

    await _enqueue(thread_id)

    return {"ok": True, "thread_id": thread_id}


@router.post("/{thread_id}/cancel", response_model=CancelResponse)
async def cancel_research(thread_id: str):
    """请求取消任务。

    语义是**协作式**的：`RUNNING` 任务只是置标志，worker 在下一个事件边界
    生效；**已经发出的 LLM / Search 调用无法安全抢占**（设计文档 §5.3）。
    未在执行的任务直接转入 CANCELLED。
    """
    from datetime import datetime

    task = _get_task_or_404(thread_id)
    current = coerce_status(task.status)

    # 已取消 → 幂等返回（§26 要求 test_cancel_is_idempotent）。
    # 其余终态（已完成 / 已失败 / 已删除）→ 409，取消它们没有意义。
    if current is TaskStatus.CANCELLED:
        return {"ok": True, "thread_id": thread_id, "status": current.value}

    if current in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.DELETED):
        raise ApiError(
            CODE_CANCEL_NOT_ALLOWED,
            f"终态任务不可取消: {current.value}",
            status_code=409,
            details={"current": current.value},
        )

    if not task.cancel_requested:
        task.cancel_requested = True
        task.cancel_requested_at = datetime.now()
        task.updated_at = datetime.now()

    if current in (TaskStatus.PENDING, TaskStatus.WAITING_REVIEW):
        # 没有 worker 在跑它 —— 直接终局
        task.status = TaskStatus.CANCELLED.value
    # RUNNING 的情况保持 RUNNING，等 worker 在边界处理

    get_repository().save(task)

    try:
        redis = rt_redis.get_redis()
        await publish(
            redis, thread_id, EventType.TASK_CANCEL_REQUESTED, {},
            maxlen=_settings().event_retention_maxlen,
        )
    except Exception:
        # 取消已落库，事件失败不影响语义（DB 是事实源）
        pass

    return {"ok": True, "thread_id": thread_id, "status": task.status}


# ===== 事件投影 =====


@router.get("/{thread_id}/stream")
async def stream_research(thread_id: str, request: Request):
    """SSE 事件投影。

    **只观察，不驱动**：读 Redis Stream + DB，不构建 LangGraph，
    不写任务状态。客户端断开只结束本次投影，不影响 worker。
    """
    _get_task_or_404(thread_id)

    last_event_id = request.headers.get("Last-Event-ID") or request.query_params.get("last_event_id")

    try:
        redis = rt_redis.get_redis()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "EVENT_STREAM_UNAVAILABLE", "message": str(exc)},
        ) from exc

    async def generator():
        try:
            async for frame in sse_projection.project(
                redis, get_repository(), thread_id, last_event_id=last_event_id
            ):
                yield frame
        except Exception as exc:  # 投影失败不应影响任务本身
            import logging

            logging.getLogger(__name__).warning("SSE 投影异常: %s", exc)
            payload = json.dumps({"event": "error", "data": {"message": str(exc)}}, ensure_ascii=False)
            yield f"data: {payload}\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{thread_id}/events")
async def task_events(thread_id: str, request: Request):
    """`/stream` 的别名（§14 允许的新路径），语义完全相同。"""
    return await stream_research(thread_id, request)


# ===== 查询 =====


@router.get("/{thread_id}/status", response_model=TaskSummaryResponse)
async def get_research_status(thread_id: str):
    task = _get_task_or_404(thread_id)
    return _task_summary(task)


@router.get("/{thread_id}/report", response_model=ReportResponse)
async def get_research_report(thread_id: str):
    """读取最终报告。

    **直接读 DB**，不再经 graph 读 checkpoint state——数据库是业务事实源
    （设计文档 §16）。
    """
    task = _get_task_or_404(thread_id)
    if not task.final_report:
        raise HTTPException(status_code=404, detail="Report not found or task not completed")
    return {
        "thread_id": task.thread_id,
        "query": task.query,
        "final_report": task.final_report,
        "verification": task.verification,
        "draft_report": task.draft_report,
    }


@router.get("/history/list", response_model=list[TaskListItem])
async def get_history():
    return [t.to_list_item() for t in get_repository().list_tasks()]


@router.delete("/{thread_id}", response_model=DeleteResponse)
async def delete_research_task(thread_id: str):
    """软删除。"""
    from datetime import datetime

    task = _get_task_or_404(thread_id)
    try:
        current = coerce_status(task.status)
    except UnknownTaskStatus:
        current = None

    if current is not None and current is not TaskStatus.DELETED:
        from backend.domain.task import ensure_transition

        ensure_transition(current, TaskStatus.DELETED)

    task.status = TaskStatus.DELETED.value
    task.updated_at = datetime.now()
    get_repository().save(task)
    return {"deleted": True}


def _task_summary(task: Any) -> dict:
    return {
        "thread_id": task.thread_id,
        "status": task.status,
        "stage": task.stage,
        "query": task.query,
        "draft_report": task.draft_report,
        "final_report": task.final_report,
        "verification": task.verification,
        "error": task.error,
        "attempt": task.attempt,
        "cancel_requested": task.cancel_requested,
    }
