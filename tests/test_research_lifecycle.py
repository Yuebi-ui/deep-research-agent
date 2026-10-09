"""任务生命周期 / HITL —— API 契约层（执行包 §15-§21、§28）。

**Phase G 之后本文件的定位变了。**

之前它验证的是「API 驱动图的完整生命周期」。Phase G 把执行搬到了
独立 Worker，API 只做命令、查询与投影，因此：

```text
执行层面的生命周期（图跑到哪、checkpoint 怎么恢复）
    → tests/test_runtime_worker.py（驱动真实 TaskRunner）

API 契约层（命令是否入队、状态校验、投影是否只观察）
    → 本文件
```

两者**不重复**：这里用 stub 队列断言「API 做了什么」，不驱动图。

§28 要求的 HITL 用例：
    test_review_enqueues_resume
    test_api_review_does_not_resume_graph_inline
    test_duplicate_review_is_rejected
    test_duplicate_resume_does_not_double_execute
"""

from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from backend.db import engine as db_engine
from backend.db import get_repository
from backend.db.models import Base
from backend.domain.task import TaskStatus


@pytest.fixture
def api(tmp_path, monkeypatch):
    """隔离的 DB + stub 掉 Redis 与队列投递。

    刻意**不**替换 `route._enqueue`：那个函数内部才有
    「EnqueueRejected → 503」的转换逻辑，替换掉它等于绕过被测代码。
    这里替换的是它依赖的底层：Redis 客户端与 `queue.enqueue`。
    """
    import backend.main as main_module
    from backend.routes import research as route  # noqa: F401  # 确保模块已加载
    from backend.runtime import queue as job_queue
    from backend.runtime import redis as rt_redis

    db_path = tmp_path / "tasks.db"
    monkeypatch.setattr(db_engine, "resolve_database_url", lambda: f"sqlite+pysqlite:///{db_path}")
    db_engine.reset_engine()
    Base.metadata.create_all(db_engine.get_engine())

    enqueued: list[str] = []

    class _DummyRedis:
        """publish 内部会失败并记 warning —— 事件失败不影响业务语义。"""

    monkeypatch.setattr(rt_redis, "get_redis", lambda: _DummyRedis())

    async def _record_enqueue(_redis, thread_id, **_kw):
        enqueued.append(thread_id)
        return "0-1"

    monkeypatch.setattr(job_queue, "enqueue", _record_enqueue)

    client = TestClient(main_module.app)
    yield client, enqueued, get_repository(), route

    db_engine.reset_engine()


def _create(client, query: str = "Compare LangGraph and CrewAI") -> str:
    r = client.post("/api/research/start", json={"query": query})
    assert r.status_code == 200, r.text
    return r.json()["thread_id"]


def _status(client, tid: str) -> str:
    return client.get(f"/api/research/{tid}/status").json()["status"]


def _put_waiting_review(repo, tid: str, draft: str = "# 草稿") -> None:
    """把任务置为 WAITING_REVIEW。

    直接操作 DB 是刻意的：执行层面的迁移已由 test_runtime_worker.py 覆盖，
    这里只关心「处于该状态时 API 的行为」。
    """
    task = repo.get(tid)
    task.status = TaskStatus.WAITING_REVIEW.value
    task.stage = "human_review"
    task.draft_report = draft
    repo.save(task)


# ===== §18 Create 新路径 =====


def test_create_returns_immediately_and_enqueues(api):
    client, enqueued, _repo, _route = api
    tid = _create(client)

    assert _status(client, tid) == TaskStatus.PENDING.value
    assert enqueued == [tid], "创建后应入队且只入队一次"


def test_create_does_not_run_the_graph(api):
    """创建请求**不得**执行研究——执行归 worker。"""
    client, _enqueued, _repo, route = api

    # API 模块不应再 import 图构建器
    import backend.routes.research as r

    assert not hasattr(r, "_build_agent")
    assert not hasattr(r, "sse_event_stream"), "SSE 不再驱动执行"


# ===== §17 / §28 HITL 新路径 =====


def test_review_enqueues_resume(api):
    """审查决定应落库并入队，而不是内联 resume。"""
    client, enqueued, repo, _route = api
    tid = _create(client)
    enqueued.clear()
    _put_waiting_review(repo, tid)

    r = client.post(f"/api/research/{tid}/resume", json={"action": "approve", "feedback": ""})

    assert r.status_code == 200
    assert enqueued == [tid], "审查后应入队 resume job"
    assert repo.latest_review(tid) is not None, "审查决定必须落库"
    assert repo.latest_review(tid).action == "approve"


def test_review_persists_feedback(api):
    client, _enqueued, repo, _route = api
    tid = _create(client)
    _put_waiting_review(repo, tid)

    client.post(f"/api/research/{tid}/resume",
                json={"action": "revise", "feedback": "补充数据来源"})

    review = repo.latest_review(tid)
    assert review.action == "revise"
    assert review.feedback == "补充数据来源"


def test_review_on_pending_task_is_rejected(api):
    """未经审查状态就提交 → 409，且不落库、不入队。"""
    client, enqueued, repo, _route = api
    tid = _create(client)
    enqueued.clear()

    r = client.post(f"/api/research/{tid}/resume", json={"action": "approve", "feedback": ""})

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "REVIEW_NOT_ALLOWED"
    assert repo.latest_review(tid) is None, "被拒的审查不得落库"
    assert enqueued == [], "被拒的审查不得入队"


def test_duplicate_review_is_rejected(api):
    """重复 review 不得导致重复入队（§28）。"""
    client, enqueued, repo, _route = api
    tid = _create(client)
    _put_waiting_review(repo, tid)

    first = client.post(f"/api/research/{tid}/resume", json={"action": "approve", "feedback": ""})
    assert first.status_code == 200
    enqueued.clear()

    second = client.post(f"/api/research/{tid}/resume", json={"action": "approve", "feedback": ""})
    assert second.status_code == 409
    assert enqueued == [], "重复 review 不得再次入队"


def test_review_on_completed_task_is_rejected(api):
    client, _enqueued, repo, _route = api
    tid = _create(client)
    task = repo.get(tid)
    task.status = TaskStatus.COMPLETED.value
    repo.save(task)

    r = client.post(f"/api/research/{tid}/resume", json={"action": "approve", "feedback": ""})
    assert r.status_code == 409


# ===== §26 Cancellation =====


def test_cancel_pending_task_transitions_to_cancelled(api):
    client, _enqueued, _repo, _route = api
    tid = _create(client)

    r = client.post(f"/api/research/{tid}/cancel")

    assert r.status_code == 200
    assert _status(client, tid) == TaskStatus.CANCELLED.value


def test_cancel_waiting_review_task(api):
    client, _enqueued, repo, _route = api
    tid = _create(client)
    _put_waiting_review(repo, tid)

    client.post(f"/api/research/{tid}/cancel")
    assert _status(client, tid) == TaskStatus.CANCELLED.value


def test_cancel_running_task_keeps_running_until_worker_reacts(api):
    """RUNNING 任务只置标志——协作式取消，不抢占（设计文档 §5.3）。"""
    client, _enqueued, repo, _route = api
    tid = _create(client)
    task = repo.get(tid)
    task.status = TaskStatus.RUNNING.value
    repo.save(task)

    client.post(f"/api/research/{tid}/cancel")

    task = repo.get(tid)
    assert task.cancel_requested is True, "取消请求必须持久化"
    assert task.status == TaskStatus.RUNNING.value, "不应立即改变状态——等 worker 在边界处理"


def test_cancel_is_idempotent(api):
    client, _enqueued, _repo, _route = api
    tid = _create(client)

    client.post(f"/api/research/{tid}/cancel")
    r = client.post(f"/api/research/{tid}/cancel")

    assert r.status_code == 200
    assert _status(client, tid) == TaskStatus.CANCELLED.value


def test_cancel_terminal_task_is_rejected(api):
    client, _enqueued, repo, _route = api
    tid = _create(client)
    task = repo.get(tid)
    task.status = TaskStatus.COMPLETED.value
    repo.save(task)

    r = client.post(f"/api/research/{tid}/cancel")
    assert r.status_code == 409


# ===== §19 Soft delete =====


def test_soft_delete(api):
    client, _enqueued, _repo, _route = api
    tid = _create(client)

    assert client.delete(f"/api/research/{tid}").status_code == 200
    assert _status(client, tid) == TaskStatus.DELETED.value


def test_deleted_task_excluded_from_history(api):
    client, _enqueued, _repo, _route = api
    keep, drop = _create(client, "保留"), _create(client, "删除")
    client.delete(f"/api/research/{drop}")

    ids = {t["thread_id"] for t in client.get("/api/research/history/list").json()}
    assert keep in ids and drop not in ids


def test_deleted_task_still_directly_readable(api):
    client, _enqueued, _repo, _route = api
    tid = _create(client)
    client.delete(f"/api/research/{tid}")
    assert _status(client, tid) == TaskStatus.DELETED.value


# ===== 查询 =====


def test_unknown_task_returns_404(api):
    client, _enqueued, _repo, _route = api
    assert client.get("/api/research/nonexistent/status").status_code == 404


def test_report_reads_from_db(api):
    """报告直接读 DB，不再经 graph 读 checkpoint（§35.3）。"""
    client, _enqueued, repo, _route = api
    tid = _create(client)
    task = repo.get(tid)
    task.status = TaskStatus.COMPLETED.value
    task.final_report = "# 最终报告"
    task.verification = {"total_claims": 3}
    repo.save(task)

    body = client.get(f"/api/research/{tid}/report").json()
    assert body["final_report"] == "# 最终报告"
    assert body["verification"] == {"total_claims": 3}


def test_report_404_when_not_completed(api):
    client, _enqueued, _repo, _route = api
    tid = _create(client)
    assert client.get(f"/api/research/{tid}/report").status_code == 404


def test_status_exposes_runtime_metadata(api):
    client, _enqueued, _repo, _route = api
    tid = _create(client)

    body = client.get(f"/api/research/{tid}/status").json()
    assert "attempt" in body
    assert "cancel_requested" in body


def test_create_returns_503_when_queue_unavailable(api, monkeypatch):
    """Redis 不可达时入队失败 → 503，且任务**不**被标记为失败（§8.4）。

    注意 patch 的是底层的 `queue.enqueue`，而不是路由的 `_enqueue`——
    后者内部才有 try/except 与 503 的转换逻辑，替换掉它等于绕过被测代码。
    """
    client, _enqueued, repo, _route = api
    from backend.runtime import queue as job_queue

    async def _boom(*_a, **_kw):
        raise job_queue.EnqueueRejected("redis down")

    monkeypatch.setattr(job_queue, "enqueue", _boom)

    r = client.post("/api/research/start", json={"query": "q"})

    assert r.status_code == 503
    assert r.json()["error"]["code"] == "QUEUE_UNAVAILABLE"

    # 任务保持 PENDING，不被标成 FAILED —— 用户可以重试
    tasks = repo.list_tasks()
    assert tasks and tasks[0].status == TaskStatus.PENDING.value
