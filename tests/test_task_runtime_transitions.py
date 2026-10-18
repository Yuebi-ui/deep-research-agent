"""Task state changes through the current domain policy and repository."""
import pytest
from backend.domain.task import TaskStatus, InvalidTaskTransition, ensure_transition, status_for_stage
from backend.db.repository import SqlAlchemyTaskRepository
from backend.db.models import Base

@pytest.fixture
def repo(tmp_path, monkeypatch):
    from backend.db import engine as db_engine
    url = f"sqlite+pysqlite:///{tmp_path / 'tasks.db'}"
    monkeypatch.setattr(db_engine, "resolve_database_url", lambda: url)
    db_engine.reset_engine()
    Base.metadata.create_all(db_engine.get_engine())
    yield SqlAlchemyTaskRepository()
    db_engine.reset_engine()


def advance(repo, task, status, stage, *, draft=None, report=None):
    ensure_transition(TaskStatus(task.status), status)
    task.status, task.stage = status.value, stage
    if draft is not None:
        task.draft_report = draft
    if report is not None:
        task.final_report = report
    return repo.save(task)


def test_current_stateful_lifecycle(repo):
    t = repo.create("t1", "research", user_id="alice")
    assert t.status == TaskStatus.PENDING.value
    t = advance(repo, t, TaskStatus.RUNNING, "write_research_brief")
    t = advance(repo, t, TaskStatus.WAITING_REVIEW, "human_review_v3", draft="# Draft")
    assert repo.get("t1").draft_report == "# Draft"
    t = advance(repo, t, TaskStatus.RUNNING, "claim_verification")
    t = advance(repo, t, TaskStatus.COMPLETED, "final_report_generation", report="# Report")
    assert repo.get("t1").final_report == "# Report"
    assert repo.get("t1").user_id == "alice"
    with pytest.raises(InvalidTaskTransition):
        ensure_transition(TaskStatus(t.status), TaskStatus.RUNNING)


def test_cancel_delete_and_idempotency(repo):
    t = repo.create("t2", "query")
    ensure_transition(TaskStatus(t.status), TaskStatus.CANCELLED)
    ensure_transition(TaskStatus.CANCELLED, TaskStatus.CANCELLED)
    ensure_transition(TaskStatus.CANCELLED, TaskStatus.DELETED)
    with pytest.raises(InvalidTaskTransition):
        ensure_transition(TaskStatus.DELETED, TaskStatus.RUNNING)
    assert status_for_stage("human_review_v3") is TaskStatus.WAITING_REVIEW


def test_task_history_owner_filter(repo):
    repo.create("alice-a", "a", user_id="alice")
    repo.create("bob-b", "b", user_id="bob")
    assert [t.thread_id for t in repo.list_tasks(user_id="alice")] == ["alice-a"]
    assert [t.thread_id for t in repo.list_tasks(user_id="bob")] == ["bob-b"]
