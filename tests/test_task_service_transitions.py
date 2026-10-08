"""状态机在 service 层的实际拦截效果。

`test_task_domain.py` 验证规则本身；本文件验证规则**真的接上了**——
即 `agent_service` 的写入路径确实会拒绝非法迁移，而不是绕过状态机直接落库。
"""

import pytest

from backend.domain.task import InvalidTaskTransition, TaskStatus
from backend.services import agent_service as svc


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """把任务存储指向临时目录，避免污染真实 data/。

    必须替换 ``resolve_database_url``（配置解析层），而不是
    ``_default_db_path`` 或 ``DR_DATA_DIR``：真实 ``config.yml`` 里写有
    ``database.sqlite.path: data/tasks.db``，该显式配置**优先于**默认值，
    因此只改默认值或数据目录都不会生效，测试会静默写进真实数据库。
    """
    from backend.db import engine as db_engine
    from backend.db.models import Base

    db_path = tmp_path / "tasks.db"
    monkeypatch.setattr(
        db_engine,
        "resolve_database_url",
        lambda: f"sqlite+pysqlite:///{db_path}",
    )
    db_engine.reset_engine()
    # 生产环境的建表由 Alembic 负责；服务层测试直接从模型建表即可
    # （迁移本身由 tests/test_task_migration.py 单独验证）
    Base.metadata.create_all(db_engine.get_engine())

    yield db_path.parent

    db_engine.reset_engine()
    assert db_path.exists(), "测试未使用临时数据库——隔离失效"


def test_created_task_starts_pending(isolated_store):
    task = svc.create_task("测试查询")
    assert task["status"] == "pending"

    status = svc.get_task_status(task["thread_id"])
    assert status["status"] == TaskStatus.PENDING.value


def test_pending_to_running_allowed(isolated_store):
    task = svc.create_task("q")
    tid = task["thread_id"]

    svc.set_task_stage(tid, "write_research_brief")
    assert svc.get_task_status(tid)["status"] == TaskStatus.RUNNING.value
    assert svc.get_task_status(tid)["stage"] == "write_research_brief"


def test_running_to_waiting_review_carries_draft(isolated_store):
    """HITL 关键路径：草稿必须落库，否则前端无法预览。"""
    task = svc.create_task("q")
    tid = task["thread_id"]

    svc.set_task_stage(tid, "write_research_brief")
    svc.set_task_stage(tid, "human_review", draft_report="# 草稿内容")

    status = svc.get_task_status(tid)
    assert status["status"] == TaskStatus.WAITING_REVIEW.value
    assert status["draft_report"] == "# 草稿内容"


def test_waiting_review_back_to_running(isolated_store):
    """审查通过后恢复执行。"""
    task = svc.create_task("q")
    tid = task["thread_id"]

    svc.set_task_stage(tid, "write_research_brief")
    svc.set_task_stage(tid, "human_review")
    svc.set_task_stage(tid, "supervisor_subgraph")

    assert svc.get_task_status(tid)["status"] == TaskStatus.RUNNING.value


def test_full_happy_path(isolated_store):
    task = svc.create_task("q")
    tid = task["thread_id"]

    svc.set_task_stage(tid, "write_research_brief")   # pending -> running
    svc.set_task_stage(tid, "human_review")           # running -> waiting_review
    svc.set_task_stage(tid, "supervisor_subgraph")    # waiting_review -> running
    svc.mark_task_completed(tid, "# 最终报告", {"ok": True})

    status = svc.get_task_status(tid)
    assert status["status"] == TaskStatus.COMPLETED.value
    assert status["final_report"] == "# 最终报告"
    assert status["verification"] == {"ok": True}


def test_completed_task_cannot_restart(isolated_store):
    """终态任务不得被重新启动。

    这是真实风险：GET /stream 曾被用于驱动执行，若对已完成任务重连，
    会把状态从 completed 改回 running 并重启整张图。
    """
    task = svc.create_task("q")
    tid = task["thread_id"]
    svc.set_task_stage(tid, "write_research_brief")
    svc.mark_task_completed(tid, "报告", None)

    with pytest.raises(InvalidTaskTransition):
        svc.set_task_stage(tid, "write_research_brief")

    assert svc.get_task_status(tid)["status"] == TaskStatus.COMPLETED.value


def test_failed_task_cannot_restart(isolated_store):
    task = svc.create_task("q")
    tid = task["thread_id"]
    svc.mark_task_failed(tid, "boom")

    with pytest.raises(InvalidTaskTransition):
        svc.set_task_stage(tid, "write_research_brief")


def test_transition_on_missing_task_is_noop(isolated_store):
    """保持既有语义：对不存在的任务写入是静默 no-op，不抛异常。"""
    svc.transition_task("nonexistent-id", TaskStatus.RUNNING)
    assert svc.get_task_status("nonexistent-id")["status"] == "unknown"


def test_soft_delete_from_any_state(isolated_store):
    task = svc.create_task("q")
    tid = task["thread_id"]
    svc.set_task_stage(tid, "write_research_brief")

    svc.mark_task_deleted(tid)
    assert svc.get_task_status(tid)["status"] == TaskStatus.DELETED.value
    # 已删除的任务不出现在历史列表中
    assert tid not in {t["thread_id"] for t in svc.list_tasks()}


def test_deleted_task_is_terminal(isolated_store):
    task = svc.create_task("q")
    tid = task["thread_id"]
    svc.mark_task_deleted(tid)

    with pytest.raises(InvalidTaskTransition):
        svc.set_task_stage(tid, "write_research_brief")


def test_idempotent_status_write(isolated_store):
    """重复写入同一状态必须成功（重试 / 重复 resume 是正常现象）。"""
    task = svc.create_task("q")
    tid = task["thread_id"]

    svc.set_task_stage(tid, "supervisor_subgraph")
    svc.set_task_stage(tid, "supervisor_subgraph")
    assert svc.get_task_status(tid)["status"] == TaskStatus.RUNNING.value


# ===== 启动时清理残留任务 =====


def test_fail_stale_tasks_marks_running_and_pending(isolated_store):
    running = svc.create_task("r")["thread_id"]
    pending = svc.create_task("p")["thread_id"]
    done = svc.create_task("d")["thread_id"]

    svc.set_task_stage(running, "supervisor_subgraph")
    svc.set_task_stage(done, "supervisor_subgraph")
    svc.mark_task_completed(done, "报告", None)

    count = svc.fail_stale_tasks("后端重启导致任务中断，请重新提交")

    assert count == 2
    assert svc.get_task_status(running)["status"] == TaskStatus.FAILED.value
    assert svc.get_task_status(pending)["status"] == TaskStatus.FAILED.value
    # 已完成的任务不受影响
    assert svc.get_task_status(done)["status"] == TaskStatus.COMPLETED.value


def test_fail_stale_tasks_is_idempotent(isolated_store):
    tid = svc.create_task("q")["thread_id"]
    svc.mark_task_failed(tid, "already failed")

    assert svc.fail_stale_tasks("again") == 0
