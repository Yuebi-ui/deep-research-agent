"""Legacy best-effort post-completion task tracker.

The default Memory 3.0 path uses the durable SQLite outbox in
``backend.runtime.memory_outbox``. This module is intentionally retained as a
rollback/embedded-runtime fallback when ``DR_MEMORY_OUTBOX_ENABLED=off`` and for
other bounded post-completion work.

The tracker isolates callback context, contains failures, and provides bounded
drain/wait semantics. Because this fallback is not persistent, process crashes may
lose its derived work; the completed report remains the source of truth and can be
re-enriched later.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from typing import Any, Awaitable, Callable

from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)


class PostCompletionTasks:
    """跟踪 / 等待 post-completion 后台任务。"""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    # ---- 调度 ----

    def schedule(
        self,
        factory: Callable[[], Awaitable[Any]],
        *,
        name: str,
        on_done: Callable[[Any, Exception | None, float], None] | None = None,
    ) -> asyncio.Task | None:
        """在当前事件循环里调度一个 post-completion 任务。

        ``factory`` 是**零参协程函数**（而不是协程对象）：协程在空 context 中
        才被创建，确保不继承调用方的 contextvars。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("post-completion 任务 %s 无法调度：没有运行中的事件循环", name)
            return None

        async def _wrapped() -> Any:
            started = time.monotonic()
            result: Any = None
            error: Exception | None = None
            try:
                result = await factory()
                return result
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 后台任务失败必须就地隔离
                error = exc
                logger.warning("post-completion 任务 %s 失败（已隔离）: %s", name, exc, exc_info=True)
                return None
            finally:
                elapsed = time.monotonic() - started
                logger.info("post-completion 任务 %s 结束: outcome=%s elapsed=%.2fs",
                            name, "failed" if error else "ok", elapsed)
                if on_done is not None:
                    try:
                        on_done(result, error, elapsed)
                    except Exception:  # noqa: BLE001
                        logger.debug("post-completion on_done 回调失败（已忽略）", exc_info=True)

        try:
            # context=contextvars.Context() —— 全新的空 context（不复制调用方）
            task = loop.create_task(_wrapped(), context=contextvars.Context(), name=name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("post-completion 任务 %s 调度失败（已忽略）: %s", name, exc)
            return None

        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---- 查询 / 等待 ----

    def pending(self) -> int:
        return sum(1 for t in self._tasks if not t.done())

    def names(self) -> list[str]:
        return sorted(t.get_name() for t in self._tasks if not t.done())

    async def wait_idle(self) -> int:
        """等待全部在飞任务结束（worker 在消费下一个 job 之前调用）。

        单个任务内部已经做了异常隔离，这里用 ``return_exceptions=True`` 兜底，
        保证等待本身永不抛出。
        """
        tasks = [t for t in self._tasks if not t.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    async def drain(self, *, timeout: float = 30.0) -> int:
        """优雅退出：等待在飞任务，返回被放弃（超时）的数量。

        超时后取消任务：这些是 best-effort 派生数据，报告已落库，允许丢失。
        """
        tasks = [t for t in self._tasks if not t.done()]
        if not tasks:
            return 0
        logger.info("等待 %d 个 post-completion 任务结束（timeout=%.0fs）", len(tasks), timeout)
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            logger.warning(
                "丢弃 %d 个未完成的 post-completion 任务（best-effort 派生数据，可由报告重建）: %s",
                len(pending), sorted(t.get_name() for t in pending),
            )
        return len(pending)
