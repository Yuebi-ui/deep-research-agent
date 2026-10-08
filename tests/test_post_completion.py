"""Phase 4C-lite：post-completion 后台任务生命周期 + runner 接线。

覆盖的不变量：

1. **顺序**：报告落库 / 任务置 completed / task.completed 发布 **之后**，才做
   结构化记忆落库（user-visible completion 不等待它）；
2. **不阻塞**：有生命周期管理器时，`_finish_completed` 不等落库跑完就返回；
3. **失败隔离**：落库失败只降级记忆，任务状态与 outcome 不变；
4. **空 context**：后台任务不继承 run 的 collector（否则 run artifacts 会因
   落库时序不同而不可复现）；
5. **有界收尾**：`drain()` 超时后显式放弃并计数；
6. **兼容路径**：没有生命周期管理器时内联执行（绝不 fire-and-forget）。
"""

from __future__ import annotations

import asyncio
import threading
import uuid

import pytest

from backend.runtime.post_completion import PostCompletionTasks
from deep_research.callbacks.baseline_metrics import (
    BaselineMetricsCollector,
    RunContext,
    get_baseline_collector,
    set_baseline_collector,
)

# ===== 纯 tracker 语义（不需要 Redis）=====


async def test_schedule_runs_task_and_clears_pending():
    tracker = PostCompletionTasks()
    seen: list[str] = []

    async def work():
        seen.append("ran")
        return 42

    task = tracker.schedule(work, name="t1")
    assert task is not None
    assert tracker.pending() == 1
    await tracker.wait_idle()
    assert seen == ["ran"]
    assert tracker.pending() == 0
    assert task.result() == 42


async def test_failure_is_isolated_and_reported_to_on_done():
    tracker = PostCompletionTasks()
    captured: dict = {}

    async def boom():
        raise RuntimeError("extraction failed")

    tracker.schedule(
        boom, name="t-boom",
        on_done=lambda result, error, elapsed: captured.update(result=result, error=error, elapsed=elapsed),
    )
    await tracker.wait_idle()  # 不得抛出
    assert isinstance(captured["error"], RuntimeError)
    assert captured["result"] is None
    assert captured["elapsed"] >= 0


async def test_task_runs_in_empty_context_not_inheriting_collector():
    """后台任务不得继承 run 的 baseline collector（run artifacts 必须可复现）。"""
    tracker = PostCompletionTasks()
    seen: dict = {}
    parent = BaselineMetricsCollector(RunContext(run_id="r1", task_id="t1", attempt=1, worker_id="w"))
    set_baseline_collector(parent)
    try:
        assert get_baseline_collector() is parent  # 父 context 确实有 collector

        async def check():
            seen["collector"] = get_baseline_collector()

        tracker.schedule(check, name="ctx-check")
        await tracker.wait_idle()
    finally:
        set_baseline_collector(None)

    assert seen["collector"] is None


async def test_wait_idle_waits_for_completion():
    tracker = PostCompletionTasks()
    release = threading.Event()
    done: list[bool] = []

    async def slow():
        await asyncio.to_thread(release.wait, 5)
        done.append(True)

    tracker.schedule(slow, name="slow")
    await asyncio.sleep(0.01)
    assert done == []          # 还在跑
    release.set()
    await tracker.wait_idle()
    assert done == [True]


async def test_drain_times_out_and_reports_abandoned():
    tracker = PostCompletionTasks()
    block = threading.Event()

    async def stuck():
        await asyncio.to_thread(block.wait, 5)

    tracker.schedule(stuck, name="stuck")
    abandoned = await tracker.drain(timeout=0.05)
    block.set()
    assert abandoned == 1
    assert tracker.pending() <= 1  # 已被 cancel


def test_schedule_without_running_loop_returns_none():
    """没有事件循环时不得抛异常（只记 warning）。必须是非 async 测试：async
    测试本身跑在事件循环里，测不到这条路径。"""
    tracker = PostCompletionTasks()

    async def work():  # pragma: no cover - 不会被调度
        return 1

    assert tracker.schedule(work, name="no-loop") is None


async def test_wait_idle_and_drain_are_noops_when_empty():
    tracker = PostCompletionTasks()
    assert await tracker.wait_idle() == 0
    assert await tracker.drain(timeout=1.0) == 0


# ===== runner 接线（真 Redis + 临时 DB + 真图以外的最小路径）=====

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


pytestmark_redis = pytest.mark.skipif(not _redis_available(), reason="Redis 不可达")


@pytest.fixture
async def env(tmp_path, monkeypatch):
    import redis.asyncio as aioredis

    import deep_research.checkpoint as ckpt
    from backend.core.settings import get_settings
    from backend.db import engine as db_engine
    from backend.db import get_repository
    from backend.db.models import Base

    db_path = tmp_path / "tasks.db"
    monkeypatch.setattr(db_engine, "resolve_database_url", lambda: f"sqlite+pysqlite:///{db_path}")
    db_engine.reset_engine()
    Base.metadata.create_all(db_engine.get_engine())

    ckpt_path = tmp_path / "ckpt.db"
    monkeypatch.setattr(ckpt, "resolve_sqlite_path", lambda: str(ckpt_path))
    ckpt.discard_checkpointer()
    managed = await ckpt.create_checkpointer("sqlite")

    # reliability 事件落到 tmp，避免污染仓库 artifacts/
    from deep_research.callbacks import baseline_metrics as bm

    monkeypatch.setattr(bm, "default_raw_root", lambda: tmp_path / "raw")

    client = aioredis.from_url("redis://localhost:6379", db=TEST_DB, decode_responses=True)
    await client.flushdb()

    yield {
        "redis": client,
        "repo": get_repository(),
        "checkpointer": managed.saver,
        "settings": get_settings(),
        "tmp": tmp_path,
    }

    await client.flushdb()
    await client.aclose()
    await managed.aclose()
    ckpt.discard_checkpointer()
    db_engine.reset_engine()


def _runner(env, post_completion: PostCompletionTasks | None = None):
    from backend.runtime.runner import TaskRunner

    return TaskRunner(
        redis=env["redis"],
        worker_id="test-worker:1:abcdef01",
        settings=env["settings"],
        repository=env["repo"],
        checkpointer=env["checkpointer"],
        post_completion=post_completion,
    )


def _completed_task(env, report: str = "最终报告正文" * 50) -> str:
    tid = uuid.uuid4().hex[:12]
    env["repo"].create(tid, "Compare LangGraph and CrewAI")
    task = env["repo"].get(tid)
    task.status = "running"
    env["repo"].save(task)
    return tid


@pytest.fixture
def patch_enrichment(monkeypatch):
    """替换 store_report_memory，并记录调用顺序与落库时刻的任务状态。"""
    observed: dict = {"calls": [], "events_before_enrich": []}

    def _install(store_fn):
        import backend.runtime.events as ev_mod
        import deep_research.agent_builder as ab

        original_publish = ev_mod.publish

        async def _publish(redis, thread_id, event_type, payload, **kwargs):
            observed["events_before_enrich"].append(str(event_type))
            return await original_publish(redis, thread_id, event_type, payload, **kwargs)

        monkeypatch.setattr(ev_mod, "publish", _publish)
        monkeypatch.setattr(ab, "store_report_memory", store_fn)
        return observed

    return _install


async def _wait_flag(flag: threading.Event, timeout: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while not flag.is_set() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    return flag.is_set()


@pytestmark_redis
async def test_completion_precedes_enrichment_and_is_not_blocked(env, patch_enrichment):
    """核心不变量：完成事件先发；落库不阻塞返回；落库时报告已落库。"""
    tid = _completed_task(env)
    report = "最终报告正文" * 50
    release = threading.Event()
    started = threading.Event()
    observed: dict = {}

    def store(user_query, report_text):
        task = env["repo"].get(tid)
        observed["status_at_enrich"] = task.status
        observed["report_persisted"] = task.final_report == report_text
        observed["user_query"] = user_query
        started.set()
        release.wait(5)          # 阻塞直到测试放行 —— 用来证明"没有等待它"
        observed["finished"] = True
        return "doc-123"

    tracker = PostCompletionTasks()
    events = patch_enrichment(store)
    runner = _runner(env, tracker)

    outcome = await asyncio.wait_for(
        runner._finish_completed(tid, report, None, user_query="Compare X and Y"), timeout=2
    )

    assert outcome.status == "completed"
    assert env["repo"].get(tid).status == "completed"
    assert tracker.pending() == 1, "落库应在后台进行，而不是阻塞 _finish_completed"
    assert "finished" not in observed, "_finish_completed 不得等待落库完成"

    # 后台任务真正开始执行后，才检查它的前置条件
    assert await _wait_flag(started), "后台落库任务未启动"
    assert observed["status_at_enrich"] == "completed"
    assert observed["report_persisted"] is True
    assert observed["user_query"] == "Compare X and Y"
    assert "task.completed" in events["events_before_enrich"]

    release.set()
    await tracker.wait_idle()
    assert observed["finished"] is True


@pytestmark_redis
async def test_enrichment_failure_does_not_affect_completion(env, patch_enrichment):
    tid = _completed_task(env)
    report = "最终报告正文" * 50

    def boom(user_query, report_text):
        raise RuntimeError("dashscope 500")

    tracker = PostCompletionTasks()
    patch_enrichment(boom)
    runner = _runner(env, tracker)

    outcome = await runner._finish_completed(tid, report, None, user_query="q")
    await tracker.wait_idle()

    assert outcome.status == "completed"
    assert env["repo"].get(tid).status == "completed"


@pytestmark_redis
async def test_ownership_lost_skips_completion_and_enrichment(env, patch_enrichment):
    """失去 claim 所有权 → 不写终态、**也不做记忆落库**（由新 owner 负责）。"""
    tid = _completed_task(env)
    calls: list = []

    def store(user_query, report_text):
        calls.append(user_query)
        return "doc-x"

    patch_enrichment(store)
    runner = _runner(env, PostCompletionTasks())

    class _LostHB:
        lost = True
        loss_reason = "renewal_failed"

    runner._hb = _LostHB()  # 模拟心跳判定失去所有权

    outcome = await runner._finish_completed(tid, "报告正文" * 60, None, user_query="q")

    assert outcome.retryable is True, "失去所有权必须放弃终态写入"
    assert env["repo"].get(tid).status != "completed"
    assert calls == [], "stale owner 不得写记忆"
    assert runner._post_completion.pending() == 0


@pytestmark_redis
async def test_without_tracker_enrichment_runs_inline(env, patch_enrichment):
    """兼容路径：没有生命周期管理器时内联执行（保持旧语义，绝不 fire-and-forget）。"""
    tid = _completed_task(env)
    report = "最终报告正文" * 50
    observed: dict = {}

    def store(user_query, report_text):
        observed["ran"] = True
        observed["status_at_enrich"] = env["repo"].get(tid).status
        return "doc-1"

    patch_enrichment(store)
    runner = _runner(env, post_completion=None)

    await runner._finish_completed(tid, report, None, user_query="q")

    assert observed.get("ran") is True, "无 tracker 时必须内联执行"
    assert observed["status_at_enrich"] == "completed"
