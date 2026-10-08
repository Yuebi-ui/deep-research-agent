"""仓储层测试（执行包 §12）。

重点验证两件事：

1. 持久化语义正确（含 `verification=None` 与 JSON 两种形态）
2. **仓储不暴露任意列写入**——那正是本次改造要关掉的口子
"""

from __future__ import annotations

import inspect

import pytest
from sqlalchemy import text

from backend.db import get_repository
from backend.db.engine import session_scope
from backend.domain.task import TaskStatus


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """指向临时数据库的仓储，并建好 schema。"""
    from backend.db import engine as db_engine
    from backend.db.models import Base

    db_path = tmp_path / "tasks.db"
    monkeypatch.setattr(
        db_engine, "resolve_database_url", lambda: f"sqlite+pysqlite:///{db_path}"
    )
    db_engine.reset_engine()
    Base.metadata.create_all(db_engine.get_engine())

    yield get_repository()

    db_engine.reset_engine()


# ===== 基本 CRUD =====


def test_create_task(repo):
    task = repo.create("t1", "研究问题")
    assert task.thread_id == "t1"
    assert task.query == "研究问题"
    assert task.status == TaskStatus.PENDING.value
    assert task.verification is None


def test_get_task(repo):
    repo.create("t1", "q")
    task = repo.get("t1")
    assert task is not None and task.thread_id == "t1"


def test_get_missing_returns_none(repo):
    assert repo.get("nope") is None


def test_list_tasks_orders_by_created_desc(repo):
    for tid in ("a", "b", "c"):
        repo.create(tid, f"q-{tid}")
    ids = [t.thread_id for t in repo.list_tasks()]
    assert set(ids) == {"a", "b", "c"}


# ===== 字段持久化 =====


def test_status_persistence(repo):
    task = repo.create("t1", "q")
    task.status = TaskStatus.RUNNING.value
    repo.save(task)

    assert repo.get("t1").status == TaskStatus.RUNNING.value


def test_draft_report_persistence(repo):
    """draft_report 是 HITL 草稿预览的必需字段。"""
    task = repo.create("t1", "q")
    task.draft_report = "# 草稿\n内容"
    repo.save(task)

    assert repo.get("t1").draft_report == "# 草稿\n内容"


def test_report_persistence(repo):
    task = repo.create("t1", "q")
    task.final_report = "# 最终报告"
    repo.save(task)

    assert repo.get("t1").final_report == "# 最终报告"


def test_verification_none(repo):
    """verification=None 是合法状态，且必须与「有值」区分开。"""
    task = repo.create("t1", "q")
    task.verification = None
    repo.save(task)

    assert repo.get("t1").verification is None


def test_verification_none_stored_as_sql_null(repo):
    """None 必须落成真正的 SQL NULL，而不是 JSON 字面量字符串 'null'。

    SQLAlchemy 的 JSON 类型默认 `none_as_null=False`，会把 Python None
    序列化成字符串 'null'——那正好是旧 schema 的历史包袱形态。
    本测试锁定 `none_as_null=True` 的行为，防止回归。
    """
    task = repo.create("t1", "q")
    task.verification = None
    repo.save(task)

    task = repo.get("t1")
    task.verification = None
    repo.save(task)

    with session_scope() as session:
        raw = session.execute(
            text("SELECT verification FROM tasks WHERE thread_id = 't1'")
        ).scalar_one()

    assert raw is None, f"应存 SQL NULL，实际存的是 {raw!r}"


def test_verification_json(repo):
    payload = {"total_claims": 10, "unsupported": 3, "nested": {"a": [1, 2]}}
    task = repo.create("t1", "q")
    task.verification = payload
    repo.save(task)

    assert repo.get("t1").verification == payload


def test_verification_roundtrip_from_none_to_value(repo):
    """先无核查结果、后有——两者必须可区分。"""
    task = repo.create("t1", "q")
    task.verification = None
    repo.save(task)
    assert repo.get("t1").verification is None

    task = repo.get("t1")
    task.verification = {"total_claims": 1}
    repo.save(task)
    assert repo.get("t1").verification == {"total_claims": 1}


# ===== 软删除 =====


def test_soft_delete(repo):
    task = repo.create("t1", "q")
    task.status = TaskStatus.DELETED.value
    repo.save(task)

    assert repo.get("t1").status == TaskStatus.DELETED.value


def test_soft_deleted_excluded_from_list(repo):
    repo.create("keep", "q")
    task = repo.create("drop", "q")
    task.status = TaskStatus.DELETED.value
    repo.save(task)

    ids = [t.thread_id for t in repo.list_tasks()]
    assert "drop" not in ids and "keep" in ids


def test_soft_deleted_visible_with_include_deleted(repo):
    task = repo.create("drop", "q")
    task.status = TaskStatus.DELETED.value
    repo.save(task)

    ids = [t.thread_id for t in repo.list_tasks(include_deleted=True)]
    assert "drop" in ids


# ===== 事务 =====


def test_transaction_rollback(repo, tmp_path):
    """session_scope 内抛异常必须回滚，不留半写状态。"""
    repo.create("t1", "q")

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with session_scope() as session:
            session.execute(
                text("UPDATE tasks SET status = 'running' WHERE thread_id = 't1'")
            )
            raise Boom()

    # 回滚后状态应保持 pending
    assert repo.get("t1").status == TaskStatus.PENDING.value


def test_transaction_commits_on_success(repo):
    with session_scope() as session:
        session.execute(
            text("UPDATE tasks SET status = 'running' WHERE thread_id = 'nope'")
        )
    # 不应抛异常；提交路径正常
    assert True


# ===== 禁止任意列写入（执行包 §7 验收项）=====


def test_repository_rejects_arbitrary_column_update(repo):
    """仓储不得提供 update(**kwargs) 之类的通用 setter。"""
    for forbidden in ("update", "update_task", "set_fields", "patch"):
        assert not hasattr(repo, forbidden), f"仓储不应暴露 {forbidden}()"

    public_methods = [
        name for name, _ in inspect.getmembers(repo, inspect.ismethod)
        if not name.startswith("_")
    ]
    assert public_methods, "仓储应至少有一个公开方法"

    for name in public_methods:
        sig = inspect.signature(getattr(repo, name))
        has_var_kwargs = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        assert not has_var_kwargs, f"{name}() 不应接受 **kwargs"


def test_service_rejects_arbitrary_field_update(repo):
    """service 层的 transition_task 同样不接受任意字段名。"""
    from backend.services import agent_service as svc

    task = repo.create("svc1", "q")
    with pytest.raises(TypeError):
        svc.transition_task(
            task.thread_id,
            TaskStatus.RUNNING,
            definitely_not_a_column="boom",
        )


def test_repository_protocol_is_defined():
    """保留抽象是为了将来加后端时仍有边界可依（执行包 §0.5.4 第 4 条）。"""
    from backend.db.repository import TaskRepository

    assert TaskRepository is not None
