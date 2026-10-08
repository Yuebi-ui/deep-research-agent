"""P0 ownership fencing 测试（Phase 2）。

覆盖任务包要求的场景：

* 正常 heartbeat 续约
* transient Redis failure 不误杀（核实所有权后继续）
* confirmed ownership loss（他人持有 / key 消失 / lease 截止兜底）
* loss during node execution（真实图，节点中途丢失 → 图停止推进，无后台残留）
* loss before settle（终态写入护栏：stale owner 不写终态）
* stale worker 不 ACK / 不 release
* XAUTOCLAIM / recovery 接管并完成（含 HITL resume 之后再恢复）
* （既有 suite 覆盖正常 HITL interrupt/resume，本文件保证不回归）

隔离：Redis db 15 + 临时 SQLite + 临时 checkpointer；
baseline 指标写入重定向到 tmp（不污染真实 data/ 目录）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace

import pytest

from backend.core.settings import get_settings
from backend.db import engine as db_engine
from backend.db import get_repository
from backend.db.models import Base
from backend.domain.task import TaskStatus
from backend.runtime import claim as claim_mod
from backend.runtime import queue as q
from backend.runtime import runner as runner_mod
from backend.runtime.runner import RunOutcome, TaskRunner


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


# ===== fixtures =====


@pytest.fixture
async def env(tmp_path, monkeypatch):
    """隔离的 DB + Redis + sqlite checkpointer（复用 test_runtime_worker 约定）。"""
    import redis.asyncio as aioredis

    import deep_research.checkpoint as ckpt
    from deep_research.callbacks import baseline_metrics as bm

    db_path = tmp_path / "tasks.db"
    monkeypatch.setattr(db_engine, "resolve_database_url", lambda: f"sqlite+pysqlite:///{db_path}")
    db_engine.reset_engine()
    Base.metadata.create_all(db_engine.get_engine())

    ckpt_path = tmp_path / "ckpt.db"
    monkeypatch.setattr(ckpt, "resolve_sqlite_path", lambda: str(ckpt_path))
    ckpt.discard_checkpointer()
    managed = await ckpt.create_checkpointer("sqlite")

    client = aioredis.from_url("redis://localhost:6379", db=15, decode_responses=True)
    await client.flushdb()

    # 观测产物重定向到 tmp（真实 data/ 不被测试污染）
    monkeypatch.setattr(bm, "default_raw_root", lambda: tmp_path / "metrics")

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


def _runner(env, worker_id: str = "test-worker:1:abcdef01") -> TaskRunner:
    return TaskRunner(
        redis=env["redis"],
        worker_id=worker_id,
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


def claim_key_for(tid: str) -> str:
    from backend.runtime.redis import claim_key

    return claim_key(tid)


# ===== FakeHeartbeat / 注入 =====


class FakeHeartbeat:
    """可控心跳：测试显式触发 lost（替代真实时钟）。"""

    def __init__(self, *, on_lost=None, **kwargs):
        self.lost = False
        self.loss_reason: str | None = None
        self.renewals = 0
        self._on_lost = on_lost
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def force_lose(self, reason: str = "test_forced") -> None:
        if self.lost:
            return
        self.lost = True
        self.loss_reason = reason
        if self._on_lost is not None:
            self._task = asyncio.ensure_future(self._on_lost())


def _install_heartbeat_factory(monkeypatch, hearts: list[FakeHeartbeat]) -> None:
    """按创建顺序分发 FakeHeartbeat 实例。"""
    pending = list(hearts)

    def factory(*args, **kwargs):
        return pending.pop(0) if pending else FakeHeartbeat(**kwargs)

    monkeypatch.setattr(runner_mod.hb_mod, "Heartbeat", factory)


# ===== Stub graph（节点级确定性控制）=====


class StubGraph:
    def __init__(self, n_events: int, *, on_event=None, on_finish=None,
                 raise_after: int | None = None, values=None):
        self.n_events = n_events
        self.on_event = on_event
        self.on_finish = on_finish
        self.raise_after = raise_after
        self.values = values or {"final_report": "stub-report"}
        self.consumed = 0
        self.closed = False
        self.aget_calls = 0  # _execute 开头必然有一次（判定 resume）；settle 会再加一次

    async def astream_events(self, *args, **kwargs):
        try:
            for i in range(self.n_events):
                if self.raise_after is not None and i >= self.raise_after:
                    raise ValueError("stub graph exploded")
                self.consumed += 1
                if self.on_event is not None:
                    self.on_event(i)
                yield {"event": "on_chain_start", "name": "supervisor", "run_id": f"r{i}"}
        finally:
            self.closed = True
            if self.on_finish is not None:
                self.on_finish()

    async def aget_state(self, config):
        self.aget_calls += 1
        return SimpleNamespace(next=(), values=self.values)


class _FakeBuilder:
    def __init__(self, graph):
        self._graph = graph

    def compile(self, *args, **kwargs):
        return self._graph


def _install_stub_graph(monkeypatch, graph: StubGraph) -> None:
    import deep_research.agent_builder as ab

    # Phase 5B：runner 会按 lineage 显式传 speculative（新任务传 None = 按 env）
    monkeypatch.setattr(
        ab, "_create_builder", lambda with_hitl=True, speculative=None: _FakeBuilder(graph)
    )


# =====================================================================
# 1) Heartbeat 语义（真实 Redis）
# =====================================================================


@pytest.fixture
async def rtenv():
    import redis.asyncio as aioredis

    client = aioredis.from_url("redis://localhost:6379", db=15, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


async def _wait_lost(hb, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if hb.lost:
            return True
        await asyncio.sleep(0.05)
    return hb.lost


async def test_heartbeat_normal_renewal(rtenv):
    from backend.runtime.heartbeat import Heartbeat

    tid = uuid.uuid4().hex[:12]
    await claim_mod.acquire(rtenv, tid, "w1", ttl_ms=1500)

    hb = Heartbeat(rtenv, tid, "w1", interval_ms=50, ttl_ms=1500)
    await hb.start()
    try:
        await asyncio.sleep(0.35)
        assert hb.lost is False
        assert hb.renewals >= 2
        assert hb.degraded is False
        assert await claim_mod.holder(rtenv, tid) == "w1"
    finally:
        await hb.stop()


async def test_transient_renew_failure_does_not_kill(rtenv, monkeypatch):
    """单次续约异常但 claim 仍是自己的 —— 不得误杀（P0 新语义）。"""
    from backend.runtime.heartbeat import Heartbeat

    tid = uuid.uuid4().hex[:12]
    await claim_mod.acquire(rtenv, tid, "w1", ttl_ms=1500)

    real_renew = claim_mod.renew
    calls = {"n": 0}

    async def flaky(redis, thread_id, worker_id, ttl_ms):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient redis blip")
        return await real_renew(redis, thread_id, worker_id, ttl_ms)

    monkeypatch.setattr(claim_mod, "renew", flaky)

    hb = Heartbeat(rtenv, tid, "w1", interval_ms=50, ttl_ms=1500)
    await hb.start()
    try:
        await asyncio.sleep(0.4)
        assert calls["n"] >= 2, "应重试续约"
        assert hb.lost is False, "瞬时失败不得判为丢失"
        assert hb.renewals >= 1
    finally:
        await hb.stop()


async def test_claim_missing_is_confirmed_loss(rtenv, monkeypatch):
    """续约异常 + key 已消失 = 所有权不可再证明 → 丢失。"""
    from backend.runtime.heartbeat import Heartbeat

    tid = uuid.uuid4().hex[:12]
    await claim_mod.acquire(rtenv, tid, "w1", ttl_ms=3000)
    await rtenv.delete(claim_key_for(tid))  # 模拟 TTL 过期

    async def broken(redis, thread_id, worker_id, ttl_ms):
        raise RuntimeError("redis down for us")

    monkeypatch.setattr(claim_mod, "renew", broken)

    hb = Heartbeat(rtenv, tid, "w1", interval_ms=50, ttl_ms=600)
    await hb.start()
    try:
        assert await _wait_lost(hb), "key 消失后必须判定丢失"
        assert hb.loss_reason == "claim_missing"
    finally:
        await hb.stop()


async def test_other_owner_is_confirmed_loss(rtenv):
    from backend.runtime.heartbeat import Heartbeat

    tid = uuid.uuid4().hex[:12]
    await claim_mod.acquire(rtenv, tid, "w1", ttl_ms=3000)
    await rtenv.set(claim_key_for(tid), "other-worker", px=60000)

    hb = Heartbeat(rtenv, tid, "w1", interval_ms=50, ttl_ms=600)
    await hb.start()
    try:
        assert await _wait_lost(hb)
        assert hb.loss_reason == "renew_rejected_holder_other"
    finally:
        await hb.stop()


async def test_unknown_state_lost_only_after_lease_deadline(rtenv, monkeypatch):
    """续约与复核都异常：先保持执行（不误杀），超过 lease 截止才判丢失。"""
    from backend.runtime.heartbeat import Heartbeat

    tid = uuid.uuid4().hex[:12]

    async def broken_renew(redis, thread_id, worker_id, ttl_ms):
        raise RuntimeError("redis unreachable")

    async def broken_holder(redis, thread_id):
        raise RuntimeError("redis unreachable")

    monkeypatch.setattr(claim_mod, "renew", broken_renew)
    monkeypatch.setattr(claim_mod, "holder", broken_holder)

    interval_ms, ttl_ms = 50, 400
    hb = Heartbeat(rtenv, tid, "w1", interval_ms=interval_ms, ttl_ms=ttl_ms)
    await hb.start()
    try:
        await asyncio.sleep(0.15)
        assert hb.lost is False, "截止前不得误杀"
        assert await _wait_lost(hb, timeout=2.0)
        assert hb.loss_reason == "lease_deadline_exceeded"
    finally:
        await hb.stop()


async def test_on_lost_callback_receives_reason(rtenv):
    from backend.runtime.heartbeat import Heartbeat

    tid = uuid.uuid4().hex[:12]
    await claim_mod.acquire(rtenv, tid, "w1", ttl_ms=3000)
    await rtenv.delete(claim_key_for(tid))

    seen: list[str] = []

    async def on_lost():
        seen.append(hb.loss_reason or "")

    hb = Heartbeat(rtenv, tid, "w1", interval_ms=50, ttl_ms=600, on_lost=on_lost)
    await hb.start()
    try:
        assert await _wait_lost(hb)
        await asyncio.sleep(0.1)
        assert seen == ["claim_missing"]
    finally:
        await hb.stop()


# =====================================================================
# 2) Runner：丢失后停止推进 / 不写终态
# =====================================================================


async def test_loss_mid_stream_stops_and_skips_settle(env, monkeypatch):
    """流中丢失：停止消费、关闭事件流、不 settle、不写终态。"""
    tid = _new_task(env)
    hb = FakeHeartbeat()
    _install_heartbeat_factory(monkeypatch, [hb])
    graph = StubGraph(10, on_event=lambda i: hb.force_lose() if i == 1 else None)
    _install_stub_graph(monkeypatch, graph)

    outcome = await _runner(env).run(tid)

    assert graph.consumed <= 3, "丢失后不应继续消费事件"
    assert graph.closed is True, "事件流必须被显式关闭（停止图任务）"
    assert graph.aget_calls == 1, "除 _execute 开头的判定外不得再调用（即未进入 settle）"
    assert outcome.retryable is True
    assert outcome.status == TaskStatus.RUNNING.value
    assert _status(env, tid) != TaskStatus.COMPLETED.value


async def test_loss_before_settle_blocks_terminal_write(env, monkeypatch):
    """流正常结束但丢失发生在 settle 前：终态护栏必须拦住写入。"""
    tid = _new_task(env)
    hb = FakeHeartbeat()
    _install_heartbeat_factory(monkeypatch, [hb])
    graph = StubGraph(1, on_finish=hb.force_lose)  # 流结束瞬间失去所有权
    _install_stub_graph(monkeypatch, graph)

    outcome = await _runner(env).run(tid)

    assert graph.aget_calls == 1, "settle 之前必须先检查所有权（不得进入 settle 的 aget_state）"
    assert outcome.retryable is True
    assert _status(env, tid) != TaskStatus.COMPLETED.value


async def test_loss_during_failure_handling_does_not_write_failed(env, monkeypatch):
    """图异常 + 同时丢失所有权：不得写 FAILED（同样属于终态）。"""
    tid = _new_task(env)
    hb = FakeHeartbeat()
    _install_heartbeat_factory(monkeypatch, [hb])
    graph = StubGraph(2, raise_after=1, on_event=lambda i: hb.force_lose() if i == 0 else None)
    _install_stub_graph(monkeypatch, graph)

    outcome = await _runner(env).run(tid)

    assert outcome.retryable is True
    assert _status(env, tid) != TaskStatus.FAILED.value


async def test_normal_stub_run_settles_completed(env, monkeypatch):
    """反例基线：未丢失时正常 settle 到 completed。"""
    tid = _new_task(env)
    hb = FakeHeartbeat()
    _install_heartbeat_factory(monkeypatch, [hb])
    graph = StubGraph(1)
    _install_stub_graph(monkeypatch, graph)

    outcome = await _runner(env).run(tid)

    assert outcome.status == TaskStatus.COMPLETED.value
    assert graph.aget_calls == 2, "未丢失时应正常进入 settle"
    assert _status(env, tid) == TaskStatus.COMPLETED.value


async def test_real_graph_stops_after_ownership_loss(env, monkeypatch):
    """真实图（Fake 模型）中途丢失：节点迭代必须冻结，无后台残留。"""
    tid = _new_task(env)
    import deep_research.agent_builder as ab
    from langchain_core.messages import HumanMessage
    from langgraph.types import Command

    from deep_research.testing.fake_llm import make_fake_chat_model

    m = make_fake_chat_model("writer")
    progress = {"n": 0}

    async def slow_brief(state):
        for i in range(40):
            progress["n"] = i + 1
            await m.ainvoke([HumanMessage(content=f"step {i}")])  # 产生真实事件流
            await asyncio.sleep(0.02)
        return Command(goto="write_draft_report", update={"research_brief": "x"})

    monkeypatch.setattr(ab, "write_research_brief", slow_brief)

    hb = FakeHeartbeat()
    _install_heartbeat_factory(monkeypatch, [hb])

    async def trigger_soon():
        await asyncio.sleep(0.3)
        hb.force_lose("test_forced")

    asyncio.ensure_future(trigger_soon())

    outcome = await _runner(env).run(tid)
    n_at_return = progress["n"]
    await asyncio.sleep(0.5)

    assert progress["n"] == n_at_return, "丢失后图不得继续推进（后台残留）"
    assert n_at_return < 40, "必须在节点中途停止（而非自然跑完）"
    assert outcome.retryable is True
    assert _status(env, tid) != TaskStatus.COMPLETED.value


# =====================================================================
# 3) Worker：stale owner 不 ACK / 不 release；恢复接管
# =====================================================================


class StubRunner:
    """worker._handle 用的假 TaskRunner。"""

    ownership_lost = False
    before_return = None  # type: ignore[assignment]

    def __init__(self, **kwargs):
        pass

    async def run(self, thread_id: str) -> RunOutcome:
        if self.before_return is not None:
            await self.before_return(thread_id)
        return RunOutcome(
            TaskStatus.RUNNING.value if self.ownership_lost else TaskStatus.COMPLETED.value,
            "stub", retryable=self.ownership_lost,
        )


def _worker(env, worker_id: str):
    from backend.worker import Worker

    w = Worker()
    w._redis = env["redis"]
    w.worker_id = worker_id
    w._checkpointer = env["checkpointer"]
    return w


def _job(tid: str) -> q.Job:
    return q.Job(job_id="j1", thread_id=tid, operation="run", attempt=0,
                 created_at=time.time(), message_id="1-1")


def _patch_side_effects(env, monkeypatch):
    calls = {"ack": 0, "release": 0, "reliability": []}

    async def _ack(redis, job):
        calls["ack"] += 1

    async def _release(redis, tid, wid):
        calls["release"] += 1
        return True

    monkeypatch.setattr(q, "ack", _ack)
    monkeypatch.setattr(claim_mod, "release", _release)

    from deep_research.callbacks import baseline_metrics as bm

    monkeypatch.setattr(
        bm, "append_reliability_event",
        lambda run_id, task_id, event, **details: calls["reliability"].append((event, details)),
    )
    return calls


async def test_stale_worker_skips_ack_and_release(env, monkeypatch):
    tid = _new_task(env)
    calls = _patch_side_effects(env, monkeypatch)

    class Lost(StubRunner):
        ownership_lost = True

    monkeypatch.setattr("backend.worker.TaskRunner", Lost)

    await _worker(env, "w1")._handle(_job(tid))

    assert calls["ack"] == 0, "stale owner 不得 ACK"
    assert calls["release"] == 0, "stale owner 不得 release"
    assert any(e[0] == "stale_execution_abandoned" for e in calls["reliability"])


async def test_normal_worker_acks_and_releases(env, monkeypatch):
    tid = _new_task(env)
    calls = _patch_side_effects(env, monkeypatch)

    class Ok(StubRunner):
        ownership_lost = False

    monkeypatch.setattr("backend.worker.TaskRunner", Ok)

    await _worker(env, "w1")._handle(_job(tid))

    assert calls["ack"] == 1
    assert calls["release"] == 1
    assert calls["reliability"] == []


async def test_pre_ack_verify_blocks_ack_when_claim_stolen(env, monkeypatch):
    """runner 未报告丢失，但 ACK 前核验发现 claim 已易主 → 不 ACK。"""
    tid = _new_task(env)
    calls = _patch_side_effects(env, monkeypatch)
    redis = env["redis"]

    async def steal(thread_id):
        await redis.set(claim_key_for(thread_id), "intruder", px=60000)

    class OkButStolen(StubRunner):
        ownership_lost = False
        before_return = staticmethod(steal)

    monkeypatch.setattr("backend.worker.TaskRunner", OkButStolen)

    await _worker(env, "w1")._handle(_job(tid))

    assert calls["ack"] == 0
    assert calls["release"] == 0
    assert any(
        e[0] == "stale_execution_abandoned" and e[1].get("reason") == "pre_ack_verify"
        for e in calls["reliability"]
    )


# =====================================================================
# 4) 事件循环饥荒回归（P0 根因修复的守护测试）
# =====================================================================


async def test_claim_verification_does_not_starve_event_loop():
    """verification 阶段的同步 LLM/搜索若跑在事件循环上，会饿死 heartbeat。

    Phase 2 实测：3/3 次运行在 verification 洪峰后丢失 claim
    （心跳被阻塞 > claim TTL）。修复（to_thread / ainvoke）后，
    即使每条 claim 的搜索与判断都是慢调用，循环也必须保持可调度。
    """
    from deep_research.tools import tool as dr_tools
    from deep_research.verification.claim_verifier import ClaimVerifier

    verifier = ClaimVerifier()
    verifier._model.delay_seconds = 0.2  # judge：每次 0.2s（同步 sleep 模拟慢推理）
    old_delay = dr_tools.summarization_model.delay_seconds
    dr_tools.summarization_model.delay_seconds = 0.2  # 网页总结：每次 0.2s

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.02)

    task = asyncio.create_task(ticker())
    try:
        verdicts = await verifier.verify(
            ["断言一 about LangGraph", "断言二", "断言三", "断言四"]
        )
    finally:
        task.cancel()
        dr_tools.summarization_model.delay_seconds = old_delay

    assert len(verdicts) == 4
    assert ticks >= 8, (
        f"事件循环被同步调用饿死（ticks={ticks}）—— 真实环境下 heartbeat "
        "会因此丢失 claim"
    )


# ===== 恢复接管（integration，真实 TaskRunner + 真实图）=====


class _TriggerModel:
    """包装 compress_model：首次调用时触发所有权丢失，再委托真实模型。"""

    def __init__(self, inner, trigger):
        self._inner = inner
        self._trigger = trigger

    def invoke(self, messages, *args, **kwargs):
        self._trigger()
        return self._inner.invoke(messages, *args, **kwargs)


async def test_recovery_after_abandoned_execution_completes(env, monkeypatch):
    """完整时间线：HITL resume → 研究阶段丢失所有权 → 中止且不 ACK →
    XAUTOCLAIM 接管 → 新 owner 从 checkpoint 恢复 → completed。"""
    import deep_research.agents.research_agent as ra
    from deep_research.callbacks import baseline_metrics as bm

    tid = _new_task(env)

    # --- 第一次执行：正常跑到 HITL ---
    _install_heartbeat_factory(monkeypatch, [FakeHeartbeat()])
    await _runner(env, "w0").run(tid)
    assert _status(env, tid) == TaskStatus.WAITING_REVIEW.value
    env["repo"].create_review(tid, "approve", "")
    # 与 API /resume 的行为对齐：受理审查决定后状态回到 RUNNING
    task = env["repo"].get(tid)
    task.status = TaskStatus.RUNNING.value
    env["repo"].save(task)

    # --- resume 执行：compress 触发丢失所有权 ---
    losing_hb = FakeHeartbeat()
    _install_heartbeat_factory(monkeypatch, [losing_hb])
    events: list[tuple] = []
    monkeypatch.setattr(
        bm, "append_reliability_event",
        lambda run_id, task_id, event, **details: events.append((event, details)),
    )
    monkeypatch.setattr(
        ra, "compress_model",
        _TriggerModel(ra.compress_model, lambda: losing_hb.force_lose("test_forced_mid_research")),
    )

    await q.ensure_group(env["redis"])
    await q.enqueue(env["redis"], tid, dedupe_ttl_ms=0)
    jobs = await q.consume(env["redis"], "w1", block_ms=200)
    assert len(jobs) == 1

    await _worker(env, "w1")._handle(jobs[0])

    # 中止且未 ACK：任务仍非终态；PEL 中仍有 pending
    assert _status(env, tid) == TaskStatus.RUNNING.value
    assert await q.pending_count(env["redis"]) == 1
    assert any(e[0] == "stale_execution_abandoned" for e in events)

    # 真实时间线里丢失后 claim 已消失 —— 这里显式删除以模拟 TTL 到期
    await env["redis"].delete(claim_key_for(tid))

    # --- XAUTOCLAIM 接管 ---
    stale = await q.reclaim_stale(env["redis"], "w2", min_idle_ms=0)
    assert len(stale) == 1

    # --- 新 owner 正常执行（不再丢失）→ completed ---
    _install_heartbeat_factory(monkeypatch, [FakeHeartbeat()])
    await _worker(env, "w2")._handle(stale[0])

    task = env["repo"].get(tid)
    assert task.status == TaskStatus.COMPLETED.value
    assert task.final_report
    # 1=初次执行, 2=被中止的 resume, 3=恢复执行
    assert task.attempt == 3
    assert await q.pending_count(env["redis"]) == 0, "恢复完成后 pending 必须清空"
