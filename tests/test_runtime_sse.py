"""G6 —— SSE 投影（执行包 §27）。

本文件锁定的两条不变量是 Phase G 的核心验收项：

```text
SSE 只观察，不驱动   → SSE 连接/断开不改变任务状态
SSE 断开 ≠ 任务取消  → worker 照常跑完
```

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
from backend.runtime import events as ev
from backend.runtime import queue as q
from backend.runtime import sse as projection
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

    yield {"redis": client, "repo": get_repository(), "checkpointer": managed.saver,
           "settings": get_settings()}

    await client.flushdb()
    await client.aclose()
    await managed.aclose()
    ckpt.discard_checkpointer()
    db_engine.reset_engine()


def _new_task(env) -> str:
    tid = uuid.uuid4().hex[:12]
    env["repo"].create(tid, "Compare LangGraph and CrewAI")
    return tid


async def _collect(agen, limit: int, timeout: float = 3.0) -> list[str]:
    """收集最多 limit 个 SSE 帧。

    必须带超时：投影在「任务未终态且暂无新事件」时会**合法地**保持连接
    打开（这正是 SSE 该有的行为），没有超时的收集器会挂住。
    """
    frames: list[str] = []

    async def _drain() -> None:
        async for frame in agen:
            frames.append(frame)
            if len(frames) >= limit:
                break

    try:
        await asyncio.wait_for(_drain(), timeout)
    except (TimeoutError, asyncio.TimeoutError):
        pass
    finally:
        await agen.aclose()

    return frames


# ===== 投影不驱动执行 =====


def test_sse_route_module_does_not_build_graph():
    """**核心验收项**：API 路由不得构建或驱动 LangGraph（§14）。

    用 **AST** 而不是文本匹配：文本匹配会把注释与 docstring 也算进去，
    本文件最初就因此误报（模块 docstring 里恰好写了 "_create_builder"
    这个禁令本身）。AST 只看真正的导入与调用，判据才是准确的。
    """
    import ast
    import inspect

    from backend.routes import research as route

    tree = ast.parse(inspect.getsource(route))

    imported: set[str] = set()
    called_attrs: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name.split(".")[-1] for a in node.names)
        elif isinstance(node, ast.Attribute):
            called_attrs.add(node.attr)

    forbidden_imports = {"_create_builder", "_build_agent", "sse_event_stream"}
    assert not (imported & forbidden_imports), (
        f"API 路由不得导入图构建/驱动入口: {imported & forbidden_imports}"
    )

    forbidden_attrs = {"astream_events", "astream", "aget_state"}
    assert not (called_attrs & forbidden_attrs), (
        f"API 路由不得调用图执行 API: {called_attrs & forbidden_attrs}"
    )


async def test_connecting_sse_does_not_change_task_status(env):
    """连上投影不应改变任何任务状态。"""
    tid = _new_task(env)
    before = env["repo"].get(tid).status

    # 发布一个**面向浏览器**的事件（内部事件会被投影过滤，见下一条用例）
    await ev.publish(env["redis"], tid, ev.EventType.GRAPH_NODE_STARTED, {"node": "supervisor"})
    frames = await _collect(
        projection.project(env["redis"], env["repo"], tid, last_event_id="0-0"), 1
    )
    assert frames, "应能读到事件"

    assert env["repo"].get(tid).status == before, "投影不得改变任务状态"


# ===== 事件名映射（前端契约）=====


async def test_canonical_events_are_translated_to_frontend_names(env):
    """canonical 名 → 浏览器契约名的映射是前端零改动的前提。"""
    tid = _new_task(env)

    await ev.publish(env["redis"], tid, ev.EventType.GRAPH_NODE_STARTED, {"node": "supervisor"})
    await ev.publish(env["redis"], tid, ev.EventType.GRAPH_NODE_COMPLETED, {"node": "supervisor"})
    await ev.publish(env["redis"], tid, ev.EventType.TOOL_STARTED, {"tool": "tavily_search"})
    await ev.publish(env["redis"], tid, ev.EventType.TASK_WAITING_REVIEW, {"draft_preview": "# 草稿"})

    frames = await _collect(
        projection.project(env["redis"], env["repo"], tid, last_event_id="0-0"), 4
    )
    text = "".join(frames)

    for wire in ("node_start", "node_complete", "tool_call", "human_review_required"):
        assert wire in text, f"缺少前端契约事件 {wire}"

    # canonical 名不应泄漏给浏览器
    assert "graph.node.started" not in text


async def test_internal_events_are_not_sent_to_browser(env):
    """task.queued / task.claimed 等仅供可观测性消费。"""
    tid = _new_task(env)
    await ev.publish(env["redis"], tid, ev.EventType.TASK_QUEUED, {})

    frames = await _collect(
        projection.project(env["redis"], env["repo"], tid, last_event_id="0-0"), 1
    )
    assert frames == [], "内部事件不应产生 SSE 帧"


async def test_sse_frames_carry_event_id_for_reconnect(env):
    """`id:` 行是 Last-Event-ID 重连的基础。"""
    tid = _new_task(env)
    await ev.publish(env["redis"], tid, ev.EventType.GRAPH_NODE_STARTED, {"node": "supervisor"})

    frames = await _collect(
        projection.project(env["redis"], env["repo"], tid, last_event_id="0-0"), 1
    )
    assert frames[0].startswith("id: ")


# ===== 回放 =====


async def test_reconnect_replays_missed_events(env):
    """§15：收到 42 → 断开 → 期间产生 43..51 → 从 42 重连 → 回放 43..51。"""
    tid = _new_task(env)
    # 必须用面向浏览器的事件：TASK_STARTED 属内部事件，投影会过滤掉
    for _ in range(6):
        await ev.publish(env["redis"], tid, ev.EventType.GRAPH_NODE_STARTED, {"node": "supervisor"})

    all_frames = await _collect(
        projection.project(env["redis"], env["repo"], tid, last_event_id="0-0"), 6
    )
    assert len(all_frames) == 6

    third_id = all_frames[2][4:].split("\n", 1)[0].strip()

    replayed = await _collect(
        projection.project(env["redis"], env["repo"], tid, last_event_id=third_id), 3
    )
    assert len(replayed) == 3, "应只回放第三帧之后的事件"


async def test_terminal_task_gets_terminal_frame_from_db(env):
    """任务已终态：即使事件被驱逐，也要按 DB 补发终态（§10.4）。"""
    tid = _new_task(env)
    task = env["repo"].get(tid)
    task.status = TaskStatus.COMPLETED.value
    task.final_report = "# 报告"
    env["repo"].save(task)

    frames = await _collect(
        projection.project(env["redis"], env["repo"], tid, last_event_id="0-0"), 1
    )
    assert frames and "complete" in frames[0]


# ===== SSE 断开不停止任务（核心不变量）=====


async def test_sse_disconnect_does_not_stop_task(env):
    """§15：客户端断开 → worker 继续 → 任务完成。

    做法：不建立任何 SSE 连接，直接驱动 worker —— 若任务仍能完成，
    就证明执行不依赖 SSE 是否存在。
    """
    tid = _new_task(env)

    runner = TaskRunner(
        redis=env["redis"], worker_id="sse-test:1:aaaa1111",
        settings=env["settings"], repository=env["repo"], checkpointer=env["checkpointer"],
    )
    outcome = await runner.run(tid)

    assert outcome.status == TaskStatus.WAITING_REVIEW.value, "无 SSE 连接时 worker 仍应执行"

    # 审查后继续跑到完成，同样全程无 SSE
    env["repo"].create_review(tid, "approve", "")
    outcome2 = await runner.run(tid)

    assert outcome2.status == TaskStatus.COMPLETED.value
    assert env["repo"].get(tid).final_report


async def test_worker_does_not_wait_for_consumers(env):
    """慢客户端不得拖慢 worker：worker 只 XADD，不等待消费者。"""
    tid = _new_task(env)

    # 完全不消费事件的前提下跑完整流程
    runner = TaskRunner(
        redis=env["redis"], worker_id="sse-test:2:bbbb2222",
        settings=env["settings"], repository=env["repo"], checkpointer=env["checkpointer"],
    )
    await runner.run(tid)

    # 事件都还在，等着被投影——说明 worker 没有因无人消费而阻塞
    events = await ev.read_since(env["redis"], tid)
    assert len(events) > 0


async def test_events_survive_api_absence(env):
    """API 重启场景的实质：任务状态在 DB + Redis，不在 API 进程里。

    这里完全不涉及的 API 进程地跑一遍 worker，再新建一个 repository 实例
    读状态——证明状态不依赖任何进程内缓存。
    """
    tid = _new_task(env)
    runner = TaskRunner(
        redis=env["redis"], worker_id="sse-test:3:cccc3333",
        settings=env["settings"], repository=env["repo"], checkpointer=env["checkpointer"],
    )
    await runner.run(tid)

    # 模拟"新进程"：丢弃所有进程内缓存，重新读
    db_engine.reset_engine()
    fresh_repo = get_repository()

    task = fresh_repo.get(tid)
    assert task.status == TaskStatus.WAITING_REVIEW.value
    assert task.draft_report, "草稿必须能从 DB 读回"

    events = await ev.read_since(env["redis"], tid)
    assert events, "事件必须能从 Redis 读回"


async def test_duplicate_job_does_not_double_execute(env):
    """§25：重复投递不得导致双执行——claim 是唯一安全边界。"""
    tid = _new_task(env)
    env["redis"].flushdb()  # 清掉 claim，确保从干净状态开始
    await q.ensure_group(env["redis"])
    await q.enqueue(env["redis"], tid, dedupe_ttl_ms=0)  # dedupe 关闭，允许重复
    await q.enqueue(env["redis"], tid, dedupe_ttl_ms=0)

    jobs = await q.consume(env["redis"], "w1", block_ms=300)
    assert len(jobs) >= 1

    from backend.worker import Worker

    w1 = Worker()
    w1.worker_id = "dup-test:1:dddd4444"
    w1._redis = env["redis"]
    w1._checkpointer = env["checkpointer"]
    w1.settings = env["settings"]

    # 两个 job 交给同一个 worker —— 第二次应因 claim 已被持有而跳过
    await w1._handle(jobs[0])
    attempt_after_first = env["repo"].get(tid).attempt

    if len(jobs) > 1:
        await w1._handle(jobs[1])
        assert env["repo"].get(tid).attempt == attempt_after_first, (
            "第二个 job 不得再次执行（claim 应拦住）"
        )


# ===== §8 envelope 契约 =====


async def test_sse_frame_has_stable_envelope(env):
    """§8：事件必须有稳定 envelope。

    同时**必须保留** `event` / `data` 两个字段名 —— 前端 switch 依赖它们。
    """
    import json as _json

    tid = _new_task(env)
    await ev.publish(env["redis"], tid, ev.EventType.GRAPH_NODE_STARTED, {"node": "supervisor"})

    frames = await _collect(
        projection.project(env["redis"], env["repo"], tid, last_event_id="0-0"), 1
    )
    assert frames

    payload = _json.loads(frames[0].split("data: ", 1)[1].strip())

    # 前端契约字段（不可改名）
    assert payload["event"] == "node_start"
    assert "data" in payload

    # envelope 元数据（§8 要求）
    for field in ("event_id", "task_id", "sequence", "timestamp", "schema_version"):
        assert field in payload, f"envelope 缺少 {field}"

    assert payload["task_id"] == tid
    assert payload["sequence"] >= 1
    assert payload["schema_version"] == projection.ENVELOPE_SCHEMA_VERSION


async def test_sse_frame_id_matches_envelope_event_id(env):
    """SSE 的 `id:` 行必须与 envelope 的 event_id 一致 —— Last-Event-ID 依赖它。"""
    import json as _json

    tid = _new_task(env)
    await ev.publish(env["redis"], tid, ev.EventType.GRAPH_NODE_STARTED, {"node": "supervisor"})

    frames = await _collect(
        projection.project(env["redis"], env["repo"], tid, last_event_id="0-0"), 1
    )
    header_id = frames[0][4:].split("\n", 1)[0].strip()
    payload = _json.loads(frames[0].split("data: ", 1)[1].strip())

    assert header_id == payload["event_id"]
    assert header_id == "0-0" or "-" in header_id, "应为 Redis Stream ID 形态"
