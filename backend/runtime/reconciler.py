"""Orphan Reconciler —— 回收「没人管」的非终态任务。

见 `CLAUDE_CODE_SERVER_V1_LOCAL_MODEL_EXECUTION_PLAN.md` §6。

Phase G 已支持 claim 过期与崩溃恢复，但仍有一个缺口：

```text
Task = RUNNING
没有有效 Claim
Queue 中没有有效 Job     ← 没人会再碰它，永久卡住
```

典型来源：API 入队失败（返回 503）后用户没重试；或 job 被 ACK 但任务
未被推进到终态。

**本模块只做一件事：把「无 claim 且无 job」的非终态任务重新入队。**

幂等性依赖既有机制，不另造互斥系统（§6.3）：

```text
queue dedupe     抑制瞬时重复投递
Atomic Claim     最终安全边界 —— 即使重复入队也只有一个 worker 执行
```
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from backend.domain.task import TERMINAL_STATUSES, TaskStatus, UnknownTaskStatus, coerce_status
from backend.runtime import queue as q
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)


class OrphanReconciler:
    """扫描并回收孤儿任务。"""

    def __init__(
        self,
        *,
        redis: Any,
        repository: Any,
        settings: Any,
        worker_id: str = "",
    ) -> None:
        self._redis = redis
        self._repo = repository
        self._settings = settings
        self._worker_id = worker_id

    async def sweep(self) -> list[str]:
        """扫描一轮，返回本次被重新入队的 thread_id。

        不在 Redis 上做任何写操作，除了最终那次 enqueue。
        """
        try:
            outstanding = await q.outstanding_thread_ids(self._redis)
        except Exception as exc:
            # 无法判断队列状态时**不做任何事** —— 宁可漏收，不可误投
            logger.warning("reconciler 无法读取队列状态，本轮跳过: %s", exc)
            return []

        from backend.runtime import claim as claim_mod

        requeued: list[str] = []
        for task in self._repo.list_tasks(include_deleted=True):
            try:
                reason = await self._orphan_reason(task, outstanding, claim_mod)
            except Exception as exc:
                logger.warning("reconciler 检查任务 %s 失败: %s", task.thread_id, exc)
                continue

            if reason is None:
                continue

            try:
                message_id = await q.enqueue(
                    self._redis,
                    task.thread_id,
                    dedupe_ttl_ms=int(self._settings.job_dedupe_ttl_seconds * 1000),
                )
            except Exception as exc:
                logger.warning("reconciler 重新入队 %s 失败: %s", task.thread_id, exc)
                continue

            # `enqueue` 返回 None 表示被 dedupe 抑制（另一个 reconciler 或
            # API 刚投过）。那不是本轮的成果，不应计入——否则并发扫描时
            # 会报告「入队了两次」，虽无实际危害但会掩盖真实行为。
            if message_id is None:
                logger.debug("reconciler 跳过 %s：已有并发投递", task.thread_id)
                continue

            requeued.append(task.thread_id)
            logger.info("reconciler 重新入队孤儿任务 %s（原因: %s）", task.thread_id, reason)
            await self._record_recovery(task.thread_id, reason)

        return requeued

    async def _record_recovery(self, thread_id: str, reason: str) -> None:
        """旁路记录 recovery 事件（观测用；失败不影响回收逻辑）。"""
        try:
            from backend.runtime.redis import resolve_run_id
            from deep_research.callbacks.baseline_metrics import append_reliability_event

            run_id = await resolve_run_id(self._redis, thread_id)
            append_reliability_event(
                run_id, thread_id, "recovery_requeued",
                worker_id=self._worker_id, reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("recovery 事件记录失败（已忽略）: %s", exc)

    async def _orphan_reason(
        self,
        task: Any,
        outstanding: set[str],
        claim_mod: Any,
    ) -> str | None:
        """判断任务是否为孤儿；是则返回原因描述，否则返回 None。"""
        try:
            status = coerce_status(task.status)
        except UnknownTaskStatus:
            # 状态不可识别的任务不擅自动它
            logger.warning("任务 %s 状态未知（%r），跳过", task.thread_id, task.status)
            return None

        # --- 终态永不重新调度（§6.2）---
        if status in TERMINAL_STATUSES or status is TaskStatus.DELETED:
            return None

        # --- 等待人工审核是**正常状态**，不是孤儿（§6.4）---
        # 重新投递普通 run job 没有意义：worker 会再次发现停在 human_review。
        if status is TaskStatus.WAITING_REVIEW:
            return None

        # --- 有 claim 在身 → 有 worker 正在/将会处理 ---
        if await claim_mod.holder(self._redis, task.thread_id) is not None:
            return None

        # --- 队列里还有它的 job → 等着被消费即可 ---
        if task.thread_id in outstanding:
            return None

        # --- 宽限期：避免与「刚创建、job 尚未被消费」的任务竞争 ---
        if not self._past_grace_period(task):
            return None

        return f"status={status.value} 无 claim 无 job"

    def _past_grace_period(self, task: Any) -> bool:
        """任务是否已安静足够久，可以判定为孤儿。

        没有这道判断的话，reconciler 会与刚入队的任务竞争——
        那个任务其实只是还没被 worker 取走。
        """
        grace = float(getattr(self._settings, "orphan_grace_seconds", 60.0))
        if grace <= 0:
            return True

        updated = task.updated_at or task.created_at
        if updated is None:
            return True
        return datetime.now() - updated > timedelta(seconds=grace)


__all__ = ["OrphanReconciler"]
