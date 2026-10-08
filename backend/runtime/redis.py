"""Runtime 的 Redis 连接与命名空间。

与 checkpointer 的关系：checkpointer 自管连接（由
`deep_research.checkpoint` 负责）；本模块提供 **runtime** 侧连接
（claim / queue / events）。

刻意不共用连接：

* 生命周期不同——checkpointer 常驻，runtime 连接在 worker 启动时建立
* 故障语义不同——runtime Redis 不可达时 worker 应退避重试，
  而 checkpointer 不可达时应直接停止执行（见设计文档 §13）
"""

from __future__ import annotations

import os
from typing import Any

from deep_research import logging as dr_logging
from deep_research.settings import get_engine_settings
from deep_research.utils import load_config

logger = dr_logging.get_logger(__name__)


class RedisUnavailable(RuntimeError):
    """Runtime 所需的 Redis 不可用。

    与 checkpointer 侧一致：**不静默降级**。缺失 Redis 时 worker 不应
    假装能工作，API 也应在入队失败时明确返回 503。
    """


# ===== 命名空间（设计文档 §12）=====

CLAIM_PREFIX = "dr:claim:task:"
JOBS_STREAM = "dr:jobs"
JOBS_GROUP = "dr:workers"
JOB_DEDUPE_PREFIX = "dr:jobdedupe:"
EVENTS_PREFIX = "dr:events:"
EVENT_SEQ_PREFIX = "dr:evseq:"
RUNID_PREFIX = "dr:runid:"


def claim_key(thread_id: str) -> str:
    return f"{CLAIM_PREFIX}{thread_id}"


def events_key(thread_id: str) -> str:
    return f"{EVENTS_PREFIX}{thread_id}"


def event_seq_key(thread_id: str) -> str:
    return f"{EVENT_SEQ_PREFIX}{thread_id}"


def job_dedupe_key(thread_id: str) -> str:
    return f"{JOB_DEDUPE_PREFIX}{thread_id}"


def runid_key(thread_id: str) -> str:
    return f"{RUNID_PREFIX}{thread_id}"


async def resolve_run_id(redis: Any, thread_id: str) -> str:
    """解析任务的 run_id（baseline 观测用）。

    ``POST /start`` 携带 run_id 时由 API 写入 ``dr:runid:{thread_id}``；
    未提供或读取失败时回退 ``auto-<thread_id>`` —— 观测系统不因此失败，
    但会在 artifacts 中体现（data_quality）。
    """
    try:
        value = await redis.get(runid_key(thread_id))
        if value:
            return str(value)
    except Exception as exc:  # noqa: BLE001
        logger.debug("run_id 读取失败（回退 auto）: %s", exc)
    return f"auto-{thread_id}"


# ===== 配置解析 =====


def resolve_redis_config() -> dict[str, Any]:
    """读取当前 stage 的 redis 配置块。未启用时抛 :class:`RedisUnavailable`。"""
    try:
        cfg = load_config(stage_name=get_engine_settings().stage)
        redis_cfg = cfg.get("redis") or {}
    except Exception as exc:
        raise RedisUnavailable(f"无法读取 redis 配置: {exc}") from exc

    if not redis_cfg.get("enabled"):
        raise RedisUnavailable(
            "runtime 需要 Redis，但 config.yml 中 stages.<stage>.redis.enabled 为 false。"
        )
    if not redis_cfg.get("url"):
        raise RedisUnavailable("runtime 需要 Redis，但未配置 redis.url")
    return redis_cfg


def redis_url() -> str:
    return str(resolve_redis_config()["url"])


def redis_db() -> int:
    """`redis.db` 单独配置，URL 里通常不带。

    直接交给 redis-py 的 `db=` 参数，避免手工拼 URL 出错。
    """
    return int(resolve_redis_config().get("db", 0) or 0)


# ===== 连接 =====

_client: Any = None


def get_redis() -> Any:
    """获取进程内缓存的异步 Redis 客户端。"""
    global _client
    if _client is None:
        import redis.asyncio as aioredis

        _client = aioredis.from_url(
            redis_url(),
            db=redis_db(),
            decode_responses=True,
            socket_connect_timeout=float(os.environ.get("REDIS_CONNECT_TIMEOUT", "2")),
        )
        logger.info("runtime Redis 客户端已建立")
    return _client


async def ping() -> bool:
    """探活。供 readiness 使用。"""
    try:
        return bool(await get_redis().ping())
    except Exception as exc:
        logger.warning("Redis ping 失败: %s", exc)
        return False


async def reset_redis() -> None:
    """关闭并丢弃缓存客户端（worker 退出 / 测试隔离）。"""
    global _client
    client, _client = _client, None
    if client is not None:
        try:
            await client.aclose()
        except Exception as exc:
            logger.warning("关闭 runtime Redis 连接失败: %s", exc)


def discard_redis() -> None:
    """仅丢弃引用（同步版，供测试 fixture 使用）。"""
    global _client
    _client = None
