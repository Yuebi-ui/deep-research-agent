"""G1 —— 运行时原语：身份、Atomic Claim、心跳（执行包 §24）。

`test_only_one_worker_can_claim_task` 是**真并发**测试：多个独立 Redis 连接
同时对同一 key 发 `SET NX`，断言恰好一个成功。串行模拟不算证明（§24 明令）。
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from backend.core.settings import get_settings
from backend.runtime import claim as claim_mod
from backend.runtime import heartbeat as hb_mod
from backend.runtime import redis as rt_redis
from backend.runtime.identity import new_worker_id


def _redis_available() -> bool:
    try:
        import redis

        client = redis.Redis.from_url("redis://localhost:6379", socket_connect_timeout=1.5)
        client.ping()
        client.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_available(), reason="Redis 不可达（runtime 依赖 Redis Stack）"
)


@pytest.fixture
async def rt(request):
    """一个独立的 runtime Redis 客户端，并在用例后清理本用例写入的 key。"""
    import redis.asyncio as aioredis

    client = aioredis.from_url("redis://localhost:6379", db=0, decode_responses=True)
    thread_id = f"claimtest-{uuid.uuid4().hex[:8]}"

    yield client, thread_id

    # 只删本用例的 key，避免影响真实 Redis 中的其他数据
    await client.delete(rt_redis.claim_key(thread_id))
    await client.aclose()


# ===== 身份 =====


def test_worker_id_contains_hostname_pid_and_random():
    """不能只用 PID —— 容器与 PID 复用会撞（§10 明令）。"""
    wid = new_worker_id()
    parts = wid.split(":")

    assert len(parts) == 3, f"格式应为 host:pid:rand，实际 {wid!r}"
    assert parts[1] == str(os.getpid())
    assert len(parts[2]) == 8

    assert new_worker_id() != new_worker_id(), "两次生成必须不同"


# ===== 基本语义 =====


async def test_acquire_succeeds_when_free(rt):
    client, tid = rt
    result = await claim_mod.acquire(client, tid, "w1", ttl_ms=5000)

    assert result.acquired is True
    assert await claim_mod.holder(client, tid) == "w1"


async def test_only_one_worker_can_claim_task(rt):
    """**真并发**：多个独立连接同时抢同一 key，恰好一个成功。

    这是 §24 的核心断言。刻意不用串行调用——串行不能证明原子性。
    """
    client, tid = rt

    # 每个竞争者是独立的 Redis 连接，模拟真实的多 worker
    import redis.asyncio as aioredis

    conns = [
        aioredis.from_url("redis://localhost:6379", db=0, decode_responses=True)
        for _ in range(8)
    ]
    try:
        results = await asyncio.gather(
            *(
                claim_mod.acquire(conns[i], tid, f"worker-{i}", ttl_ms=5000)
                for i in range(len(conns))
            )
        )
    finally:
        for c in conns:
            await c.aclose()

    winners = [r for r in results if r.acquired]
    assert len(winners) == 1, f"应恰好一个 worker 抢到，实际 {len(winners)}"
    assert await claim_mod.holder(client, tid) == winners[0].worker_id


async def test_second_acquire_reports_holder(rt):
    client, tid = rt
    await claim_mod.acquire(client, tid, "w1", ttl_ms=5000)

    second = await claim_mod.acquire(client, tid, "w2", ttl_ms=5000)
    assert second.acquired is False
    assert second.holder == "w1"
    assert second.held_by_other is True


# ===== 续约 =====


async def test_claim_owner_can_renew(rt):
    client, tid = rt
    await claim_mod.acquire(client, tid, "w1", ttl_ms=3000)

    await asyncio.sleep(1.2)
    assert await claim_mod.renew(client, tid, "w1", ttl_ms=5000) is True
    assert await claim_mod.holder(client, tid) == "w1"


async def test_non_owner_cannot_renew(rt):
    client, tid = rt
    await claim_mod.acquire(client, tid, "w1", ttl_ms=5000)

    assert await claim_mod.renew(client, tid, "w2", ttl_ms=5000) is False
    assert await claim_mod.holder(client, tid) == "w1", "非 owner 不得改变持有者"


# ===== 释放 =====


async def test_non_owner_cannot_release_claim(rt):
    client, tid = rt
    await claim_mod.acquire(client, tid, "w1", ttl_ms=5000)

    assert await claim_mod.release(client, tid, "w2") is False
    assert await claim_mod.holder(client, tid) == "w1"


async def test_owner_can_release(rt):
    client, tid = rt
    await claim_mod.acquire(client, tid, "w1", ttl_ms=5000)

    assert await claim_mod.release(client, tid, "w1") is True
    assert await claim_mod.holder(client, tid) is None


# ===== 过期与接管 =====


async def test_claim_expires_when_worker_dies(rt):
    """worker 崩溃 → 心跳停止 → claim 自然过期。"""
    client, tid = rt
    await claim_mod.acquire(client, tid, "dead-worker", ttl_ms=800)

    await asyncio.sleep(1.2)
    assert await claim_mod.holder(client, tid) is None, "claim 应已过期"


async def test_stale_task_can_be_reclaimed(rt):
    """claim 过期后，另一个 worker 应能接管。"""
    client, tid = rt
    await claim_mod.acquire(client, tid, "dead-worker", ttl_ms=800)
    await asyncio.sleep(1.2)

    result = await claim_mod.acquire(client, tid, "new-worker", ttl_ms=5000)
    assert result.acquired is True
    assert await claim_mod.holder(client, tid) == "new-worker"


# ===== 心跳 =====


async def test_heartbeat_keeps_claim_alive(rt):
    """心跳持续续约，claim 不应过期。"""
    client, tid = rt
    await claim_mod.acquire(client, tid, "w1", ttl_ms=900)

    hb = hb_mod.Heartbeat(client, tid, "w1", interval_ms=300, ttl_ms=900)
    await hb.start()
    try:
        await asyncio.sleep(1.5)  # 远超过原始 TTL
        assert hb.lost is False
        assert await claim_mod.holder(client, tid) == "w1", "心跳应保持 claim 存活"
    finally:
        await hb.stop()


async def test_heartbeat_detects_lost_ownership(rt):
    """claim 被抢走后，心跳必须察觉并停止 —— 否则会双跑。"""
    client, tid = rt
    await claim_mod.acquire(client, tid, "w1", ttl_ms=600)

    hb = hb_mod.Heartbeat(client, tid, "w1", interval_ms=200, ttl_ms=600)
    await hb.start()
    try:
        # 模拟 claim 过期后被他人接管（直接覆盖 key）
        await asyncio.sleep(0.1)
        await client.set(rt_redis.claim_key(tid), "other-worker", px=60_000)

        for _ in range(30):
            if hb.lost:
                break
            await asyncio.sleep(0.1)

        assert hb.lost is True, "心跳未察觉所有权丢失"
    finally:
        await hb.stop()


# ===== 配置约束 =====


def test_claim_ttl_is_at_least_three_heartbeat_intervals():
    """§7 明令：claim TTL >= 3 × heartbeat interval。"""
    s = get_settings()

    assert s.claim_ttl_seconds >= 3 * s.heartbeat_interval_seconds, (
        f"claim TTL ({s.claim_ttl_seconds}s) 必须 >= 3 × "
        f"心跳间隔 ({s.heartbeat_interval_seconds}s)"
    )


def test_runtime_settings_have_ms_helpers():
    s = get_settings()
    assert s.claim_ttl_ms == int(s.claim_ttl_seconds * 1000)
    assert s.heartbeat_interval_ms == int(s.heartbeat_interval_seconds * 1000)
