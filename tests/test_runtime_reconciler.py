"""Orphan Reconciler（执行计划 §6.4）。

覆盖：
    RUNNING + no claim + no job  → 重新入队
    WAITING_REVIEW               → 不重新入队（等待人工是正常状态）
    终态                          → 忽略
    有有效 claim                  → 忽略
    已入队尚未消费                → 忽略
    两个 reconciler 并发          → 不产生重复执行
    checkpoint 不可用             → 明确状态，不假装恢复成功

隔离：DB 为临时 SQLite；Redis 用 db 15 且前后 FLUSHDB。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.core.settings import get_settings
from backend.db import engine as db_engine
from backend.db import get_repository
from backend.db.models import Base
from backend.domain.task import TaskStatus
from backend.runtime import claim as claim_mod
from backend.runtime import queue as q
from backend.runtime.reconciler import OrphanReconciler

TEST_DB = 15


def _redis_available() -> bool:
    try:
        import redis

        c = redis.Redis.from_url("redis://localhost:6379", socket_connect_timeout=1.5)
        c.ping()
        c.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _redis_available(), reason="Redis 不可达")


@pytest.fixture
async def env(tmp_path, monkeypatch):
    import redis.asyncio as aioredis

    db_path = tmp_path / "tasks.db"
    monkeypatch.setattr(db_engine, "resolve_database_url", lambda: f"sqlite+pysqlite:///{db_path}")
    db_engine.reset_engine()
    Base.metadata.create_all(db_engine.get_engine())

    client = aioredis.from_url("redis://localhost:6379", db=TEST_DB, decode_responses=True)
    await client.flushdb()
    await q.ensure_group(client)

    settings = get_settings()
    # 宽限期设 0：测试里不需要等待
    monkeypatch.setattr(settings, "orphan_grace_seconds", 0.0, raising=False)

    yield {"redis": client, "repo": get_repository(), "settings": settings}

    await client.flushdb()
    await client.aclose()
    db_engine.reset_engine()


def _make_task(env, status: TaskStatus, *, query: str = "q") -> str:
    tid = uuid.uuid4().hex[:12]
    env["repo"].create(tid, query)
    if status is not TaskStatus.PENDING:
        task = env["repo"].get(tid)
        task.status = status.value
        env["repo"].save(task)
    return tid


def _reconciler(env, worker_id: str = "rec:1:aaaa0000") -> OrphanReconciler:
    return OrphanReconciler(
        redis=env["redis"], repository=env["repo"],
        settings=env["settings"], worker_id=worker_id,
    )


async def _job_thread_ids(env) -> set[str]:
    """队列里当前有哪些任务的 job。"""
    return await q.outstanding_thread_ids(env["redis"])


# ===== 基本回收 =====


async def test_running_without_claim_or_job_is_requeued(env):
    """核心场景：RUNNING 但没人管 → 重新入队。"""
    tid = _make_task(env, TaskStatus.RUNNING)

    requeued = await _reconciler(env).sweep()

    assert tid in requeued
    assert tid in await _job_thread_ids(env), "应已入队"


async def test_pending_without_job_is_requeued(env):
    """API 入队失败（503）后留下的 PENDING 任务也应被回收。"""
    tid = _make_task(env, TaskStatus.PENDING)

    requeued = await _reconciler(env).sweep()

    assert tid in requeued


# ===== 不该动的 =====


@pytest.mark.parametrize(
    "status",
    [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.DELETED],
)
async def test_terminal_states_are_never_requeued(env, status):
    """§6.2：终态永不重新调度。"""
    tid = _make_task(env, status)

    requeued = await _reconciler(env).sweep()

    assert tid not in requeued
    assert tid not in await _job_thread_ids(env)


async def test_waiting_review_is_not_requeued(env):
    """§6.4：等待人工审核是**正常状态**，不是孤儿。

    重新投递普通 run job 没有意义——worker 会再次发现停在 human_review。
    """
    tid = _make_task(env, TaskStatus.WAITING_REVIEW)

    requeued = await _reconciler(env).sweep()

    assert tid not in requeued, "WAITING_REVIEW 不应被自动重新调度"


async def test_task_with_valid_claim_is_ignored(env):
    """有 worker 持 claim 在身 → 不是孤儿。"""
    tid = _make_task(env, TaskStatus.RUNNING)
    await claim_mod.acquire(env["redis"], tid, "live-worker", 60_000)

    requeued = await _reconciler(env).sweep()

    assert tid not in requeued


async def test_task_already_queued_is_ignored(env):
    """队列里还有它的 job → 等着被消费即可，不要重复投递。"""
    tid = _make_task(env, TaskStatus.RUNNING)
    await q.enqueue(env["redis"], tid, dedupe_ttl_ms=0)

    requeued = await _reconciler(env).sweep()

    assert tid not in requeued


async def test_task_consumed_but_unacked_is_ignored(env):
    """job 已被某 worker 取走但尚未 ack —— 仍在处理中，不是孤儿。"""
    tid = _make_task(env, TaskStatus.RUNNING)
    await q.enqueue(env["redis"], tid, dedupe_ttl_ms=0)
    jobs = await q.consume(env["redis"], "w1", block_ms=300)
    assert jobs

    requeued = await _reconciler(env).sweep()

    assert tid not in requeued, "pending（未 ack）的 job 说明还在处理中"


# ===== 宽限期 =====


async def test_grace_period_defers_fresh_tasks(env, monkeypatch):
    """刚创建、job 尚未被消费的任务不应被判定为孤儿。"""
    monkeypatch.setattr(env["settings"], "orphan_grace_seconds", 300.0, raising=False)
    tid = _make_task(env, TaskStatus.PENDING)

    requeued = await _reconciler(env).sweep()

    assert tid not in requeued, "宽限期内不应重新入队"


# ===== 幂等 =====


async def test_sweep_is_idempotent(env):
    """重复扫描不应产生重复 job（dedupe + 队列检查共同保证）。"""
    tid = _make_task(env, TaskStatus.RUNNING)

    rec = _reconciler(env)
    first = await rec.sweep()
    second = await rec.sweep()

    assert tid in first
    assert tid not in second, "第二轮应看到已有 job 而跳过"


async def test_two_reconcilers_do_not_duplicate(env):
    """§6.4：两个 reconciler 并发也不得产生重复执行。

    它们可能各自入队一次（dedupe 窗口内会被抑制），但真正的安全边界是
    Atomic Claim —— 即使有两条 job，也只有一个 worker 能执行。
    """
    tid = _make_task(env, TaskStatus.RUNNING)

    results = await asyncio.gather(
        _reconciler(env, "rec-A").sweep(),
        _reconciler(env, "rec-B").sweep(),
    )

    total = sum(1 for r in results if tid in r)
    assert total <= 1, f"两个 reconciler 不应同时入队（实际 {total} 次）"

    # 无论如何，claim 保证只有一个 worker 能执行
    acquired = await claim_mod.acquire(env["redis"], tid, "w1", 60_000)
    assert acquired.acquired is True
    assert (await claim_mod.acquire(env["redis"], tid, "w2", 60_000)).acquired is False


async def test_reconciler_does_not_create_duplicate_when_claim_taken(env):
    """已被 claim 的任务，即使两个 reconciler 并发也不会入队。"""
    tid = _make_task(env, TaskStatus.RUNNING)
    await claim_mod.acquire(env["redis"], tid, "live-worker", 60_000)

    results = await asyncio.gather(
        _reconciler(env, "rec-A").sweep(),
        _reconciler(env, "rec-B").sweep(),
    )

    assert all(tid not in r for r in results)


# ===== 失败语义 =====


async def test_unreadable_queue_state_makes_sweep_a_noop(env):
    """无法读取队列状态时**不做任何事** —— 宁可漏收，不可误投。

    否则「读取失败」会被误判成「队列里没有 job」，从而把正在执行的任务
    重新投递一遍。
    """

    class DeadRedis:
        async def xpending_range(self, *a, **kw):
            raise ConnectionError("redis down")

        async def xinfo_groups(self, *a, **kw):
            raise ConnectionError("redis down")

    tid = _make_task(env, TaskStatus.RUNNING)
    rec = OrphanReconciler(
        redis=DeadRedis(), repository=env["repo"],
        settings=env["settings"], worker_id="rec:dead",
    )

    requeued = await rec.sweep()

    assert requeued == [], "读取失败时不应入队任何任务"
    assert tid not in await _job_thread_ids(env)


async def test_unknown_status_is_left_alone(env):
    """状态不可识别的任务不擅自动它。"""
    tid = _make_task(env, TaskStatus.RUNNING)
    task = env["repo"].get(tid)
    task.status = "totally-bogus-status"
    env["repo"].save(task)

    requeued = await _reconciler(env).sweep()

    assert tid not in requeued


async def test_deleted_task_is_never_requeued(env):
    """软删除的任务绝不能被回收执行。"""
    tid = _make_task(env, TaskStatus.RUNNING)
    task = env["repo"].get(tid)
    task.status = TaskStatus.DELETED.value
    env["repo"].save(task)

    requeued = await _reconciler(env).sweep()

    assert tid not in requeued
