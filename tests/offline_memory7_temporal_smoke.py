"""Real SQLite/Alembic smoke; no Chroma, LangGraph or paid APIs required.

Tests durable completion atomicity, lease fencing, interrupted phases, retries,
append-only review decisions, and temporal intervals without assuming a live LLM.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("APP_ENV", "test")
os.environ["DR_MEMORY_OUTBOX_ENABLED"] = "on"

from sqlalchemy import event, select
from alembic.config import Config
from alembic import command

from backend.db import engine as db_engine
from backend.db.models import MemoryOutbox, MemoryTemporalAudit
from backend.db.repository import SqlAlchemyTaskRepository
from backend.runtime.memory_outbox import MemoryOutboxStore, MemoryOutboxProcessor, LeaseLost
from backend.db.schema import schema_status
from deep_research.memory.schemas import MemoryClaim, Contradiction
from deep_research.memory.temporal import TemporalReviewLedger, interval_relation

ROOT = Path(__file__).resolve().parents[1]
passes = 0


def check(label, test):
    global passes
    test()
    passes += 1
    print(f"PASS {passes:02d} {label}")


def task(repo, tid, report=None, *, finish=False):
    obj = repo.create(tid, f"用户研究任务 {tid}")
    obj.status = "completed" if finish else "running"
    if report:
        obj.final_report = report
    repo.save(obj)
    return obj


def run():
    with tempfile.TemporaryDirectory(prefix="memory7_smoke_") as td:
        db_path = Path(td) / "tasks.sqlite3"
        # Validate the actual alembic upgrade path from an empty database.
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path}")
        command.upgrade(cfg, "head")
        old_resolve = db_engine.resolve_database_url
        db_engine.resolve_database_url = lambda: f"sqlite+pysqlite:///{db_path}"
        db_engine.reset_engine()
        try:
            check("Alembic migration revision and all new tables", lambda: (
                (_ for _ in ()).throw(AssertionError()) if schema_status(db_engine.get_engine())[0] !=
                "0003_memory_reliability" else None
            ))
            repo = SqlAlchemyTaskRepository()
            store = MemoryOutboxStore()

            def atomically_enqueue():
                row = task(repo, "task01")
                row.status = "completed"
                row.final_report = "长报告有效结果与事实" * 30
                trace = [{"generation": 1, "topic": "拒绝的投机研究", "queries": ["ignored"]},
                         {"generation": 2, "topic": "新能源汽车电池研究", "queries": ["电池寿命"]}]
                repo.save_completed_with_outbox(row, research_trace=trace, research_generation=2)
                with db_engine.session_scope() as s:
                    jobs = s.scalars(select(MemoryOutbox)).all()
                    assert len(jobs) == 1
                    assert jobs[0].status == "pending"
                    assert jobs[0].episode_payload[0]["topic"] == "新能源汽车电池研究"
                    assert len(jobs[0].episode_payload) == 1
                    assert jobs[0].report_sha256 == hashlib.sha256(row.final_report.encode()).hexdigest()
                    assert s.get(type(row), "task01").status == "completed"
            check("Completion and sanitized outbox enqueue are atomic", atomically_enqueue)

            def idempotent():
                row = repo.get("task01")
                repo.save_completed_with_outbox(row)
                with db_engine.session_scope() as s:
                    assert len(s.scalars(select(MemoryOutbox)).all()) == 1
                    assert s.scalars(select(MemoryOutbox)).one().attempts == 0
            check("Repeated settlement never duplicates the outbox", idempotent)

            def rollback():
                row = task(repo, "task02")
                row.status = "completed"
                row.final_report = "回滚证明" * 50
                def raise_on_insert(*args):
                    raise OSError("simulated SQLite write failure")
                event.listen(MemoryOutbox, "before_insert", raise_on_insert)
                try:
                    try:
                        repo.save_completed_with_outbox(row)
                        assert False, "expected insert failure"
                    except OSError:
                        pass
                finally:
                    event.remove(MemoryOutbox, "before_insert", raise_on_insert)
                assert repo.get("task02").status == "running"
                with db_engine.session_scope() as s:
                    assert s.query(MemoryOutbox).filter_by(task_id="task02").count() == 0
            check("Queue write failure rolls back task completion", rollback)

            def fenced():
                first = store.claim("worker-A", lease_seconds=30)
                assert first and first.task_id == "task01"
                assert store.claim("worker-B", lease_seconds=30) is None
                # Simulate a process death, time jumps past the lease's end.
                second = store.claim("worker-B", now=__import__('time').time() + 50, lease_seconds=120)
                assert second and second.job_id == first.job_id and second.token != first.token
                try:
                    store.report_finished(first, "stale")
                    assert False, "stale worker unexpectedly won"
                except LeaseLost:
                    pass
                store.report_finished(second, "doc")
                store.episodes_finished(second)
                store.complete(second)
                assert store.stats().get("done") == 1
            check("Lease reclaim and stale-owner fencing", fenced)

            def stage_retry():
                row = repo.get("task02")
                row.status = "completed"
                row.final_report = "失败后重试的结构化报告" * 30
                repo.save_completed_with_outbox(
                    row, research_trace=[{"generation": 0, "topic": "科研方法复盘"}],
                )
                reports, episodes = [], []
                def write_report(q, r):
                    reports.append(r)
                    return "report-indexed"
                def write_episode(payload):
                    episodes.append(payload)
                    if len(episodes) == 1:
                        raise OSError("episode DB is temporarily locked")
                    return 1
                proc = MemoryOutboxProcessor("worker", store=store,
                                             report_writer=write_report,
                                             episode_writer=write_episode)
                assert asyncio.run(proc.process_one())
                assert len(reports) == 1 and len(episodes) == 1
                with db_engine.session_scope() as s:
                    job = s.query(MemoryOutbox).filter_by(task_id="task02").one()
                    assert job.report_done and not job.episodes_done and job.status == "retry"
                    assert job.next_attempt_at > 0
                    job.next_attempt_at = 0
                assert asyncio.run(proc.process_one())
                assert len(reports) == 1 and len(episodes) == 2
                assert store.stats().get("done") == 2
            check("Episode failure resumes episode phase without extracting report again", stage_retry)

            def no_crosstalk():
                row = task(repo, "task03")
                row.status = "completed"
                row.final_report = "不可混入别的研究报告" * 50
                repo.save_completed_with_outbox(row)
                # Simulate a post-completion data revision/deletion by an operator.
                row.final_report = "完全另一篇报告" * 80
                repo.save(row)
                proc = MemoryOutboxProcessor("worker", store=store,
                    report_writer=lambda *_: (_ for _ in ()).throw(AssertionError("must not be called")),
                    episode_writer=lambda *_: 0)
                assert asyncio.run(proc.process_one())
                with db_engine.session_scope() as s:
                    assert s.query(MemoryOutbox).filter_by(task_id="task03").one().status == "retry"
            check("Fingerprint prevents stale report enrichment", no_crosstalk)

            def dead_and_requeue():
                with db_engine.session_scope() as s:
                    job = s.query(MemoryOutbox).filter_by(task_id="task03").one()
                    job.next_attempt_at = 0
                    job.attempts = 5
                current = store.claim("worker-C")
                assert current.attempts == 6
                assert store.fail(current, RuntimeError("simulated")) == "dead"
                assert store.retry_dead(current.job_id)
                resumed = store.claim("worker-C")
                assert resumed is not None
                store.report_finished(resumed, "manually-checked")
                store.episodes_finished(resumed)
                store.complete(resumed)
            check("Dead-letter and explicit operator replay", dead_and_requeue)

            def backfill():
                assert repo.enqueue_existing_completed("task01") is False
                row = task(repo, "old_completed", "历史长报告" * 40, finish=True)
                assert repo.enqueue_existing_completed(row.thread_id)
                assert not repo.enqueue_existing_completed(row.thread_id)
                assert repo.enqueue_existing_completed("not_exists") is False
            check("Legacy completed task backfill is explicit and idempotent", backfill)

            def serial_chroma_writer():
                task(repo, "parallel-second", "并发第二项报告" * 30, finish=True)
                assert repo.enqueue_existing_completed("parallel-second")
                # Multiple pending jobs may exist, but only one Chroma writer
                # can have a valid lease across all research-worker processes.
                a = store.claim("memory-A", lease_seconds=30)
                assert a is not None
                b = store.claim("memory-B", lease_seconds=30)
                assert b is None, "a second job must not be concurrently leased"
                store.report_finished(a, "indexed")
                store.episodes_finished(a)
                store.complete(a)
                assert store.claim("memory-B", lease_seconds=30) is not None
                # Lease will naturally recover if the simulated process exits.
            check("All workers share one fenced Chroma write lease", serial_chroma_writer)

            def rollout_off_pauses_processing():
                previous=os.environ.get("DR_MEMORY_OUTBOX_ENABLED")
                os.environ["DR_MEMORY_OUTBOX_ENABLED"]="off"
                try:
                    proc=MemoryOutboxProcessor("paused-worker", store=store)
                    assert asyncio.run(proc.process_one()) is False
                finally:
                    if previous is None: os.environ.pop("DR_MEMORY_OUTBOX_ENABLED", None)
                    else: os.environ["DR_MEMORY_OUTBOX_ENABLED"]=previous
            check("Rollback switch pauses embedded and dedicated outbox processors", rollout_off_pauses_processing)

            def bad_optional_trace_does_not_block_completion():
                row = task(repo, "invalid_trace")
                row.status = "completed"
                row.final_report = "真实任务不能因为坏的研究统计数据失败" * 20
                repo.save_completed_with_outbox(row, research_trace=[{
                    "generation": 0, "topic": "统计出错的历史", "search_calls": "garbage",
                }])
                with db_engine.session_scope() as s:
                    j = s.query(MemoryOutbox).filter_by(task_id="invalid_trace").one()
                    assert j.episode_payload == []
                    assert s.get(type(row), "invalid_trace").status == "completed"
            check("Malformed optional episode trace cannot block report settlement", bad_optional_trace_does_not_block_completion)

            def incomplete_report_never_acknowledged():
                from backend.runtime.memory_outbox import IncompleteMemory
                from backend.runtime.memory_outbox import MemoryOutboxProcessor
                from deep_research.memory import runtime as memory_runtime
                class Manager:
                    def store_from_report(self, query, text): return "doc-pending"
                    def is_report_complete(self, doc): return False
                original = memory_runtime.get_memory_manager
                memory_runtime.get_memory_manager = lambda: Manager()
                try:
                    try:
                        MemoryOutboxProcessor._write_report("query", "正文"*90)
                        assert False, "pending structured memory should be retried"
                    except IncompleteMemory:
                        pass
                finally:
                    memory_runtime.get_memory_manager = original
            check("A pending Chroma section cannot be acknowledged as complete", incomplete_report_never_acknowledged)

            def runner_atomic_wiring():
                from datetime import datetime
                from backend.domain.task import TaskStatus
                # Execute the REAL async method, without needing LangGraph or
                # Redis in the offline harness.
                tree = ast.parse((ROOT / "backend/runtime/runner.py").read_text("utf-8"))
                klass = next(x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == "TaskRunner")
                func = next(x for x in klass.body if isinstance(x, ast.AsyncFunctionDef)
                            and x.name == "_finish_completed")
                import __future__
                events = []
                async def publish(_redis, tid, kind, payload, **kw):
                    with db_engine.session_scope() as s:
                        assert s.get(type(obj), tid).status == "completed"
                        assert s.query(MemoryOutbox).filter_by(task_id=tid).count() == 1
                    events.append((tid, kind))
                scope = dict(
                    TaskStatus=TaskStatus, datetime=datetime,
                    ev=SimpleNamespace(publish=publish),
                    EventType=SimpleNamespace(TASK_COMPLETED="task.completed"),
                    RunOutcome=lambda status: SimpleNamespace(status=status),
                )
                exec(compile(ast.fix_missing_locations(ast.Module(body=[func], type_ignores=[])),
                             str(ROOT / "backend/runtime/runner.py"), "exec",
                             flags=__future__.annotations.compiler_flag), scope)
                obj = task(repo, "runner01")
                fallback = []
                dummy = SimpleNamespace(
                    _repo=repo, _redis=None, _worker_id="runner-test",
                    _settings=SimpleNamespace(event_retention_maxlen=1000),
                    _abandon_if_ownership_lost=lambda name: None,
                    _run_post_completion=lambda *a, **k: fallback.append(True),
                )
                result = asyncio.run(scope["_finish_completed"](
                    dummy, "runner01", "正式完成的研究报告" * 60, None,
                    user_query="主题", research_trace=[{"generation":0,"topic":"真实子任务"}],
                ))
                assert result.status == "completed"
                assert events == [("runner01", "task.completed")]
                assert not fallback, "durable path must not trigger legacy best-effort enrichment"
            check("Runner publishes completion AFTER durable enqueue in same transaction", runner_atomic_wiring)

            def failed_commit_has_no_completion_event():
                import __future__
                from datetime import datetime
                from backend.domain.task import TaskStatus
                tree = ast.parse((ROOT / "backend/runtime/runner.py").read_text("utf-8"))
                klass = next(x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == "TaskRunner")
                fn = next(x for x in klass.body if isinstance(x, ast.AsyncFunctionDef) and x.name == "_finish_completed")
                notified = []
                async def publish(*a, **k):
                    notified.append(True)
                namespace = dict(
                    TaskStatus=TaskStatus, datetime=datetime, ev=SimpleNamespace(publish=publish),
                    EventType=SimpleNamespace(TASK_COMPLETED="task.completed"),
                    RunOutcome=lambda status: SimpleNamespace(status=status),
                )
                exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                             "backend/runtime/runner.py", "exec",
                             flags=__future__.annotations.compiler_flag), namespace)
                row = task(repo, "runner_fail")
                original_save = repo.save_completed_with_outbox
                def fail_save(*a, **k):
                    raise OSError("database is locked")
                repo.save_completed_with_outbox = fail_save
                dummy = SimpleNamespace(
                    _repo=repo, _redis=None, _worker_id="worker",
                    _settings=SimpleNamespace(event_retention_maxlen=99),
                    _abandon_if_ownership_lost=lambda _: None,
                )
                try:
                    try:
                        asyncio.run(namespace["_finish_completed"](
                            dummy, "runner_fail", "事务失败的长报告" * 30, None,
                        ))
                        assert False
                    except Exception as exc:
                        from backend.runtime.memory_outbox import MemoryOutboxEnqueueError
                        assert isinstance(exc, MemoryOutboxEnqueueError)
                    assert not notified
                    assert repo.get("runner_fail").status == "running"
                finally:
                    repo.save_completed_with_outbox = original_save
            check("Atomic enqueue failure publishes no completion event", failed_commit_has_no_completion_event)

            def stale_worker_does_not_ack():
                import __future__
                from backend.runtime.memory_outbox import MemoryOutboxEnqueueError
                tree = ast.parse((ROOT / "backend/worker.py").read_text("utf-8"))
                klass = next(x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == "Worker")
                fn = next(x for x in klass.body if isinstance(x, ast.AsyncFunctionDef) and x.name == "_handle")
                events=[]
                class FakeRunner:
                    def __init__(self, **kwargs): self.ownership_lost=False
                    async def run(self, tid):
                        raise MemoryOutboxEnqueueError("injected atomic enqueue failure")
                async def acquire(*a): return SimpleNamespace(acquired=True)
                async def release(*a): events.append("release")
                async def ack(*a): events.append("ACK")
                async def noop(*a, **k): pass
                namespace = dict(
                    q=SimpleNamespace(ack=ack), claim_mod=SimpleNamespace(acquire=acquire,release=release),
                    TaskRunner=FakeRunner, logger=SimpleNamespace(error=noop,warning=noop,info=noop),
                )
                # Loggers in the real source are synchronous.
                namespace["logger"] = SimpleNamespace(error=lambda *a,**k:None,
                    warning=lambda *a,**k:None, info=lambda *a,**k:None)
                exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),
                    "backend/worker.py","exec",flags=__future__.annotations.compiler_flag),namespace)
                instance=SimpleNamespace(
                    _redis=object(), _current=None, worker_id="w1", _checkpointer=None,
                    settings=SimpleNamespace(claim_ttl_ms=10000), _post_completion=None,
                    _repository=lambda: repo, _claimed_by_us=lambda *a: asyncio.sleep(0, result=True),
                    _record_reliability=noop,
                )
                asyncio.run(namespace["_handle"](instance,SimpleNamespace(thread_id="fake", message_id="m")))
                assert events == ["release"], events
            check("Failed outbox commit releases claim but retains Redis PEL job", stale_worker_does_not_ack)

            def checkpoint_does_not_research_again():
                import __future__, sys, types
                tree = ast.parse((ROOT / "backend/runtime/runner.py").read_text("utf-8"))
                klass = next(x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == "TaskRunner")
                func = next(x for x in klass.body if isinstance(x, ast.AsyncFunctionDef) and x.name == "_execute")
                namespace = dict(logger=SimpleNamespace(info=lambda *a:None))
                exec(compile(ast.fix_missing_locations(ast.Module(body=[func],type_ignores=[])),
                             "backend/runtime/runner.py", "exec",
                             flags=__future__.annotations.compiler_flag),namespace)
                calls=[]
                class Graph:
                    async def aget_state(self, cfg):
                        return SimpleNamespace(next=(), values={"final_report":"already written"})
                class Builder:
                    def compile(self, **k): return Graph()
                fake=types.ModuleType("deep_research.agent_builder")
                fake._create_builder=lambda **kw: Builder()
                previous=sys.modules.get("deep_research.agent_builder")
                sys.modules["deep_research.agent_builder"]=fake
                async def finalize(*a):
                    calls.append("settle")
                    return SimpleNamespace(status="completed")
                try:
                    instance=SimpleNamespace(
                        _lineage_is_speculative=lambda *a: asyncio.sleep(0,result=None),
                        _checkpointer=object(), _collector=None, _settle=finalize,
                    )
                    out=asyncio.run(namespace["_execute"](instance,"runner01",object()))
                    assert out.status == "completed" and calls == ["settle"]
                finally:
                    if previous is None: sys.modules.pop("deep_research.agent_builder", None)
                    else: sys.modules["deep_research.agent_builder"]=previous
            check("Checkpoint final report resumes settlement without rerunning graph", checkpoint_does_not_research_again)

            base = dict(entities=["entity-1"], source_url="https://docs.example.com/source")
            older = MemoryClaim(id="c-old", text="2024的每股收益为25", report_id="r-old",
                                valid_from="2024-01-01", valid_to="2024-12-31", **base)
            newer = MemoryClaim(id="c-new", text="2025的每股收益为28", report_id="r-new",
                                valid_from="2025-01-01", valid_to="2025-12-31", **base)
            relation = Contradiction(id="rel01", claim_a_id="c-old", claim_b_id="c-new",
                                     relation_type="possible_change")
            ledger = TemporalReviewLedger()
            def intervals():
                assert interval_relation(older, newer) == "disjoint"
                assert interval_relation(MemoryClaim(id="unknown", text="?"), older) == "unknown"
                conflict = older.model_copy(update={"valid_from":"2025-01-01", "valid_to":"2025-03-01"})
                assert interval_relation(conflict, newer) == "overlap"
                invalid = older.model_copy(update={"valid_from":"2026-01-01"})
                assert interval_relation(invalid, newer) == "invalid"
            check("Temporal ranges distinguish unknown, nonoverlapping and invalid", intervals)

            def paged_history_and_iso_dates():
                assert interval_relation(older, newer) == "disjoint"
                # SQLite list bind limit and empty records must be handled.
                refs = {f"unrelated-{i:04d}" for i in range(650)} | {"c-old"}
                assert ledger.for_claims(refs) == []
                compact=older.model_copy(update={"valid_from":"20240101", "valid_to":None})
                assert interval_relation(compact, newer) == "unknown"
            check("Audit lookup pages large ID sets and rejects compact dates", paged_history_and_iso_dates)

            def unauthorized():
                for attrs in ({"reviewer":"", "rationale":"足够长的说明文字"},
                              {"reviewer":"human", "rationale":"short"}):
                    try:
                        ledger.review(relation, older, newer, decision="confirmed_change", **attrs)
                        assert False
                    except ValueError:
                        pass
                no_url = newer.model_copy(update={"source_url":None})
                try:
                    ledger.review(relation, older, no_url, decision="confirmed_change",
                                  reviewer="human", rationale="已逐条核对两个来源文档，确认有效日期不同")
                    assert False
                except ValueError:
                    pass
                try:
                    ledger.review(relation, older, newer, decision="confirmed_conflict",
                                  reviewer="human", rationale="这些日期并没有重叠，所以不能确认冲突")
                    assert False
                except ValueError:
                    pass
            check("Review rejects absent actor, missing source and false conflicts", unauthorized)

            def confirmed():
                args=dict(decision="confirmed_change", reviewer="analyst-1",
                          rationale="已核对两个官方资料有效区间，结论确实随时间发生变更")
                decided = ledger.review(relation, older, newer, **args)
                assert decided.older_claim_id == "c-old" and decided.newer_claim_id == "c-new"
                assert decided.decision == "confirmed_change"
                assert len(ledger.audit("rel01")) == 1
                again = ledger.review(relation, older, newer, **args)
                assert again == decided and len(ledger.audit("rel01")) == 1
                result = ledger.review(relation, older, newer, decision="dismissed",
                                       reviewer="analyst-2", rationale="审核复议确认两个数字属于不同产品而不是相同对象")
                assert result.older_claim_id is None and result.newer_claim_id is None
                audit = ledger.audit("rel01")
                assert len(audit) == 2 and audit[-1]["prior_decision"] == "confirmed_change"
                assert ledger.get("rel01").decision == "dismissed"
                assert ledger.for_claims({"c-new"})[0].decision == "dismissed"
                with db_engine.session_scope() as s:
                    assert s.query(MemoryTemporalAudit).filter_by(relation_id="rel01").count() == 2
            check("Review is audited, idempotent and reversable without stale version links", confirmed)

            def overlap_review():
                a=older.model_copy(update={"valid_from":"2025-01-01", "valid_to":"2025-06-30"})
                r=ledger.review(relation, a, newer, decision="confirmed_conflict",
                                reviewer="analyst-3", rationale="证据确实适用于同一重叠期间且计量口径一致")
                assert r.decision == "confirmed_conflict"
                assert len(ledger.audit("rel01")) == 3
                assert ledger.audit("rel01")[-1]["prior_decision"] == "dismissed"
            check("Explicit overlapping intervals may be reviewed as conflict", overlap_review)

            def source_consistency():
                cross=relation.model_copy(update={"claim_b_id":"unrelated"})
                try:
                    ledger.review(cross, older, newer, decision="dismissed", reviewer="analyst",
                                  rationale="此关系其实指向另一对不同的事实声明")
                    assert False
                except ValueError:
                    pass
            check("Audit cannot be silently reassigned to different Claims", source_consistency)

            def review_overlay():
                import __future__
                tree=ast.parse((ROOT / "deep_research/memory/manager.py").read_text("utf-8"))
                klass=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name == "MemoryManager")
                func=next(x for x in klass.body if isinstance(x,ast.FunctionDef) and x.name == "get_contradictions")
                namespace={"logger":SimpleNamespace(warning=lambda *a:None)}
                exec(compile(ast.fix_missing_locations(ast.Module(body=[func],type_ignores=[])),
                             "deep_research/memory/manager.py","exec",
                             flags=__future__.annotations.compiler_flag),namespace)
                stub=SimpleNamespace(_structured=SimpleNamespace(get_contradictions=lambda:[relation.model_copy()]))
                overlay=namespace["get_contradictions"](stub)
                assert overlay[0].review_status == "confirmed"
                assert "重叠" in overlay[0].resolution or "计量口径" in overlay[0].resolution
            check("Readers see authoritative SQLite temporal decisions over stale Chroma candidates", review_overlay)

            print(f"MEMORY7+TEMPORAL OFFLINE: {passes} PASSED, 0 FAILED (actual SQLite/Alembic, no Chroma E2E)")
        finally:
            db_engine.reset_engine()
            db_engine.resolve_database_url = old_resolve

if __name__ == "__main__":
    run()
