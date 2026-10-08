"""Claim 心跳续约。

见 docs/phase-g-runtime-design.md §3。

```text
claim_ttl >= 3 × heartbeat_interval
```

续约失败的处理是安全边界，而不是日志问题（Phase 2 / P0）：

* **瞬时失败不误杀**：Redis 抖动导致单次续约异常时，先核实所有权
  （``GET claim_key``），只要仍是本 worker 持有就继续重试续约；
  续约成功即回到正常（原先的实现在首次失败就放弃，代价是不必要的执行中断）。
* **确认丢失才放弃**：复核发现 claim 已属于他人或已消失（过期/被删）时，
  设置 ``lost`` 信号 —— 调用方（TaskRunner）在安全边界停止执行。
* **兜底期限**：Redis 状态完全无法确认（续约与复核都异常）时，以
  ``最后一次确认所有权的时间 + ttl - interval`` 为截止 —— 超过它，
  所有权已不可证明，必须停止（宁可中断，不可双跑）。

``lost`` 只表示「确定或按 lease 语义推定失去所有权」，不表示「停止续约后
claim 一定已被别人拿走」；执行方不得继续产生新的副作用。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from backend.runtime import claim as claim_mod
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)


class Heartbeat:
    """在独立 asyncio 任务中周期性续约。

    用法：

    ```python
    hb = Heartbeat(redis, thread_id, worker_id, interval_ms, ttl_ms)
    await hb.start()
    try:
        ...执行...
    finally:
        await hb.stop()
    ```

    ``lost`` 一旦置位不再复位；``loss_reason`` 记录判定依据
    （``renew_rejected_holder_other`` / ``claim_missing`` / ``lease_deadline_exceeded``）。
    """

    def __init__(
        self,
        redis: Any,
        thread_id: str,
        worker_id: str,
        *,
        interval_ms: int,
        ttl_ms: int,
        on_lost: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._redis = redis
        self._thread_id = thread_id
        self._worker_id = worker_id
        self._interval = max(interval_ms, 1) / 1000
        self._ttl_ms = ttl_ms
        self._on_lost = on_lost

        self._task: asyncio.Task[None] | None = None
        self._lost = asyncio.Event()
        self._last_confirmed: float | None = None  # monotonic，最近一次确认所有权
        self._degraded = False

        # 观测字段
        self.renewals = 0
        self.loss_reason: str | None = None

    @property
    def lost(self) -> bool:
        """是否已失去所有权（应当停止执行）。"""
        return self._lost.is_set()

    @property
    def degraded(self) -> bool:
        """是否处于「续约未被确认但仍在 lease 窗口内」的降级状态。"""
        return self._degraded

    async def start(self) -> None:
        if self._task is None:
            # claim 由调用方在此前刚 acquire 成功 —— 此刻所有权是确定的
            self._last_confirmed = time.monotonic()
            self._task = asyncio.create_task(self._run(), name=f"heartbeat:{self._thread_id}")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    # ------------------------------------------------------------------

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                now = time.monotonic()

                renewed = await self._try_renew(now)
                if renewed:
                    continue

                # 续约未被确认 —— 核实所有权，区分「瞬时抖动」与「确认丢失」
                state = await self._ownership_state()
                if state == "self":
                    # 仍是本 worker 持有（例如响应丢失 / LoRA 无关的瞬时 eval 失败）
                    if not self._degraded:
                        logger.warning(
                            "心跳续约未被确认，但 claim 仍由本 worker 持有，继续重试: %s",
                            self._thread_id,
                        )
                    self._degraded = True
                    continue

                if state in ("lost_other", "lost_missing"):
                    await self._lose(
                        "renew_rejected_holder_other" if state == "lost_other" else "claim_missing"
                    )
                    return

                # state == "unknown"：Redis 状态无法确认 —— lease 截止时间兜底
                self._degraded = True
                deadline = self._deadline(now)
                if now >= deadline:
                    await self._lose("lease_deadline_exceeded")
                    return
                logger.warning(
                    "心跳续约异常且所有权无法确认（将在 lease 截止前重试）: %s",
                    self._thread_id,
                )
        except asyncio.CancelledError:
            raise

    async def _try_renew(self, now: float) -> bool:
        try:
            ok = await claim_mod.renew(
                self._redis, self._thread_id, self._worker_id, self._ttl_ms
            )
        except Exception as exc:
            logger.debug("心跳续约异常: %s", exc)
            return False

        if ok:
            self._last_confirmed = now
            if self._degraded:
                logger.info("心跳续约已恢复: %s", self._thread_id)
                self._degraded = False
            self.renewals += 1
            return True
        return False

    async def _ownership_state(self) -> str:
        """返回 ``self`` / ``lost_other`` / ``lost_missing`` / ``unknown``。异常时不猜。"""
        try:
            holder = await claim_mod.holder(self._redis, self._thread_id)
        except Exception:
            return "unknown"
        if holder == self._worker_id:
            return "self"
        if holder is None:
            return "lost_missing"  # key 已消失（过期/被删）—— 所有权不可再证明
        return "lost_other"

    def _deadline(self, now: float) -> float:
        """最后确认时间 + ttl - interval（保守：留出一个心跳周期余量）。"""
        base = self._last_confirmed if self._last_confirmed is not None else now
        return base + max(self._ttl_ms / 1000 - self._interval, 0.0)

    async def _lose(self, reason: str) -> None:
        self.loss_reason = reason
        logger.warning(
            "claim 续约失败，已失去 %s 的所有权（reason=%s）—— 停止执行以免双跑",
            self._thread_id,
            reason,
        )
        # 先置位信号再回调：执行方的中止不依赖回调完成
        self._lost.set()
        if self._on_lost is not None:
            try:
                await self._on_lost()
            except Exception as exc:  # noqa: BLE001 —— 旁路回调不得影响判定
                logger.debug("on_lost 回调异常（已忽略）: %s", exc)
