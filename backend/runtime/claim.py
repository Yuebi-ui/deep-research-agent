"""Atomic Execution Claim —— 任务执行的**唯一**互斥。

互斥语义由本模块实现。

核心原则：

```text
TaskStatus  = 业务状态（用户可见）
Claim       = 运行时互斥（Redis + TTL）

不得把 status == RUNNING 当作 claim —— 两者生命周期不同：
任务可以在没有 claim 的情况下处于 RUNNING（worker 崩溃后、claim 过期前）。
```

获取必须原子：`SET key value NX PX ttl`。
**禁止** `GET → if empty → SET`（执行包 §6 明令）。
续约与释放必须比较 owner，用 Lua 保证原子。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.runtime.redis import claim_key
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)

# 仅 owner 能续约
_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

# 仅 owner 能释放
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


@dataclass(frozen=True)
class ClaimResult:
    """claim 的尝试结果。"""

    acquired: bool
    thread_id: str
    worker_id: str
    holder: str | None = None

    @property
    def held_by_other(self) -> bool:
        return not self.acquired and self.holder not in (None, self.worker_id)


async def acquire(redis: Any, thread_id: str, worker_id: str, ttl_ms: int) -> ClaimResult:
    """尝试获取执行权。

    成功返回 ``acquired=True``；失败时 ``holder`` 为当前持有者（可能为 None，
    表示刚好在 SET 与 GET 之间被释放）。
    """
    key = claim_key(thread_id)

    # SET key value NX PX ttl —— 单条命令，原子
    ok = await redis.set(key, worker_id, nx=True, px=ttl_ms)
    if ok:
        logger.debug("claim 获取成功: %s -> %s", thread_id, worker_id)
        return ClaimResult(True, thread_id, worker_id)

    holder = await redis.get(key)
    logger.debug("claim 失败: %s 已被 %s 持有", thread_id, holder)
    return ClaimResult(False, thread_id, worker_id, holder=holder)


async def renew(redis: Any, thread_id: str, worker_id: str, ttl_ms: int) -> bool:
    """续约。仅 owner 成功。

    返回 False 说明已失去所有权（claim 过期后被他人接管，或 Redis 异常）。
    调用方应立即停止当前执行——继续跑会制造双执行。
    """
    result = await redis.eval(_RENEW_LUA, 1, claim_key(thread_id), worker_id, ttl_ms)
    return bool(result)


async def release(redis: Any, thread_id: str, worker_id: str) -> bool:
    """释放。仅 owner 成功。"""
    result = await redis.eval(_RELEASE_LUA, 1, claim_key(thread_id), worker_id)
    if result:
        logger.debug("claim 已释放: %s", thread_id)
    return bool(result)


async def holder(redis: Any, thread_id: str) -> str | None:
    """当前持有者；无人持有时返回 None。"""
    return await redis.get(claim_key(thread_id))
