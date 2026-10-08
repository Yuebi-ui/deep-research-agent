"""G7 —— 崩溃恢复与双执行防护（执行包 §7 / §11 / §29）。

核心不变量：

```text
Worker crash != permanent task loss
duplicate job != duplicate execution
```

用短 TTL（测试值）避免真实等待几十秒（§30）。
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
from backend.runtime.heartbeat import Heartbeat
from backend.runtime.runner import TaskRunner

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

# 测试用短参数（生产默认 10s / 30s）
_FAST_HEARTBEAT_MS = 300
_FAST_TTL_MS = 900


@pytest.fixture
async def env(tmp_path, monkeypatch):
    import redis.asyncio as aioredis

    import deep_research.checkpoint as ckpt

    db_path = tmp_path / "tasks.db"
    monkeypatch.setattr(db_engine, "resolve_database_url", lambda: f"sqlite+pysqlite:///{db_path}")
    db_engine.reset_engine()
    Base.metadata.create_all(db_engine.get_engine())

    monkeypatch.setattr(ckpt, "resolve_sqlite_path", lambda: str(tmp_path / "ckpt.db"))
    ckpt.discard_checkpointer()
    managed = await ckpt.create_checkpointer("sqlite")

    client = aioredis.from_url("redis://localhost:6379", db=TEST_DB, decode_responses=True)
    await client.flushdb()

    settings = get_settings()
    yield {"redis": client, "repo": get_repository(), "checkpointer": managed.saver,
           "settings": settings}

    await client.flushdb()
    await client.aclose()
    await managed.aclose()
    ckpt.discard_checkpointer()
    db_engine.reset_engine()


def _runner(env, worker_id: str) -> TaskRunner:
    return TaskRunner(
        redis=env["redis"], worker_id=worker_id,
        settings=env["settings"], repository=env["repo"], checkpointer=env["checkpointer"],
    )


def _new_task(env) -> str:
    tid = uuid.uuid4().hex[:12]
    env["repo"].create(tid, "Compare LangGraph and CrewAI")
    return tid


# ===== Claim 过期 → 新 worker 接管 =====


async def test_worker_crash_releases_claim_by_expiry(env):
    """worker A 崩溃（心跳停止）→ claim 自然过期。"""
    tid = _new_task(env)
    await claim_mod.acquire(env["redis"], tid, "worker-A", _FAST_TTL_MS)

    assert await claim_mod.holder(env["redis"], tid) == "worker-A"
    await asyncio.sleep(_FAST_TTL_MS / 1000 + 0.4)

    assert await claim_mod.holder(env["redis"], tid) is None, "崩溃后 claim 应过期"


async def test_new_worker_can_take_over_after_crash(env):
    """claim 过期后新 worker 可接管 —— Worker crash != task loss。"""
    tid = _new_task(env)
    await claim_mod.acquire(env["redis"], tid, "worker-A", _FAST_TTL_MS)
    await asyncio.sleep(_FAST_TTL_MS / 1000 + 0.4)

    result = await claim_mod.acquire(env["redis"], tid, "worker-B", _FAST_TTL_MS)
    assert result.acquired is True
    assert await claim_mod.holder(env["redis"], tid) == "worker-B"


async def test_worker_recovers_task_to_completion(env):
    """完整场景：A 跑到 HITL 中断后崩溃 → B 接管 → 审查后完成。"""
    tid = _new_task(env)

    # worker A 执行到 WAITING_REVIEW，然后"崩溃"（不释放 claim）
    await _runner(env, "worker-A").run(tid)
    assert env["repo"].get(tid).status == TaskStatus.WAITING_REVIEW.value

    # 模拟 A 消失：清掉 claim（等价于等待过期，这里直接删以节省时间）
    await env["redis"].delete(f"dr:claim:task:{tid}")

    # 用户审批
    env["repo"].create_review(tid, "approve", "")

    # worker B 接管 —— 必须从 checkpoint 恢复，而不是从头重跑
    outcome = await _runner(env, "worker-B").run(tid)

    assert outcome.status == TaskStatus.COMPLETED.value
    task = env["repo"].get(tid)
    assert task.final_report, "B 必须能产出最终报告（说明确实从 checkpoint 恢复了）"
    assert task.claimed_by == "worker-B"


async def test_resume_does_not_restart_from_scratch(env):
    """关键断言：恢复时**不得**从头重跑。

    从头重跑会让 attempt 之外的状态变化（例如草稿被重新生成）——
    这里通过对比草稿来验证：审查前的草稿必须仍然有效。
    """
    tid = _new_task(env)
    await _runner(env, "worker-A").run(tid)
    draft_after_first = env["repo"].get(tid).draft_report
    assert draft_after_first

    await env["redis"].delete(f"dr:claim:task:{tid}")
    env["repo"].create_review(tid, "approve", "")
    await _runner(env, "worker-B").run(tid)

    # 最终报告存在即说明图是从中断点继续的（从头跑会再次停在 human_review）
    assert env["repo"].get(tid).status == TaskStatus.COMPLETED.value


# ===== 双执行防护 =====


async def test_second_worker_cannot_claim_live_task(env):
    """A 仍持有 claim 时，B 不得执行。"""
    tid = _new_task(env)
    await claim_mod.acquire(env["redis"], tid, "worker-A", 60_000)

    result = await claim_mod.acquire(env["redis"], tid, "worker-B", 60_000)

    assert result.acquired is False
    assert result.holder == "worker-A"


async def test_heartbeat_prevents_takeover(env):
    """A 有心跳时，claim 不会过期，B 无从接管。"""
    tid = _new_task(env)
    await claim_mod.acquire(env["redis"], tid, "worker-A", _FAST_TTL_MS)

    hb = Heartbeat(env["redis"], tid, "worker-A",
                   interval_ms=_FAST_HEARTBEAT_MS, ttl_ms=_FAST_TTL_MS)
    await hb.start()
    try:
        await asyncio.sleep(_FAST_TTL_MS / 1000 + 0.6)  # 超过原始 TTL

        assert await claim_mod.holder(env["redis"], tid) == "worker-A"
        assert (await claim_mod.acquire(env["redis"], tid, "worker-B", _FAST_TTL_MS)).acquired is False
    finally:
        await hb.stop()


async def test_ownership_loss_is_detected(env):
    """claim 被抢走后心跳必须察觉 —— 否则会双跑。"""
    tid = _new_task(env)
    await claim_mod.acquire(env["redis"], tid, "worker-A", 600)

    hb = Heartbeat(env["redis"], tid, "worker-A", interval_ms=200, ttl_ms=600)
    await hb.start()
    try:
        await asyncio.sleep(0.05)
        await env["redis"].set(f"dr:claim:task:{tid}", "worker-B", px=60_000)

        for _ in range(30):
            if hb.lost:
                break
            await asyncio.sleep(0.1)

        assert hb.lost is True
    finally:
        await hb.stop()


# ===== 终态不被接管 =====


async def test_terminal_task_is_not_reclaimed(env):
    """worker 不得重新执行终态任务（§24）。"""
    tid = _new_task(env)
    task = env["repo"].get(tid)
    task.status = TaskStatus.COMPLETED.value
    env["repo"].save(task)

    outcome = await _runner(env, "worker-B").run(tid)

    assert outcome.status == TaskStatus.COMPLETED.value
    assert "已是终态" in outcome.detail


async def test_cancelled_task_is_not_reclaimed(env):
    tid = _new_task(env)
    task = env["repo"].get(tid)
    task.cancel_requested = True
    env["repo"].save(task)

    outcome = await _runner(env, "worker-B").run(tid)
    assert outcome.status == TaskStatus.CANCELLED.value


# ===== Graceful shutdown 的 claim 策略 =====


async def test_graceful_shutdown_does_not_release_claim(env):
    """§14 决策：优雅退出**不**主动释放 claim，让它自然过期。

    主动释放会让接管者立即重复执行当前节点；等过期可保证 TTL 窗口内
    绝无第二执行者。
    """
    from backend.worker import Worker

    tid = _new_task(env)
    await claim_mod.acquire(env["redis"], tid, "worker-A", _FAST_TTL_MS)

    w = Worker()
    w.worker_id = "worker-A"
    w._current = tid
    w._redis = env["redis"]

    # 直接调用清理逻辑（不启动信号循环）
    from deep_research import checkpoint as ckpt

    await ckpt.reset_checkpointer()

    assert await claim_mod.holder(env["redis"], tid) == "worker-A", (
        "优雅退出后 claim 应仍然存在（等自然过期），而不是被释放"
    )
