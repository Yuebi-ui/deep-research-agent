"""Worker Runtime 入口。

    python -m backend.worker

见 docs/phase-g-runtime-design.md §17。

**这是唯一正式的 LangGraph 执行者。** API 进程不再构建图、不再驱动执行。

生命周期：

```text
连接 Redis → 校验 checkpointer → 校验 DB schema
→ 以 consumer group 成员身份消费 dr:jobs
→ 每个 job：atomic claim → 执行 → 落库 → emit → 释放 claim → ack
→ SIGTERM/SIGINT → 停止消费 → 跑到安全边界 → 停止心跳（不释放 claim）→ 退出
```

**不释放 claim** 是刻意的（§14）：图可能正跑在节点中途，主动释放会让另一个
worker 立刻接管并重复执行当前节点。让 claim 自然过期可保证 TTL 窗口内
绝无第二执行者。
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Any

from backend.core.errors import InfrastructureError
from backend.core.settings import get_settings
from backend.runtime import claim as claim_mod
from backend.runtime import queue as q
from backend.runtime import redis as rt_redis
from backend.runtime.identity import new_worker_id
from backend.runtime.post_completion import PostCompletionTasks
from backend.runtime.runner import TaskRunner
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)

# 退出时为 post-completion 任务留的收尾窗口（实测单次 11–20s，留足余量但必须有界）
_POST_COMPLETION_DRAIN_TIMEOUT_S = 60.0


class Worker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.worker_id = new_worker_id()
        self._redis = None
        self._checkpointer = None
        self._shutdown = asyncio.Event()
        self._current: str | None = None
        # Phase 4C-lite：post-completion 后台任务（best-effort 记忆落库）。
        # worker 是唯一持有它的地方 —— 负责“下一个 job 之前 wait_idle”和
        # “退出时 drain”，避免裸 create_task 的 fire-and-forget。
        self._post_completion = PostCompletionTasks()
        from backend.runtime.memory_outbox import MemoryOutboxProcessor, enabled as memory_outbox_enabled
        self._memory_outbox = (MemoryOutboxProcessor(owner=self.worker_id)
                               if memory_outbox_enabled() else None)

    # ------------------------------------------------------------------

    async def start(self) -> None:
        logger.info("worker 启动: %s", self.worker_id)

        # 与 API 侧一致：schema / checkpointer 不就绪就不启动，不静默降级
        from backend.db.engine import get_engine
        from backend.db.schema import assert_schema_up_to_date

        assert_schema_up_to_date(get_engine())

        from deep_research import checkpoint as checkpoint_factory

        managed = await checkpoint_factory.init_checkpointer()
        if managed.backend == "memory":
            raise InfrastructureError(
                "worker 需要持久化 checkpointer（跨进程恢复的前提），"
                "当前解析为 memory。请设置 CHECKPOINTER_BACKEND=redis 或 sqlite。"
            )
        self._checkpointer = managed.saver
        logger.info("checkpointer 就绪: %s", managed.backend)

        self._redis = rt_redis.get_redis()
        if not await rt_redis.ping():
            raise InfrastructureError("Redis 不可达，worker 无法启动")
        await q.ensure_group(self._redis)

        logger.info("worker 就绪，开始消费 %s", q.JOBS_STREAM)

    async def stop(self) -> None:
        self._shutdown.set()

    # ------------------------------------------------------------------

    async def serve(self) -> None:
        """主循环。

        每轮：

        1. 周期性地回收孤儿任务（无 claim 无 job 的非终态任务）
        2. 接管其他 worker 崩溃后遗留的 pending job
        3. 阻塞读取新 job

        三者都走同一条处理路径——claim 决定谁真正执行。
        """
        assert self._redis is not None

        next_sweep = 0.0
        next_memory_poll = 0.0
        while not self._shutdown.is_set():
            # --- 孤儿回收（周期执行，不每轮都扫）---
            loop_time = asyncio.get_running_loop().time()
            if loop_time >= next_sweep:
                await self._sweep_orphans()
                next_sweep = loop_time + self.settings.orphan_sweep_interval_seconds

            # Persistent memory outbox: process a bounded amount per loop.
            # Set DR_MEMORY_OUTBOX_POLL_ON_WORKER=off if a dedicated memory
            # worker runs; never allow memory failure to mark a task failed.
            if (self._memory_outbox is not None and loop_time >= next_memory_poll
                    and os.getenv("DR_MEMORY_OUTBOX_POLL_ON_WORKER", "on")
                    .strip().lower() not in {"0", "off", "false"}):
                try:
                    worked = await self._memory_outbox.process_one()
                    next_memory_poll = asyncio.get_running_loop().time() + (0.5 if worked else 4.0)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("memory outbox poll failed; will retry: %s", exc)
                    next_memory_poll = asyncio.get_running_loop().time() + 4.0

            # 接管超时未 ack 的 job（worker 崩溃恢复）
            stale = await q.reclaim_stale(
                self._redis, self.worker_id,
                min_idle_ms=self.settings.claim_ttl_ms * 2,
            )
            for job in stale:
                if self._shutdown.is_set():
                    break
                await self._handle(job, reclaimed_stale=True)

            if self._shutdown.is_set():
                break

            # Legacy fallback only: if durable outbox is disabled, wait for
            # best-effort post-completion work before consuming the next job.
            pending = self._post_completion.pending()
            if pending:
                logger.info("等待 %d 个 post-completion 任务结束后再消费下一个 job", pending)
                await self._post_completion.wait_idle()

            jobs = await q.consume(self._redis, self.worker_id, block_ms=1000)
            for job in jobs:
                if self._shutdown.is_set():
                    # 已收到停止信号：不 ack，让 job 留给下一个 worker
                    break
                await self._handle(job)

    async def _sweep_orphans(self) -> None:
        """回收「无 claim 无 job」的非终态任务。

        失败只记 warning 并跳过本轮 —— reconciler 是补偿机制，
        不应因为它自身出错而影响正常 job 的处理。
        """
        assert self._redis is not None
        try:
            from backend.runtime.reconciler import OrphanReconciler

            reconciler = OrphanReconciler(
                redis=self._redis,
                repository=self._repository(),
                settings=self.settings,
                worker_id=self.worker_id,
            )
            requeued = await reconciler.sweep()
            if requeued:
                logger.info("orphan reconciler 重新入队 %d 个任务", len(requeued))
        except Exception as exc:
            logger.warning("orphan reconciler 扫描失败: %s", exc, exc_info=True)

    async def _handle(self, job: q.Job, *, reclaimed_stale: bool = False) -> None:
        assert self._redis is not None
        thread_id = job.thread_id
        self._current = thread_id

        if reclaimed_stale:
            await self._record_reliability(
                thread_id, "job_reclaimed_stale", message_id=job.message_id
            )

        # Atomic Claim —— 唯一的安全边界。抢不到就直接放弃，
        # 由持有者负责（设计文档 §2）。
        result = await claim_mod.acquire(
            self._redis, thread_id, self.worker_id, self.settings.claim_ttl_ms
        )
        if not result.acquired:
            logger.info(
                "任务 %s 已被 %s 持有，跳过", thread_id, result.holder
            )
            await self._record_reliability(
                thread_id, "claim_not_acquired", holder=result.holder,
                job_message_id=job.message_id,
            )
            await q.ack(self._redis, job)
            self._current = None
            return

        # Only atomic-finalization failures bypass ACK; the checkpoint can
        # resume settlement without re-running research or charging another LLM.
        enqueue_failed = False
        runner = TaskRunner(
            redis=self._redis,
            worker_id=self.worker_id,
            settings=self.settings,
            repository=self._repository(),
            checkpointer=self._checkpointer,
            post_completion=self._post_completion,
        )

        try:
            outcome = await runner.run(thread_id)
            logger.info(
                "任务 %s 执行结束: status=%s retryable=%s",
                thread_id, outcome.status, outcome.retryable,
            )
        except Exception as exc:
            from backend.runtime.memory_outbox import MemoryOutboxEnqueueError
            enqueue_failed = isinstance(exc, MemoryOutboxEnqueueError)
            logger.error("任务 %s 执行异常: %s", thread_id, exc, exc_info=True)
        finally:
            self._current = None
            if runner.ownership_lost:
                # P0 ownership fencing：stale owner 不释放、不 ACK ——
                # ACK 是全局的（无 owner 语义），ack 一个已被他人接管的执行
                # 会破坏 recovery 的 pending 跟踪。留着 PEL 条目，
                # 由 reclaim_stale / reconciler 合法接管。
                logger.warning(
                    "任务 %s 已失去 claim 所有权：跳过 release/ACK，交由 recovery 接管",
                    thread_id,
                )
                await self._record_reliability(
                    thread_id, "stale_execution_abandoned",
                    reason="ownership_lost", job_message_id=job.message_id,
                )
            else:
                owned = await self._claimed_by_us(thread_id)
                if owned is False:
                    # ACK 前的最终围栏：核验发现 claim 已不属于本 worker
                    logger.warning(
                        "任务 %s 的 claim 已不属于本 worker：跳过 release/ACK",
                        thread_id,
                    )
                    await self._record_reliability(
                        thread_id, "stale_execution_abandoned",
                        reason="pre_ack_verify", job_message_id=job.message_id,
                    )
                else:
                    if owned is None:
                        logger.debug("任务 %s claim 归属无法核验，按既有路径处理", thread_id)
                    # 正常结束与 HITL 等待都释放 claim：等待人工审核期间**不能**持有
                    # claim，否则 TTL 过期后另一个 worker 会 resume 一个用户尚未
                    # 批准的任务（设计文档 §7）。
                    await claim_mod.release(self._redis, thread_id, self.worker_id)
                    if enqueue_failed:
                        # Keep Redis PEL entry for reclaim_stale; acknowledging
                        # here would strand an otherwise completed graph.
                        logger.warning("outbox 原子提交失败，保留 pending Redis job: %s", thread_id)
                    else:
                        await q.ack(self._redis, job)

    async def _claimed_by_us(self, thread_id: str) -> bool | None:
        """ACK 前的归属核验。异常时返回 None（不改变既有处理路径）。"""
        try:
            return await claim_mod.holder(self._redis, thread_id) == self.worker_id
        except Exception as exc:  # noqa: BLE001
            logger.debug("claim 归属核验失败（已忽略）: %s", exc)
            return None

    def _repository(self):
        from backend.db import get_repository

        return get_repository()

    async def _record_reliability(self, thread_id: str, event: str, **details: Any) -> None:
        """旁路记录 reliability 事件（观测用；失败不影响任务）。"""
        try:
            from backend.runtime.redis import resolve_run_id
            from deep_research.callbacks.baseline_metrics import append_reliability_event

            run_id = await resolve_run_id(self._redis, thread_id)
            append_reliability_event(
                run_id, thread_id, event, worker_id=self.worker_id, **details
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("reliability 记录失败（已忽略）: %s", exc)

    # ------------------------------------------------------------------

    async def run_until_signal(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))
            except NotImplementedError:
                # Windows 不支持 add_signal_handler —— 退化为 KeyboardInterrupt
                pass

        await self.start()
        try:
            await self.serve()
        finally:
            await self._shutdown_cleanup()

    async def _shutdown_cleanup(self) -> None:
        """优雅退出。

        **不释放当前任务的 claim**（设计文档 §14）：图可能正跑在节点中途，
        主动释放会让接管者重复执行当前节点。让 claim 自然过期。

        Legacy fallback post-completion tasks receive a bounded drain window.
        """
        logger.info("worker 正在退出（worker_id=%s）", self.worker_id)
        if self._current:
            logger.info(
                "任务 %s 的 claim 将自然过期（不主动释放，避免双执行）",
                self._current,
            )

        await self._post_completion.drain(timeout=_POST_COMPLETION_DRAIN_TIMEOUT_S)

        from deep_research import checkpoint as checkpoint_factory

        await checkpoint_factory.reset_checkpointer()
        await rt_redis.reset_redis()


def main() -> int:
    worker = Worker()
    try:
        asyncio.run(worker.run_until_signal())
    except KeyboardInterrupt:
        logger.info("worker 被中断")
    return 0


if __name__ == "__main__":
    sys.exit(main())
