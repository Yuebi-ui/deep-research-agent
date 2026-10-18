"""Tenant isolation contract: API owner, memory paths, Outbox and temporal ledger."""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.core.auth import authenticated_user, validate_auth_config
from deep_research.memory.tenant import (
    current_user_id, memory_user_scope, tenant_memory_dir,
)


@pytest.fixture
def accounts(monkeypatch):
    tokens = {"alice": "alice-" + "A" * 44, "bob": "bob-" + "B" * 44}
    monkeypatch.setenv("DR_API_USERS_JSON", json.dumps(tokens))
    validate_auth_config()
    return tokens


def test_token_authentication_and_untrusted_headers(accounts):
    assert asyncio.run(authenticated_user("Bearer " + accounts["alice"])) == "alice"
    assert asyncio.run(authenticated_user("Bearer " + accounts["bob"])) == "bob"
    for value in (None, "Bearer wrong", "Basic " + accounts["alice"], "Bearer"):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as err:
            asyncio.run(authenticated_user(value))
        assert err.value.status_code == 401


def test_fail_closed_when_multi_user_scope_missing(accounts, tmp_path):
    with pytest.raises(RuntimeError, match="tenant context required"):
        current_user_id()
    with memory_user_scope("alice"):
        assert current_user_id() == "alice"
        alice_dir = tenant_memory_dir(tmp_path)
        assert alice_dir.is_relative_to(tmp_path / "tenants")
        assert "alice" not in str(alice_dir)
    with memory_user_scope("bob"):
        assert tenant_memory_dir(tmp_path) != alice_dir
    with pytest.raises(ValueError):
        tenant_memory_dir(tmp_path, "../../etc")
    with pytest.raises(RuntimeError):
        current_user_id()


def test_async_and_thread_scope_is_preserved(accounts):
    async def exercise():
        with memory_user_scope("alice"):
            async def subtask():
                return current_user_id()
            assert await asyncio.to_thread(current_user_id) == "alice"
            assert await asyncio.create_task(subtask()) == "alice"
        with memory_user_scope("bob"):
            assert await asyncio.to_thread(current_user_id) == "bob"
    asyncio.run(exercise())


def test_memory_client_cache_is_owner_scoped(accounts, monkeypatch, tmp_path):
    import deep_research.memory.runtime as runtime
    created = []
    class FakeManager:
        def __init__(self, persist_dir):
            self.path = Path(persist_dir)
            created.append(self.path)
    fake_module = types.ModuleType("deep_research.memory.manager")
    fake_module.MemoryManager = FakeManager
    monkeypatch.setitem(sys.modules, "deep_research.memory.manager", fake_module)
    monkeypatch.setattr(runtime, "get_engine_settings", lambda: types.SimpleNamespace(
        resolved_memory_data_dir=tmp_path))
    runtime.reset_memory_runtime()
    try:
        with memory_user_scope("alice"):
            m1 = runtime.get_memory_manager()
            assert runtime.get_memory_manager() is m1
        with memory_user_scope("bob"):
            m2 = runtime.get_memory_manager()
        assert m1 is not m2 and m1.path != m2.path
        assert len(created) == 2
    finally:
        runtime.reset_memory_runtime()


def test_api_task_access_is_owner_scoped(accounts, monkeypatch, tmp_path):
    from backend.db import engine as db_engine
    from backend.db.models import Base
    from backend.db.repository import SqlAlchemyTaskRepository
    from backend.routes import research, observability
    monkeypatch.setattr(db_engine, "resolve_database_url", lambda: f"sqlite+pysqlite:///{tmp_path/'tasks.db'}")
    db_engine.reset_engine()
    try:
        Base.metadata.create_all(db_engine.get_engine())
        repo = SqlAlchemyTaskRepository()
        monkeypatch.setattr(research, "get_repository", lambda: repo)
        monkeypatch.setattr(observability, "get_repository", lambda: repo)
        async def enqueue(_thread_id):
            return None
        monkeypatch.setattr(research, "_enqueue", enqueue)
        app = FastAPI()
        app.include_router(research.router)
        app.include_router(observability.router)
        from backend.core.api_errors import register_exception_handlers
        register_exception_handlers(app)
        with TestClient(app) as client:
            alice = {"Authorization": "Bearer " + accounts["alice"]}
            bob = {"Authorization": "Bearer " + accounts["bob"]}
            assert client.post("/api/research/start", json={"query": "secret"}).status_code == 401
            created = client.post("/api/research/start", headers=alice, json={"query": "secret"})
            assert created.status_code == 200, created.text
            tid = created.json()["thread_id"]
            assert repo.get(tid).user_id == "alice"
            assert client.get("/api/research/history/list", headers=bob).json() == []
            assert client.get("/api/research/history/list", headers=alice).json()[0]["thread_id"] == tid
            for url in (f"/api/research/{tid}/status", f"/api/research/{tid}/report",
                        f"/api/research/{tid}/stream", f"/api/obs/cost/{tid}", f"/api/obs/trace/{tid}"):
                assert client.get(url, headers=bob).status_code == 404, url
            for url in (f"/api/research/{tid}/cancel", f"/api/research/{tid}/resume"):
                resp = client.post(url, headers=bob, json={"action": "approve"})
                assert resp.status_code == 404, (url, resp.text)
            assert client.delete(f"/api/research/{tid}", headers=bob).status_code == 404
            assert client.get(f"/api/research/{tid}/status", headers=alice).status_code == 200
    finally:
        db_engine.reset_engine()


def test_temporal_ledger_filters_rows_by_owner(accounts, monkeypatch, tmp_path):
    from backend.db import engine as db_engine
    from backend.db.models import Base, MemoryTemporalDecision, MemoryTemporalAudit
    from backend.db.engine import session_scope
    from deep_research.memory.temporal import TemporalReviewLedger
    monkeypatch.setattr(db_engine, "resolve_database_url", lambda: f"sqlite+pysqlite:///{tmp_path/'tasks.db'}")
    db_engine.reset_engine()
    try:
        Base.metadata.create_all(db_engine.get_engine())
        with session_scope() as session:
            session.add(MemoryTemporalDecision(relation_id="a-r", user_id="alice", claim_a_id="a1",
                claim_b_id="a2", decision="dismissed", reviewer="test", rationale="verified", reviewed_at=1.0))
            session.add(MemoryTemporalAudit(relation_id="a-r", user_id="alice",
                decision="dismissed", reviewer="test", rationale="verified", reviewed_at=1.0))
        with memory_user_scope("bob"):
            ledger = TemporalReviewLedger()
            assert ledger.get("a-r") is None
            assert ledger.for_claims({"a1"}) == []
            assert ledger.audit("a-r") == []
        with memory_user_scope("alice"):
            ledger = TemporalReviewLedger()
            assert ledger.get("a-r").reviewer == "test"
            assert len(ledger.for_claims({"a1"})) == 1
            assert len(ledger.audit("a-r")) == 1
    finally:
        db_engine.reset_engine()


def test_outbox_restores_owner_for_threaded_writes(accounts, monkeypatch):
    from backend.runtime import memory_outbox as outbox
    monkeypatch.setattr(outbox, "enabled", lambda: True)
    events = []
    class Store:
        def claim(self, owner, lease_seconds):
            if events: return None
            return outbox.ClaimedJob(job_id="j", task_id="t", report_sha256="h",
                query="q", episode_payload=[], token="claim", attempts=1,
                report_done=False, episodes_done=False, user_id="alice")
        def get_task_report(self, job):
            assert current_user_id() == "alice"
            return "document"
        def report_finished(self, job, doc_id): events.append(("report", doc_id))
        def episodes_finished(self, job): events.append(("episodes", "ok"))
        def complete(self, job): events.append(("complete", "ok"))
        def renew(self, job, lease_seconds): pass
        def fail(self, job, exc): raise AssertionError(str(exc))
    def write_report(query, report):
        assert current_user_id() == "alice"
        return "doc"
    def write_episodes(payload):
        assert current_user_id() == "alice"
        return 0
    worker = outbox.MemoryOutboxProcessor(owner="w", store=Store(), report_writer=write_report,
                                          episode_writer=write_episodes, lease_seconds=30)
    assert asyncio.run(worker.process_one()) is True
    assert [x[0] for x in events] == ["report", "episodes", "complete"]


def test_episode_memory_content_is_private(accounts, monkeypatch, tmp_path):
    from deep_research.memory import runtime
    from deep_research.memory.episodes import ResearchEpisode
    monkeypatch.setattr(runtime, "get_engine_settings", lambda: types.SimpleNamespace(
        resolved_memory_data_dir=tmp_path))
    runtime.reset_memory_runtime()
    try:
        with memory_user_scope("alice"):
            a = runtime.get_episode_store()
            a.upsert([ResearchEpisode(
                episode_id="private-a", task_id="job-a", generation=0,
                topic="Exclusive mountain research", queries=["Mountain research"],
            )])
            assert a.count() == 1
        with memory_user_scope("bob"):
            b = runtime.get_episode_store()
            assert b.path != a.path
            assert b.count() == 0
            assert b.search("Exclusive mountain research") == []
        with memory_user_scope("alice"):
            assert runtime.get_episode_store().search("Exclusive mountain research")[0].episode_id == "private-a"
    finally:
        runtime.reset_memory_runtime()


def test_cache_is_scoped_to_owner_and_memory_root(accounts, monkeypatch, tmp_path):
    import deep_research.memory.runtime as runtime
    roots = [tmp_path / "experiment-A", tmp_path / "experiment-B"]
    active = [roots[0]]
    created = []
    class FakeManager:
        def __init__(self, persist_dir):
            self.path = Path(persist_dir)
            created.append(self.path)
    monkeypatch.setitem(sys.modules, "deep_research.memory.manager",
                       types.SimpleNamespace(MemoryManager=FakeManager))
    monkeypatch.setattr(runtime, "get_engine_settings", lambda: types.SimpleNamespace(
        resolved_memory_data_dir=active[0]))
    runtime.reset_memory_runtime()
    try:
        with memory_user_scope("alice"):
            a = runtime.get_memory_manager()
            active[0] = roots[1]
            b = runtime.get_memory_manager()
            assert a is not b
            assert a.path != b.path
            active[0] = roots[0]
            assert runtime.get_memory_manager() is a
        assert len(created) == 2
    finally:
        runtime.reset_memory_runtime()


def test_operator_must_select_valid_owner(accounts):
    from deep_research.memory.tenant import resolve_cli_account
    with pytest.raises(ValueError, match="requires --user"):
        resolve_cli_account(None)
    with pytest.raises(ValueError, match="requires --user"):
        resolve_cli_account("mallory")
    assert resolve_cli_account("alice") == "alice"


def test_backfill_filters_task_rows_by_owner(accounts, tmp_path):
    import sqlite3
    from scripts.backfill_memory_v3 import iter_tasks
    db = tmp_path / "tasks.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE tasks (thread_id TEXT, user_id TEXT, query TEXT, final_report TEXT, "
                     "status TEXT, created_at TEXT)")
        conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)", [
            ("a", "alice", "private alpha", "A" * 120, "completed", "2026-10-17"),
            ("b", "bob", "private beta", "B" * 120, "completed", "2026-10-17"),
            ("c", "alice", "incomplete", "C" * 120, "pending", "2026-10-17"),
        ])
    assert [row[0] for row in iter_tasks(db, 0, "alice")] == ["a"]
    assert [row[0] for row in iter_tasks(db, 0, "bob")] == ["b"]
