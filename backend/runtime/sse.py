"""SSE 投影层 —— 只观察，不驱动。

SSE 行为由本模块直接定义。

**核心约束：本模块不得构建 LangGraph、不得写任务状态。**
它只做两件事：读 Redis Stream 事件、读 DB 兜底，然后翻译成稳定的 SSE 客户端契约。

**翻译是兼容性的落点**：Redis 里存的是 canonical 事件名
（`graph.node.started` 等），外部 UI/CLI 客户端消费 6 个兼容事件名。映射表见
:data:`_WIRE_NAMES`。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from backend.domain.task import TaskStatus, coerce_status
from backend.runtime.events import EventType, read_blocking, read_since
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)

# canonical → stable SSE client contract
_WIRE_NAMES: dict[str, str] = {
    EventType.GRAPH_NODE_STARTED: "node_start",
    EventType.GRAPH_NODE_COMPLETED: "node_complete",
    EventType.TOOL_STARTED: "tool_call",
    EventType.TOOL_COMPLETED: "tool_call",
    EventType.TASK_WAITING_REVIEW: "human_review_required",
    EventType.TASK_COMPLETED: "complete",
    EventType.TASK_FAILED: "error",
    EventType.TASK_CANCELLED: "error",
}

# 收到这些 canonical 事件后，投影可以结束（任务已到终局）
_TERMINAL_EVENTS = {
    EventType.TASK_COMPLETED,
    EventType.TASK_FAILED,
    EventType.TASK_CANCELLED,
}

# 仅用于可观测性、不发给浏览器的事件
_INTERNAL_EVENTS = {
    EventType.TASK_QUEUED,
    EventType.TASK_CLAIMED,
    EventType.TASK_STARTED,
    EventType.TASK_RESUMED,
    EventType.TASK_CANCEL_REQUESTED,
    EventType.AGENT_STARTED,
    EventType.AGENT_COMPLETED,
}

_POLL_BLOCK_MS = 1000
# 终态任务在收到事件后仍等待一小段时间，尽量把尾部事件也带出去
_DRAIN_TIMEOUT_SECONDS = 1.0


#: envelope 结构版本。字段增删时递增，供客户端做兼容判断。
ENVELOPE_SCHEMA_VERSION = "1"


def _frame(
    event_id: str,
    wire_name: str,
    data: dict[str, Any],
    *,
    task_id: str | None = None,
    sequence: int | None = None,
    timestamp: str | None = None,
) -> str:
    """构造 SSE 帧（执行计划 §8 的稳定 envelope）。

    同时保留历史的 `event` / `data` 两个字段名 —— 前端 switch 依赖它们，
    改名会破坏契约。其余是 envelope 元数据，前端可以忽略：

    ```json
    {
      "event": "node_start",          ← 前端契约
      "data": {...},                   ← 前端契约
      "event_id": "1712...-0",         ← 重连游标
      "task_id": "abc123",
      "sequence": 3,                   ← 任务内单调递增
      "timestamp": "...",
      "schema_version": "1"
    }
    ```

    `id:` 行是 SSE 标准的 Last-Event-ID 载体。
    """
    payload = json.dumps(
        {
            "event": wire_name,
            "data": data,
            "event_id": event_id,
            "task_id": task_id,
            "sequence": sequence,
            "timestamp": timestamp,
            "schema_version": ENVELOPE_SCHEMA_VERSION,
        },
        ensure_ascii=False,
    )
    return f"id: {event_id}\ndata: {payload}\n\n"


def _terminal_frame(task: Any) -> str | None:
    """按 DB 状态补发终态帧。

    用于「任务已终态但事件流已被 MAXLEN 驱逐」的兜底——DB 是事实源，
    不能让客户端连上来什么都收不到（设计文档 §10.4）。
    """
    if task is None:
        return None

    try:
        status = coerce_status(task.status)
    except Exception:
        return None

    common = {"task_id": task.thread_id, "timestamp": task.updated_at.isoformat() if task.updated_at else None}

    if status is TaskStatus.COMPLETED:
        return _frame("0-0", "complete", {"from_db": True}, **common)
    if status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
        return _frame("0-0", "error", {"message": task.error or status.value}, **common)
    return None


async def project(
    redis: Any,
    repository: Any,
    thread_id: str,
    *,
    last_event_id: str | None = None,
) -> AsyncIterator[str]:
    """把任务事件投影成 SSE 帧。

    生成器被取消（客户端断开）即结束——**不影响 worker**。
    """
    task = repository.get(thread_id)

    # 任务已终态：先回放已有事件，再补发终态，然后结束
    if task is not None and _is_terminal(task):
        async for frame in _replay(redis, thread_id, last_event_id):
            yield frame
        fallback = _terminal_frame(task)
        if fallback is not None:
            yield fallback
        return

    # 先回放历史
    cursor = last_event_id
    async for frame in _replay(redis, thread_id, cursor):
        yield frame
        cursor = _frame_id(frame) or cursor

    # 转实时
    async for frame in _live(redis, thread_id, cursor):
        yield frame


async def _replay(redis: Any, thread_id: str, last_event_id: str | None) -> AsyncIterator[str]:
    """回放 ``last_event_id`` 之后的事件。"""
    if last_event_id is None:
        # 首次连接：从头回放，避免漏掉任务早期事件
        after = "0-0"
    else:
        after = last_event_id

    try:
        events = await read_since(redis, thread_id, after_id=after)
    except Exception as exc:
        logger.warning("回放事件失败: %s", exc)
        return

    for event in events:
        frame = _to_frame(event)
        if frame is not None:
            yield frame


async def _live(redis: Any, thread_id: str, cursor: str | None) -> AsyncIterator[str]:
    """阻塞读取新事件，直到终态或客户端断开。"""
    last = cursor or "$"
    terminal_seen = False
    idle_rounds_after_terminal = 0

    while True:
        try:
            events = await read_blocking(redis, thread_id, after_id=last, block_ms=_POLL_BLOCK_MS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("实时读取事件失败: %s", exc)
            await asyncio.sleep(0.5)
            continue

        for event in events:
            last = event["event_id"]
            frame = _to_frame(event)
            if frame is not None:
                yield frame
            if event["type"] in _TERMINAL_EVENTS:
                terminal_seen = True

        if terminal_seen:
            # 再给一小段时间把尾部事件带出去，然后结束投影
            idle_rounds_after_terminal += 1
            if idle_rounds_after_terminal * (_POLL_BLOCK_MS / 1000) >= _DRAIN_TIMEOUT_SECONDS:
                return
        elif not events:
            # 空闲时确认任务是否已被外部置为终态（例如 API 直接取消）
            await asyncio.sleep(0)


def _to_frame(event: dict[str, Any]) -> str | None:
    """canonical 事件 → SSE 帧；不面向浏览器的事件返回 None。"""
    event_type = event.get("type", "")

    if event_type in _INTERNAL_EVENTS:
        return None

    wire_name = _WIRE_NAMES.get(event_type)
    if wire_name is None:
        logger.debug("未知事件类型，已忽略: %s", event_type)
        return None

    data = dict(event.get("data") or {})
    if wire_name == "error" and "message" not in data:
        data["message"] = data.get("code", "任务失败")

    return _frame(
        event["event_id"],
        wire_name,
        data,
        task_id=event.get("task_id"),
        sequence=event.get("sequence"),
        timestamp=event.get("timestamp"),
    )


def _frame_id(frame: str) -> str | None:
    if frame.startswith("id: "):
        return frame[4:].split("\n", 1)[0].strip()
    return None


def _is_terminal(task: Any) -> bool:
    try:
        return coerce_status(task.status) in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.DELETED,
        )
    except Exception:
        return False


__all__ = ["project"]
