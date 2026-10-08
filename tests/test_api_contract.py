"""API 契约测试（执行计划 §7 / §8 / §11）。

锁定三件事：

```text
1. 主要端点的响应有**明确 schema**，不是裸 dict（§7.3）
2. 错误响应结构统一（§7.4）+ HTTP 状态码语义（§7.5）
3. SSE envelope 稳定（§8）

这些测试不驱动图，只验证 HTTP 层面的契约。
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from backend.db import engine as db_engine
from backend.db import get_repository
from backend.db.models import Base
from backend.domain.task import TaskStatus

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def api(tmp_path, monkeypatch):
    import backend.main as main_module
    from backend.runtime import queue as job_queue
    from backend.runtime import redis as rt_redis

    db_path = tmp_path / "tasks.db"
    monkeypatch.setattr(db_engine, "resolve_database_url", lambda: f"sqlite+pysqlite:///{db_path}")
    db_engine.reset_engine()
    Base.metadata.create_all(db_engine.get_engine())

    class _DummyRedis:
        pass

    monkeypatch.setattr(rt_redis, "get_redis", lambda: _DummyRedis())

    async def _noop_enqueue(*_a, **_kw):
        return "0-1"

    monkeypatch.setattr(job_queue, "enqueue", _noop_enqueue)

    client = TestClient(main_module.app)
    yield client, get_repository(), main_module
    db_engine.reset_engine()


def _create(client) -> str:
    r = client.post("/api/research/start", json={"query": "q"})
    assert r.status_code == 200, r.text
    return r.json()["thread_id"]


# ===== §7.3 响应 schema =====


def test_openapi_declares_response_schemas_for_main_endpoints(api):
    """主要端点必须声明 response schema —— 否则 OpenAPI 里是空 object。"""
    client, _repo, main_module = api
    spec = main_module.app.openapi()

    expected_ref = {
        ("/api/research/start", "post"): "TaskCreatedResponse",
        ("/api/research/{thread_id}/status", "get"): "TaskSummaryResponse",
        ("/api/research/{thread_id}/report", "get"): "ReportResponse",
        ("/api/research/{thread_id}/cancel", "post"): "CancelResponse",
        ("/api/research/{thread_id}/resume", "post"): "ReviewResponse",
        ("/api/health", "get"): "HealthResponse",
    }

    for (path, method), schema_name in expected_ref.items():
        op = spec["paths"][path][method]
        ref = (
            op["responses"]["200"]["content"]["application/json"]["schema"]
            .get("$ref", "")
        )
        assert ref.endswith(schema_name), (
            f"{method.upper()} {path} 的响应 schema 应为 {schema_name}，实际 {ref!r}"
        )


def test_list_endpoint_declares_array_schema(api):
    client, _repo, main_module = api
    spec = main_module.app.openapi()
    op = spec["paths"]["/api/research/history/list"]["get"]
    schema = op["responses"]["200"]["content"]["application/json"]["schema"]

    assert schema.get("type") == "array"
    assert schema["items"]["$ref"].endswith("TaskListItem")


def test_committed_openapi_json_is_up_to_date(api):
    """仓库里的 openapi.json 必须与当前 schema 一致（§9 漂移检测的本地版）。"""
    import json
    import subprocess
    import sys

    out = REPO_ROOT / "openapi.json"
    assert out.exists(), "openapi.json 不存在；请运行 python scripts/export_openapi.py"

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "export_openapi.py"), "--check"],
        capture_output=True, text=True, encoding="utf-8", cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"OpenAPI 未同步:\n{result.stdout}\n{result.stderr}"
    json.loads(out.read_text(encoding="utf-8"))  # 至少要是合法 JSON


# ===== §7.4 错误契约 =====


def _assert_error_shape(body: dict, expected_code: str) -> None:
    assert set(body.keys()) == {"error"}, f"错误体应为 {{error: ...}}，实际 {body.keys()}"
    err = body["error"]
    assert err["code"] == expected_code
    assert err["message"]
    assert "request_id" in err


def test_task_not_found_uses_unified_error(api):
    client, _repo, _main = api
    r = client.get("/api/research/nonexistent/status")

    assert r.status_code == 404
    _assert_error_shape(r.json(), "TASK_NOT_FOUND")


def test_invalid_review_uses_unified_error(api):
    client, _repo, _main = api
    tid = _create(client)

    r = client.post(f"/api/research/{tid}/resume", json={"action": "approve", "feedback": ""})

    assert r.status_code == 409
    _assert_error_shape(r.json(), "REVIEW_NOT_ALLOWED")


def test_cancel_terminal_task_uses_unified_error(api):
    client, repo, _main = api
    tid = _create(client)
    task = repo.get(tid)
    task.status = TaskStatus.COMPLETED.value
    repo.save(task)

    r = client.post(f"/api/research/{tid}/cancel")

    assert r.status_code == 409
    _assert_error_shape(r.json(), "CANCEL_NOT_ALLOWED")


def test_validation_error_uses_unified_error(api):
    """422 也走统一结构，而不是 FastAPI 默认的 {detail: [...]}。"""
    client, _repo, _main = api

    r = client.post("/api/research/start", json={"query": ""})  # min_length=1

    assert r.status_code == 422
    _assert_error_shape(r.json(), "VALIDATION_ERROR")


def test_queue_unavailable_uses_unified_error(api, monkeypatch):
    client, _repo, _main = api
    from backend.runtime import queue as job_queue

    async def _boom(*_a, **_kw):
        raise job_queue.EnqueueRejected("redis down")

    monkeypatch.setattr(job_queue, "enqueue", _boom)

    r = client.post("/api/research/start", json={"query": "q"})

    assert r.status_code == 503
    _assert_error_shape(r.json(), "QUEUE_UNAVAILABLE")


def test_error_response_never_leaks_traceback(api):
    """§7.4：绝不把 traceback 返回给前端。"""
    client, _repo, _main = api
    r = client.get("/api/research/nonexistent/status")

    text = r.text
    for leaked in ("Traceback", "File \"", ".py\", line"):
        assert leaked not in text, f"错误响应泄漏了 {leaked}"


# ===== §13 Request ID =====


def test_request_id_is_echoed(api):
    client, _repo, _main = api
    r = client.get("/api/health", headers={"X-Request-ID": "probe-abc"})

    assert r.headers.get("x-request-id") == "probe-abc"


def test_request_id_is_generated_when_absent(api):
    client, _repo, _main = api
    r = client.get("/api/health")

    assert r.headers.get("x-request-id")


def test_error_response_carries_request_id(api):
    client, _repo, _main = api
    r = client.get("/api/research/nope/status", headers={"X-Request-ID": "trace-me"})

    assert r.json()["error"]["request_id"] == "trace-me"


# ===== §7.5 状态码语义 =====


def test_status_codes_are_semantically_consistent(api):
    client, repo, _main = api
    tid = _create(client)

    assert client.get("/api/research/nope/status").status_code == 404
    assert client.post("/api/research/start", json={"query": ""}).status_code == 422
    assert client.post(f"/api/research/{tid}/resume",
                       json={"action": "approve"}).status_code == 409

    # 取消用**另一个**任务：软删除后的任务是 DELETED 终态，
    # 对它 cancel 应当 409（已由 test_cancel_terminal_task_uses_unified_error 覆盖）
    other = _create(client)
    assert client.post(f"/api/research/{other}/cancel").status_code == 200

    assert client.delete(f"/api/research/{tid}").status_code == 200


def test_idempotent_cancel_returns_200(api):
    client, _repo, _main = api
    tid = _create(client)

    assert client.post(f"/api/research/{tid}/cancel").status_code == 200
    assert client.post(f"/api/research/{tid}/cancel").status_code == 200
