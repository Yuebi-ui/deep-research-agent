"""G3 / G4 —— Worker 执行与事件（执行包 §25）。

隔离：
* DB   —— 替换 resolve_database_url，指向临时 SQLite
* Redis —— db 15，用例前后 FLUSHDB
* Checkpointer —— sqlite，指向临时文件（worker 拒绝 memory 后端）

图是**真实**的（Fake 模型），因此这些测试同时覆盖了
「worker 能驱动真图并正确落库」。
"""

from __future__ import annotations

import uuid

import pytest

from backend.core.settings import get_settings
from backend.db import engine as db_engine
from backend.db import get_repository
from backend.db.models import Base
from backend.domain.task import TaskStatus
from backend.runtime import events as ev
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


@pytest.fixture
async def env(tmp_path, monkeypatch):
    """隔离的 DB + Redis + sqlite checkpointer。"""
    import redis.asyncio as aioredis

    import deep_research.checkpoint as ckpt

    # --- DB ---
    db_path = tmp_path / "tasks.db"
    monkeypatch.setattr(db_engine, "resolve_database_url", lambda: f"sqlite+pysqlite:///{db_path}")
    db_engine.reset_engine()
    Base.metadata.create_all(db_engine.get_engine())

    # --- Checkpointer（sqlite，worker 不接受 memory）---
    ckpt_path = tmp_path / "ckpt.db"
    monkeypatch.setattr(ckpt, "resolve_sqlite_path", lambda: str(ckpt_path))
    ckpt.discard_checkpointer()
    managed = await ckpt.create_checkpointer("sqlite")

    # --- Redis ---
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


def _runner(env) -> TaskRunner:
    return TaskRunner(
        redis=env["redis"],
        worker_id="test-worker:1:abcdef01",
        settings=env["settings"],
        repository=env["repo"],
        checkpointer=env["checkpointer"],
    )


def _new_task(env, query: str = "Compare LangGraph and CrewAI") -> str:
    tid = uuid.uuid4().hex[:12]
    env["repo"].create(tid, query)
    return tid


def _status(env, tid: str) -> str:
    task = env["repo"].get(tid)
    return task.status if task else "missing"


# ===== 首次执行 =====


async def test_runner_executes_task_to_waiting_review(env):
    """worker 驱动真图，停在 HITL 中断，草稿落库。"""
    tid = _new_task(env)

    outcome = await _runner(env).run(tid)

    assert outcome.status == TaskStatus.WAITING_REVIEW.value
    assert _status(env, tid) == TaskStatus.WAITING_REVIEW.value

    task = env["repo"].get(tid)
    assert task.draft_report, "草稿必须落库（前端据此做审查预览）"
    assert task.attempt == 1


# ===== 审查后 resume =====


async def test_runner_resumes_after_review_to_completion(env):
    """记录审查决定后再次执行 → 从 checkpoint 恢复并完成。"""
    tid = _new_task(env)
    await _runner(env).run(tid)
    assert _status(env, tid) == TaskStatus.WAITING_REVIEW.value

    env["repo"].create_review(tid, "approve", "")

    outcome = await _runner(env).run(tid)

    assert outcome.status == TaskStatus.COMPLETED.value
    task = env["repo"].get(tid)
    assert task.status == TaskStatus.COMPLETED.value
    assert task.final_report, "最终报告必须落库"


async def test_runner_waits_when_no_review_decision(env):
    """到达审查点但没有决定 —— 应停留等待，而不是失败。"""
    tid = _new_task(env)
    await _runner(env).run(tid)

    outcome = await _runner(env).run(tid)

    assert outcome.status == TaskStatus.WAITING_REVIEW.value
    assert _status(env, tid) == TaskStatus.WAITING_REVIEW.value


async def test_review_is_marked_consumed(env):
    """消费标记用于审计 —— 但恢复决策不依赖它（见设计文档 §6.2）。"""
    tid = _new_task(env)
    await _runner(env).run(tid)
    review = env["repo"].create_review(tid, "approve", "")

    await _runner(env).run(tid)

    stored = env["repo"].latest_review(tid)
    assert stored.consumed_at is not None
    assert stored.consumed_by == "test-worker:1:abcdef01"
    assert stored.id == review.id


# ===== 终态与取消 =====


async def test_terminal_task_is_skipped(env):
    """终态任务不得被重新执行（§24 test_terminal_task_is_not_reclaimed）。"""
    tid = _new_task(env)
    task = env["repo"].get(tid)
    task.status = TaskStatus.COMPLETED.value
    env["repo"].save(task)

    outcome = await _runner(env).run(tid)
    assert outcome.status == TaskStatus.COMPLETED.value
    assert "已是终态" in outcome.detail


async def test_cancelled_task_is_not_executed(env):
    """取消请求必须在执行前生效（§24 test_cancelled_task_is_not_reclaimed）。"""
    tid = _new_task(env)
    task = env["repo"].get(tid)
    task.cancel_requested = True
    env["repo"].save(task)

    outcome = await _runner(env).run(tid)

    assert outcome.status == TaskStatus.CANCELLED.value
    assert _status(env, tid) == TaskStatus.CANCELLED.value


async def test_missing_task_is_not_fatal(env):
    outcome = await _runner(env).run("nonexistent-id")
    assert "不存在" in outcome.detail


# ===== 失败路径 =====


async def test_runner_persists_failure(env, monkeypatch):
    """图异常且不可重试 → FAILED，error 落库，事件发出。"""
    tid = _new_task(env)

    # 让图在运行中抛一个不可重试的领域错误
    from backend.runtime import runner as runner_mod

    # 必须是普通 async def（_stream 是被 await 的协程），
    # 带 yield 会变成 async generator，await 它会报
    # "'async_generator' object can't be awaited"
    async def boom(*_a, **_kw):
        raise ValueError("图执行失败")

    monkeypatch.setattr(runner_mod.TaskRunner, "_stream", boom)

    outcome = await _runner(env).run(tid)

    assert outcome.status == TaskStatus.FAILED.value
    task = env["repo"].get(tid)
    assert task.status == TaskStatus.FAILED.value
    assert task.error and "图执行失败" in task.error


# ===== 事件（G4）=====


async def test_events_are_published_with_monotonic_sequence(env):
    tid = _new_task(env)
    await _runner(env).run(tid)

    events = await ev.read_since(env["redis"], tid)

    assert events, "应产生事件"
    sequences = [e["sequence"] for e in events]
    assert sequences == sorted(sequences), "sequence 必须单调递增"
    assert len(set(sequences)) == len(sequences), "sequence 不得重复"

    types = [e["type"] for e in events]
    assert ev.EventType.TASK_CLAIMED in types
    assert ev.EventType.TASK_STARTED in types
    assert ev.EventType.TASK_WAITING_REVIEW in types


async def test_completion_emits_completed_event(env):
    tid = _new_task(env)
    await _runner(env).run(tid)
    env["repo"].create_review(tid, "approve", "")
    await _runner(env).run(tid)

    types = [e["type"] for e in await ev.read_since(env["redis"], tid)]
    assert ev.EventType.TASK_COMPLETED in types
    assert ev.EventType.TASK_RESUMED in types


async def test_events_do_not_leak_secrets(env):
    """事件会被长期保留 —— 不得写入敏感字段（设计文档 §9.6）。"""
    tid = _new_task(env)
    await ev.publish(
        env["redis"], tid, "test.event",
        {"api_key": "EXAMPLE_API_KEY_DO_NOT_EMIT", "ok": "fine"},
    )

    events = await ev.read_since(env["redis"], tid)
    payload = str(events[0]["data"])

    assert "EXAMPLE_API_KEY_DO_NOT_EMIT" not in payload
    assert "fine" in payload


async def test_read_since_supports_replay(env):
    """回放：从某个 event_id 之后继续读 —— SSE 重连的基础。"""
    tid = _new_task(env)
    for i in range(5):
        await ev.publish(env["redis"], tid, "test.event", {"i": i})

    all_events = await ev.read_since(env["redis"], tid)
    assert len(all_events) == 5

    third = all_events[2]["event_id"]
    rest = await ev.read_since(env["redis"], tid, after_id=third)

    assert len(rest) == 2, "应只回放第三个之后的事件"
    assert rest[0]["data"]["i"] == 3
