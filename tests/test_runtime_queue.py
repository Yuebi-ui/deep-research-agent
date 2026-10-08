"""G2 —— Job Transport（执行包 §9）。

隔离策略：所有队列测试跑在 **Redis db 15**，并在用例前后 FLUSHDB。
这样绝不会碰到 db 0 里的真实数据（用户的 checkpoint、真实 dr:jobs 等）。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.runtime import queue as q

TEST_DB = 15


def _redis_available() -> bool:
    try:
        import redis

        client = redis.Redis.from_url("redis://localhost:6379", socket_connect_timeout=1.5)
        client.ping()
        client.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _redis_available(), reason="Redis 不可达")


@pytest.fixture
async def rq():
    """db 15 上的隔离客户端。"""
    import redis.asyncio as aioredis

    client = aioredis.from_url("redis://localhost:6379", db=TEST_DB, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


def _tid() -> str:
    return f"jobtest-{uuid.uuid4().hex[:8]}"


# ===== 基本流转 =====


async def test_enqueue_then_consume_then_ack(rq):
    await q.ensure_group(rq)
    tid = _tid()

    message_id = await q.enqueue(rq, tid)
    assert message_id

    jobs = await q.consume(rq, "w1", block_ms=200)
    assert len(jobs) == 1
    assert jobs[0].thread_id == tid
    assert jobs[0].operation == q.OPERATION_RUN
    assert jobs[0].attempt == 0

    await q.ack(rq, jobs[0])
    assert await q.pending_count(rq) == 0


async def test_consume_returns_empty_when_no_job(rq):
    await q.ensure_group(rq)
    assert await q.consume(rq, "w1", block_ms=100) == []


async def test_job_is_pending_until_acked(rq):
    """未 ack 的 job 进入 pending —— 这是崩溃恢复的基础。"""
    await q.ensure_group(rq)
    await q.enqueue(rq, _tid())

    jobs = await q.consume(rq, "w1", block_ms=200)
    assert len(jobs) == 1
    assert await q.pending_count(rq) == 1, "未 ack 应计入 pending"


# ===== Dedupe =====


async def test_dedupe_suppresses_immediate_duplicate(rq):
    """瞬时重复投递（双击 / HTTP retry）应被抑制。"""
    await q.ensure_group(rq)
    tid = _tid()

    first = await q.enqueue(rq, tid, dedupe_ttl_ms=5000)
    second = await q.enqueue(rq, tid, dedupe_ttl_ms=5000)

    assert first is not None
    assert second is None, "同一任务的瞬时重复投递应被 dedupe 抑制"


async def test_dedupe_expires(rq):
    """dedupe 是瞬时的，过期后应可再次投递。"""
    await q.ensure_group(rq)
    tid = _tid()

    assert await q.enqueue(rq, tid, dedupe_ttl_ms=200) is not None
    await asyncio.sleep(0.4)
    assert await q.enqueue(rq, tid, dedupe_ttl_ms=200) is not None, "dedupe 过期后应放行"


async def test_dedupe_is_per_task(rq):
    """不同任务之间不应互相抑制。"""
    await q.ensure_group(rq)
    assert await q.enqueue(rq, _tid(), dedupe_ttl_ms=5000) is not None
    assert await q.enqueue(rq, _tid(), dedupe_ttl_ms=5000) is not None


# ===== 恢复 =====


async def test_reclaim_stale_job_from_dead_worker(rq):
    """worker 崩溃后，未 ack 的 job 应可被新 worker 接管（§9 recovery）。"""
    await q.ensure_group(rq)
    tid = _tid()
    await q.enqueue(rq, tid)

    # worker-1 取走后"崩溃"（不 ack）
    taken = await q.consume(rq, "worker-1", block_ms=200)
    assert len(taken) == 1

    # 新 worker 接管空闲超过 100ms 的 pending 条目
    reclaimed = []
    for _ in range(20):
        reclaimed = await q.reclaim_stale(rq, "worker-2", min_idle_ms=100)
        if reclaimed:
            break
        await asyncio.sleep(0.1)

    assert reclaimed, "应从崩溃的 worker 手中接管 job"
    assert reclaimed[0].thread_id == tid
    await q.ack(rq, reclaimed[0])
    assert await q.pending_count(rq) == 0


async def test_reclaimed_job_is_not_delivered_twice(rq):
    """接管后原 worker 不应再收到同一 job。"""
    await q.ensure_group(rq)
    await q.enqueue(rq, _tid())
    await q.consume(rq, "worker-1", block_ms=200)

    reclaimed = []
    for _ in range(20):
        reclaimed = await q.reclaim_stale(rq, "worker-2", min_idle_ms=100)
        if reclaimed:
            break
        await asyncio.sleep(0.1)
    assert reclaimed

    # 原 worker 再读新消息应为空（该条目已归属 worker-2）
    assert await q.consume(rq, "worker-1", block_ms=100) == []


# ===== 失败语义 =====


async def test_enqueue_raises_when_redis_unavailable():
    """Redis 不可达时入队必须明确失败（API 据此返回 503），不得静默丢任务。"""

    class DeadRedis:
        async def set(self, *a, **kw):
            raise ConnectionError("redis down")

    with pytest.raises(q.EnqueueRejected):
        await q.enqueue(DeadRedis(), _tid())


async def test_enqueue_failure_does_not_consume_dedupe_slot():
    """入队失败不应留下 dedupe 标记，否则用户重试会被误抑制。"""
    calls: list[str] = []

    class FlakyRedis:
        async def set(self, *a, **kw):
            calls.append("set")
            return True  # dedupe 放行

        async def xadd(self, *a, **kw):
            raise ConnectionError("redis down")

    with pytest.raises(q.EnqueueRejected):
        await q.enqueue(FlakyRedis(), _tid())

    assert calls == ["set"], "前置条件：dedupe 已放行"


async def test_consume_is_resilient_to_redis_errors():
    """消费侧出错不应抛穿 —— 返回空列表让 worker 退避重试。"""

    class DeadRedis:
        async def xreadgroup(self, *a, **kw):
            raise ConnectionError("redis down")

    assert await q.consume(DeadRedis(), "w1", block_ms=10) == []


# ===== 多 worker 竞争 =====


async def test_only_one_worker_receives_each_job(rq):
    """consumer group 语义：同一 job 只投递给一个 consumer。"""
    await q.ensure_group(rq)
    tid = _tid()
    await q.enqueue(rq, tid)

    results = await asyncio.gather(
        q.consume(rq, "w1", block_ms=300),
        q.consume(rq, "w2", block_ms=300),
        q.consume(rq, "w3", block_ms=300),
    )

    delivered = [job for batch in results for job in batch]
    assert len(delivered) == 1, f"job 应只投递给一个 consumer，实际 {len(delivered)}"
    assert delivered[0].thread_id == tid
