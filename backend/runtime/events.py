"""Event Plane —— Redis Streams。

事件流约定由本模块定义。

**按任务分片**（`dr:events:{thread_id}`）而非全局单流：

* replay 只需从某个 stream id 继续读
* 保留策略可按任务设置 MAXLEN，不同任务不互相驱逐
* 一个任务的洪水不会挤掉另一个任务的事件

两个「序号」概念职责不同，不可混用：

```text
sequence  业务序号，由 INCR dr:evseq:{tid} 原子分配，任务内单调递增
          —— 稳定，可用于去重与排序断言

stream id Redis 生成的 transport cursor（形如 1712345678901-0）
          —— 用于 Last-Event-ID 与 XREAD 定位
```
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from backend.runtime.redis import event_seq_key, events_key
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)


class EventType:
    """canonical 事件词表（设计文档 §9.3）。

    与浏览器契约的映射**不在这一层**——投影层负责翻译成前端已知的
    6 个名字（见 backend/runtime/sse.py）。
    """

    TASK_QUEUED = "task.queued"
    TASK_CLAIMED = "task.claimed"
    TASK_STARTED = "task.started"
    TASK_RESUMED = "task.resumed"
    TASK_WAITING_REVIEW = "task.waiting_review"
    TASK_CANCEL_REQUESTED = "task.cancel_requested"
    TASK_CANCELLED = "task.cancelled"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"

    GRAPH_NODE_STARTED = "graph.node.started"
    GRAPH_NODE_COMPLETED = "graph.node.completed"
    AGENT_STARTED = "agent.started"
    AGENT_COMPLETED = "agent.completed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"


# 绝不写入事件的字段（设计文档 §9.6）
_FORBIDDEN_KEYS = {
    "api_key", "apikey", "authorization", "secret", "password", "token",
    "env", "environ", "prompt", "raw_content",
}


def _scrub(data: dict[str, Any]) -> dict[str, Any]:
    """剔除敏感字段。事件会被长期保留，不能把密钥写进去。"""
    return {k: v for k, v in data.items() if k.lower() not in _FORBIDDEN_KEYS}


async def next_sequence(redis: Any, thread_id: str) -> int:
    """原子分配任务内的下一个事件序号。"""
    return int(await redis.incr(event_seq_key(thread_id)))


async def publish(
    redis: Any,
    thread_id: str,
    event_type: str,
    data: dict[str, Any] | None = None,
    *,
    worker_id: str = "",
    maxlen: int = 1000,
) -> str | None:
    """发布一个事件。

    返回 stream message id；Redis 不可达时返回 None 并记 warning——
    **事件是可投影的派生数据，不是事实源**（设计文档 §11）：
    事件丢失由投影层按 DB 兜底，不应因此中断任务执行。
    """
    try:
        sequence = await next_sequence(redis, thread_id)
        payload = {
            "task_id": thread_id,
            "sequence": sequence,
            "type": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "worker_id": worker_id,
            "data": _json_dumps(_scrub(data or {})),
        }
        message_id = await redis.xadd(
            events_key(thread_id), payload, maxlen=maxlen, approximate=True
        )
        return message_id
    except Exception as exc:
        logger.warning("事件发布失败（不影响任务事实）: %s", exc)
        return None


async def read_since(
    redis: Any,
    thread_id: str,
    *,
    after_id: str = "0-0",
    count: int = 100,
) -> list[dict[str, Any]]:
    """从 ``after_id`` 之后读取事件（回放）。"""
    try:
        response = await redis.xrange(events_key(thread_id), min=f"({after_id}", count=count)
    except Exception as exc:
        logger.warning("读取事件失败: %s", exc)
        return []

    return [_decode(message_id, fields) for message_id, fields in response or []]


async def read_blocking(
    redis: Any,
    thread_id: str,
    *,
    after_id: str = "$",
    block_ms: int = 1000,
    count: int = 100,
) -> list[dict[str, Any]]:
    """阻塞读取新事件。``after_id="$"`` 表示只读连接之后产生的。"""
    try:
        response = await redis.xread({events_key(thread_id): after_id}, count=count, block=block_ms)
    except Exception as exc:
        logger.warning("阻塞读取事件失败: %s", exc)
        return []

    out: list[dict[str, Any]] = []
    for _stream, entries in response or []:
        out.extend(_decode(message_id, fields) for message_id, fields in entries)
    return out


def _decode(message_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """解开一条 Stream 条目为事件 envelope（§8）。

    `task_id` 冗余存在于 payload 中（虽然 stream key 里也有），
    这样投影层不必依赖 stream key 的解析就能拿到它。
    """
    return {
        "event_id": message_id,
        "task_id": fields.get("task_id", ""),
        "sequence": int(fields.get("sequence", 0) or 0),
        "type": fields.get("type", ""),
        "timestamp": fields.get("timestamp", ""),
        "worker_id": fields.get("worker_id", ""),
        "data": _json_loads(fields.get("data")),
    }


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def _json_loads(value: Any) -> Any:
    import json

    if value is None:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


__all__ = [
    "EventType",
    "next_sequence",
    "publish",
    "read_blocking",
    "read_since",
]
