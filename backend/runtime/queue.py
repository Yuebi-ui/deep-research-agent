"""Job Transport —— Redis Stream + consumer group。

见 docs/phase-g-runtime-design.md §8。

**为什么用 Stream 而非 LIST**：Stream 原生提供 at-least-once 语义、`XACK`
确认、`XPENDING` / `XAUTOCLAIM` 未确认消息恢复——正好覆盖执行包 §9 要求的
ack / recovery，无需自己实现 processing list 与超时回收。

**不引入 Celery / RQ**（§3、§9 明令）。

Job 统一为单一 operation `run`（§9 允许）。worker 依据 DB 状态 + checkpoint
决定实际是首次执行还是 resume —— 状态驱动天然幂等，重复投递不会走不同分支。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from backend.runtime.redis import JOBS_GROUP, JOBS_STREAM, job_dedupe_key
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)

OPERATION_RUN = "run"


@dataclass(frozen=True)
class Job:
    """一个待执行的 job。"""

    job_id: str
    thread_id: str
    operation: str
    attempt: int
    created_at: float
    message_id: str = ""

    def to_fields(self) -> dict[str, str]:
        return {
            "job_id": self.job_id,
            "thread_id": self.thread_id,
            "operation": self.operation,
            "attempt": str(self.attempt),
            "created_at": str(self.created_at),
        }

    @classmethod
    def from_fields(cls, message_id: str, fields: dict[str, str]) -> Job:
        return cls(
            job_id=fields.get("job_id", ""),
            thread_id=fields.get("thread_id", ""),
            operation=fields.get("operation", OPERATION_RUN),
            attempt=int(fields.get("attempt", 0) or 0),
            created_at=float(fields.get("created_at", 0) or 0),
            message_id=message_id,
        )


class EnqueueRejected(RuntimeError):
    """入队被拒（Redis 不可达）。API 应据此返回 503，且**不**改变任务状态。"""


async def ensure_group(redis: Any) -> None:
    """确保 consumer group 存在。已存在时静默通过。"""
    try:
        await redis.xgroup_create(JOBS_STREAM, JOBS_GROUP, id="0", mkstream=True)
    except Exception as exc:
        # BUSYGROUP = 已存在，属正常
        if "BUSYGROUP" not in str(exc):
            raise


async def enqueue(
    redis: Any,
    thread_id: str,
    *,
    operation: str = OPERATION_RUN,
    attempt: int = 0,
    dedupe_ttl_ms: int = 5000,
) -> str | None:
    """投递一个 job。

    返回 stream message id；被 dedupe 抑制时返回 None。

    **dedupe 仅用于抑制瞬时重复投递，不是安全边界**（§9 明令）：
    最终安全边界是 Atomic Claim。
    """
    # ttl <= 0 表示「不启用 dedupe」。
    # 不能直接把 0 传给 Redis —— 会得到
    # `invalid expire time in 'set' command`。
    if dedupe_ttl_ms > 0:
        dedupe_key = job_dedupe_key(thread_id)
        try:
            allowed = await redis.set(dedupe_key, "1", nx=True, px=dedupe_ttl_ms)
        except Exception as exc:
            raise EnqueueRejected(f"Redis 不可达，无法投递 job: {exc}") from exc

        if not allowed:
            logger.debug("job 被 dedupe 抑制: %s", thread_id)
            return None

    job = Job(
        job_id=uuid.uuid4().hex,
        thread_id=thread_id,
        operation=operation,
        attempt=attempt,
        created_at=time.time(),
    )
    try:
        message_id = await redis.xadd(JOBS_STREAM, job.to_fields())
    except Exception as exc:
        # 投递失败要让 API 明确返回 503，并**不**把任务标记为 FAILED
        raise EnqueueRejected(f"Redis 不可达，无法投递 job: {exc}") from exc

    logger.info("job 已投递: thread=%s op=%s id=%s", thread_id, operation, message_id)
    return message_id


async def consume(
    redis: Any,
    worker_id: str,
    *,
    block_ms: int = 1000,
    count: int = 1,
) -> list[Job]:
    """以 consumer 身份读取新 job（阻塞最多 block_ms 毫秒）。

    读到即进入 pending（未 ack）状态；处理完成后必须调用 :func:`ack`。
    """
    try:
        response = await redis.xreadgroup(
            JOBS_GROUP, worker_id, {JOBS_STREAM: ">"}, count=count, block=block_ms
        )
    except Exception as exc:
        logger.warning("消费 job 失败: %s", exc)
        return []

    return _parse(response)


async def reclaim_stale(
    redis: Any,
    worker_id: str,
    *,
    min_idle_ms: int,
    count: int = 10,
) -> list[Job]:
    """接管其他 consumer 长时间未确认的 job。

    覆盖「worker 崩溃导致 job 永远 pending」的场景。返回的 job 归属当前
    worker，处理完同样需要 :func:`ack`。
    """
    try:
        result = await redis.xautoclaim(
            JOBS_STREAM, JOBS_GROUP, worker_id, min_idle_time=min_idle_ms, count=count
        )
    except Exception as exc:
        logger.warning("接管 pending job 失败: %s", exc)
        return []

    # redis-py 的返回形态随版本略有差异：(cursor, entries) 或 (cursor, entries, deleted)
    entries = result[1] if isinstance(result, (list, tuple)) and len(result) > 1 else []
    return [
        Job.from_fields(mid, fields)
        for mid, fields in entries
        if fields  # 已被 XDEL 的条目 fields 为空
    ]


async def ack(redis: Any, job: Job) -> None:
    """确认处理完成。"""
    if not job.message_id:
        return
    try:
        await redis.xack(JOBS_STREAM, JOBS_GROUP, job.message_id)
    except Exception as exc:
        logger.warning("XACK 失败（job 会被重新投递）: %s", exc)


async def pending_count(redis: Any) -> int:
    """当前 pending（未 ack）的 job 数。供 readiness / 观测使用。"""
    try:
        summary = await redis.xpending(JOBS_STREAM, JOBS_GROUP)
        return int(summary.get("pending", 0)) if isinstance(summary, dict) else 0
    except Exception:
        return 0


async def outstanding_thread_ids(redis: Any) -> set[str]:
    """当前队列中仍有未完成 job 的 thread_id 集合。

    覆盖两类「未完成」：

    ```text
    1. 已投递但未 ACK（pending）  —— worker 正在处理，或 worker 崩溃遗留
    2. 尚未投递给任何 consumer    —— 刚入队、还在流里等着
    ```

    Orphan Reconciler 用它判断「这个任务是否还有 job 在路上」。
    只返回 thread_id，不关心 job 内容，也不改变任何队列状态。
    """
    thread_ids: set[str] = set()

    # --- 1. pending（已投递未 ack）---
    try:
        pending = await redis.xpending_range(
            JOBS_STREAM, JOBS_GROUP, min="-", max="+", count=1000
        )
    except Exception:
        pending = []

    for entry in pending or []:
        message_id = entry.get("message_id") if isinstance(entry, dict) else None
        if message_id:
            fields = await _read_fields(redis, message_id)
            if fields.get("thread_id"):
                thread_ids.add(fields["thread_id"])

    # --- 2. 尚未投递的（id 大于 last-delivered-id）---
    try:
        groups = await redis.xinfo_groups(JOBS_STREAM)
        last_id = None
        for g in groups or []:
            if isinstance(g, dict) and g.get("name") == JOBS_GROUP:
                last_id = g.get("last-delivered-id")
                break
        if last_id is not None:
            undelivered = await redis.xrange(
                JOBS_STREAM, min=f"({last_id}", max="+", count=1000
            )
            for _mid, fields in undelivered or []:
                if fields and fields.get("thread_id"):
                    thread_ids.add(fields["thread_id"])
    except Exception:
        pass

    return thread_ids


async def _read_fields(redis: Any, message_id: str) -> dict[str, str]:
    """读取单条消息的字段。"""
    try:
        entries = await redis.xrange(JOBS_STREAM, min=message_id, max=message_id, count=1)
        if entries:
            return entries[0][1] or {}
    except Exception:
        pass
    return {}


def _parse(response: Any) -> list[Job]:
    """把 XREADGROUP 的返回解析成 Job 列表。"""
    if not response:
        return []

    jobs: list[Job] = []
    for _stream, entries in response:
        for message_id, fields in entries:
            try:
                jobs.append(Job.from_fields(message_id, fields or {}))
            except Exception as exc:
                logger.warning("无法解析 job 条目 %s: %s", message_id, exc)
    return jobs


__all__ = [
    "EnqueueRejected",
    "Job",
    "OPERATION_RUN",
    "JOBS_GROUP",
    "JOBS_STREAM",
    "ack",
    "consume",
    "ensure_group",
    "enqueue",
    "pending_count",
    "reclaim_stale",
]
