"""Phase 5B：runner 层的 lineage 拓扑选择与端到端（离线 fake 模型 + 真 Redis/SQLite）。

锁定：

- 新任务的拓扑由 env 开关决定；**resume 的拓扑由 checkpoint 里的 lineage 标记决定**
  （开关中途翻转时，在途任务按其自身语义恢复 —— 回滚不需要 state migration）；
- ACCEPT 前投机结果隔离在 `speculative_research`，notes 保持为空；
- reject → regenerate → 第二次审查 → accept：只有最新 generation 的结果进入下游；
- worker 在 regen 中途死亡（进程重启语义：新 checkpointer 实例 + 新 graph）后，
  runner 以 None 输入续跑 pending 节点并收敛；
- 取消（进程关停等价物）中途打断流：无部分状态污染，重跑后收敛。

需要真实 Redis（db 15）与临时 SQLite —— 与 test_runtime_recovery 同一约定。
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from backend.core.settings import get_settings
from backend.db import engine as db_engine
from backend.db import get_repository
from backend.db.models import Base
from backend.domain.task import TaskStatus
from backend.runtime.runner import TaskRunner
from deep_research.research_seed import SEED_LITE_ENV
from deep_research.testing.fake_llm import set_responder

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


def _install_generation_aware_compressor() -> None:
    counter = {"n": 0}

    def respond(messages):  # noqa: ANN001
        counter["n"] += 1
        return AIMessage(content=json.dumps({
            "summary": f"compressed-summary-#{counter['n']}",
            "key_excerpts": f"excerpt-#{counter['n']}",
        }))

    set_responder("researcher_compressor", respond)


@pytest.fixture
async def env(tmp_path, monkeypatch):
    import redis.asyncio as aioredis
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    import aiosqlite

    db_path = tmp_path / "tasks.db"
    monkeypatch.setattr(db_engine, "resolve_database_url", lambda: f"sqlite+pysqlite:///{db_path}")
    db_engine.reset_engine()
    Base.metadata.create_all(db_engine.get_engine())

    settings = get_settings()

    client = aioredis.from_url("redis://localhost:6379", db=TEST_DB, decode_responses=True)
    await client.flushdb()

    ckpt_path = str(tmp_path / "ckpt.db")

    async def _new_saver():
        conn = await aiosqlite.connect(ckpt_path)
        return AsyncSqliteSaver(conn), conn

    saver, conn = await _new_saver()
    _install_generation_aware_compressor()

    yield {"redis": client, "repo": get_repository(), "saver": saver,
           "settings": settings, "new_saver": _new_saver}

    await client.flushdb()
    await client.aclose()
    await conn.close()
    db_engine.reset_engine()


def _runner(env, saver=None) -> TaskRunner:
    return TaskRunner(
        redis=env["redis"], worker_id=f"w:{uuid.uuid4().hex[:8]}",
        settings=env["settings"], repository=env["repo"],
        checkpointer=saver or env["saver"],
    )


def _new_task(env, query: str = "Compare LangGraph and CrewAI") -> str:
    tid = uuid.uuid4().hex[:12]
    env["repo"].create(tid, query)
    return tid


async def _checkpoint(env, tid: str, saver=None) -> dict:
    cfg = {"configurable": {"thread_id": tid}}
    tup = await (saver or env["saver"]).aget_tuple(cfg)
    return dict((tup.checkpoint or {}).get("channel_values") or {}) if tup else {}


async def _drive_to_waiting(env, tid: str, *, saver=None):
    runner = _runner(env, saver)
    outcome = await runner.run(tid)
    assert outcome.status == TaskStatus.WAITING_REVIEW.value, outcome
    return outcome


def _submit(env, tid: str, action: str, feedback: str = "") -> None:
    env["repo"].create_review(tid, action, feedback)


# ===== 1) 开关 off：新任务 = 串行语义（历史行为）=====


async def test_flag_off_new_task_is_serial(env, monkeypatch):
    monkeypatch.delenv(SEED_LITE_ENV, raising=False)
    tid = _new_task(env)

    await _drive_to_waiting(env, tid)
    values = await _checkpoint(env, tid)
    assert not values.get("research_generation"), "串行 lineage 不得写 generation 标记"
    assert not values.get("speculative_research")
    assert not values.get("notes"), "串行模式下研究尚未开始（在 approve 之后才跑）"
    msgs = values.get("supervisor_messages") or []
    assert any("Here is the draft report" in getattr(m, "content", "") for m in msgs), \
        "串行草案必须照常写入 supervisor_messages"

    _submit(env, tid, "approve")
    outcome = await _runner(env).run(tid)
    assert outcome.status == TaskStatus.COMPLETED.value, outcome
    values = await _checkpoint(env, tid)
    assert values.get("notes"), "approve 后研究结果进入 notes"
    assert not values.get("research_generation")
    assert values.get("final_report")


# ===== 2) 开关 on：ACCEPT 前隔离；accept 后恰好一次并入 =====


async def test_flag_on_quarantines_until_accept(env, monkeypatch):
    monkeypatch.setenv(SEED_LITE_ENV, "on")
    tid = _new_task(env)

    await _drive_to_waiting(env, tid)
    values = await _checkpoint(env, tid)
    assert values.get("research_generation") == 1
    payload = values.get("speculative_research") or {}
    assert payload.get("generation") == 1 and payload.get("notes")
    assert list(values.get("notes") or []) == [], "ACCEPT 前 notes 必须为空（隔离）"

    _submit(env, tid, "approve")
    outcome = await _runner(env).run(tid)
    assert outcome.status == TaskStatus.COMPLETED.value, outcome
    values = await _checkpoint(env, tid)
    assert list(values.get("notes") or []) == list(payload["notes"])
    assert values.get("final_report")


# ===== 3) reject → regenerate → 第二次审查 → accept =====


async def test_reject_then_accept_runner_e2e(env, monkeypatch):
    monkeypatch.setenv(SEED_LITE_ENV, "on")
    tid = _new_task(env)

    await _drive_to_waiting(env, tid)
    p1 = (await _checkpoint(env, tid)).get("speculative_research") or {}

    _submit(env, tid, "revise", "请调整结构")
    await _drive_to_waiting(env, tid)
    values = await _checkpoint(env, tid)
    assert values.get("research_generation") == 2
    p2 = values.get("speculative_research") or {}
    assert p2.get("generation") == 2 and p2["notes"] != p1["notes"]
    assert list(values.get("notes") or []) == [], "第二次审查时仍未 accept"

    _submit(env, tid, "approve")
    outcome = await _runner(env).run(tid)
    assert outcome.status == TaskStatus.COMPLETED.value, outcome
    values = await _checkpoint(env, tid)
    admitted = list(values.get("notes") or [])
    assert admitted == list(p2["notes"])
    assert all(n not in admitted for n in p1["notes"]), "generation 1 不得混入"

    # 审查历史（审计）：2 条（revise + approve）
    reviews = [r for r in [env["repo"].latest_review(tid)] if r]
    assert reviews and reviews[0].action == "approve"


# ===== 4) 开关中途翻转：投机 lineage 用**标记拓扑**恢复（含 worker 崩溃场景）=====


async def test_flag_flip_speculative_lineage_resumes_with_marker_topology(env, monkeypatch):
    """gen1 研究期间 worker 死亡（进程级：无异常、无终态写入）；在开关已翻为
    off 的进程里恢复。

    若 resume 看 env（off → 串行拓扑），pending 的 supervisor_subgraph 会沿串行边
    直接进 claim_verification —— 用**空 notes** 生成报告并跳过 HITL（语义破坏）。
    正确行为：runner 读 checkpoint 标记 → 投机拓扑 → join → 二次 HITL。
    """
    import deep_research.agent_builder as ab

    monkeypatch.setenv(SEED_LITE_ENV, "on")
    tid = _new_task(env)
    saver = env["saver"]

    # 直接压图到"研究者断气"的 checkpoint（模拟进程死亡：任务行从未被消费）
    real_research = ab.supervisor_agent

    async def dying_research(state):
        raise RuntimeError("process died mid initial research")

    async def drain(graph, inp, cfg):
        async for _ in graph.astream(inp, config=cfg, stream_mode="updates"):
            pass

    monkeypatch.setattr(ab, "supervisor_agent", dying_research)
    graph = ab._create_builder(with_hitl=True, speculative=True).compile(checkpointer=saver)
    cfg = {"configurable": {"thread_id": tid}, "recursion_limit": 80}
    with pytest.raises(RuntimeError, match="died mid initial research"):
        await drain(graph, {"messages": [HumanMessage(content="q")]}, cfg)
    monkeypatch.setattr(ab, "supervisor_agent", real_research)

    pre = await _checkpoint(env, tid)
    assert pre.get("research_generation") == 1, "brief 所在的 superstep 必须已提交"
    assert not pre.get("draft_report"), "失败的 superstep 不提交 sibling 写入"

    # "新进程"恢复：开关已翻 off
    monkeypatch.delenv(SEED_LITE_ENV, raising=False)
    outcome = await _runner(env, saver).run(tid)
    assert outcome.status == TaskStatus.WAITING_REVIEW.value, (
        f"标记拓扑未生效（若走串行拓扑会直接 completed/空 notes）: {outcome}"
    )
    values = await _checkpoint(env, tid)
    assert values.get("research_generation") == 1
    payload = values.get("speculative_research") or {}
    assert payload.get("generation") == 1 and payload.get("notes")
    assert list(values.get("notes") or []) == []

    _submit(env, tid, "approve")
    outcome = await _runner(env, saver).run(tid)
    assert outcome.status == TaskStatus.COMPLETED.value, outcome
    values = await _checkpoint(env, tid)
    assert list(values.get("notes") or []) == list(payload["notes"])
    assert values.get("final_report")


async def test_flag_flip_serial_lineage_resumes_serially(env, monkeypatch):
    """反向：串行 lineage 在开关翻为 on 后恢复，语义不得漂移。"""
    monkeypatch.delenv(SEED_LITE_ENV, raising=False)
    tid = _new_task(env)

    await _drive_to_waiting(env, tid)
    monkeypatch.setenv(SEED_LITE_ENV, "on")

    _submit(env, tid, "approve")
    outcome = await _runner(env).run(tid)
    assert outcome.status == TaskStatus.COMPLETED.value, outcome
    values = await _checkpoint(env, tid)
    assert not values.get("research_generation"), "串行 lineage 不得被升级成投机"
    assert values.get("notes"), "串行研究结果照常进入 notes"


# ===== 5) worker 在 regenerate 中途死亡 → 新进程（新 checkpointer 实例）续跑 =====


async def test_process_restart_mid_regenerate_resumes(env, monkeypatch):
    import deep_research.agent_builder as ab

    monkeypatch.setenv(SEED_LITE_ENV, "on")
    tid = _new_task(env)

    # 用真实图跑到 review#1，然后以"崩溃的 regen"推进到 pending 状态
    graph = ab._create_builder(with_hitl=True, speculative=True).compile(checkpointer=env["saver"])
    cfg = {"configurable": {"thread_id": tid}, "recursion_limit": 80}
    from langgraph.types import Command

    async def drain(inp):
        async for _ in graph.astream(inp, config=cfg, stream_mode="updates"):
            pass

    await drain({"messages": [HumanMessage(content="q")]})
    p1 = ((await graph.aget_state(cfg)).values.get("speculative_research")) or {}

    real_regen = ab.regenerate_research

    async def failing_regen(state, config):
        raise RuntimeError("worker died mid regenerate")

    # 节点函数在 build 期被捕获 → 先打补丁、再重建 graph 才生效
    monkeypatch.setattr(ab, "regenerate_research", failing_regen)
    graph_crash = ab._create_builder(with_hitl=True, speculative=True).compile(checkpointer=env["saver"])
    with pytest.raises(RuntimeError, match="died mid regenerate"):
        async for _ in graph_crash.astream(
            Command(resume={"action": "revise", "feedback": "r"}), config=cfg, stream_mode="updates"
        ):
            pass
    st = await graph.aget_state(cfg)
    assert st.next == ("regenerate_research",)
    monkeypatch.setattr(ab, "regenerate_research", real_regen)

    # "进程重启"：全新的 checkpointer 实例（同一 SQLite 文件）+ 新 runner
    fresh_saver, fresh_conn = await env["new_saver"]()
    try:
        outcome = await _runner(env, fresh_saver).run(tid)
        assert outcome.status == TaskStatus.WAITING_REVIEW.value, outcome
        values = await _checkpoint(env, tid, saver=fresh_saver)
        assert values.get("research_generation") == 2
        p2 = values.get("speculative_research") or {}
        assert p2.get("generation") == 2 and p2["notes"] != p1["notes"]

        _submit(env, tid, "approve")
        outcome = await _runner(env, fresh_saver).run(tid)
        assert outcome.status == TaskStatus.COMPLETED.value, outcome
        values = await _checkpoint(env, tid, saver=fresh_saver)
        assert list(values.get("notes") or []) == list(p2["notes"])
    finally:
        await fresh_conn.close()


# ===== 6) 崩溃发生在 brief 提交之前（标记未绑定）→ 按 env 恢复且自洽 =====


async def test_crash_before_brief_commit_resumes_consistently(env, monkeypatch):
    """回归：checkpoint 无 lineage 标记时，runner 的拓扑与 brief 的决策必须一致。

    曾实测到的坏组合：runner 因"有 checkpoint 但无标记"建了**串行拓扑**，而
    brief 节点按 env（on）起的却是**投机 lineage** —— 串行拓扑没有 brief→research
    的 fan-out，研究分支**完全不运行**，任务直接停在审查点（approve 时被 fence
    拒绝 → 必然失败）。修复：brief 总是原子提交标记（0 或 1）；无标记 = 未绑定
    → 拓扑按 env，即将重跑的 brief 会读同一 env 写下一致的标记（自洽）。
    """
    import deep_research.agent_builder as ab

    monkeypatch.setenv(SEED_LITE_ENV, "on")
    tid = _new_task(env)
    saver = env["saver"]

    real_brief = ab.write_research_brief

    async def dying_brief(state):
        raise RuntimeError("crash before brief commit")

    monkeypatch.setattr(ab, "write_research_brief", dying_brief)
    graph = ab._create_builder(with_hitl=True, speculative=True).compile(checkpointer=saver)
    cfg = {"configurable": {"thread_id": tid}, "recursion_limit": 80}
    with pytest.raises(RuntimeError, match="crash before brief commit"):
        async for _ in graph.astream(
            {"messages": [HumanMessage(content="q")]}, config=cfg, stream_mode="updates"
        ):
            pass
    pre = await _checkpoint(env, tid)
    assert "research_generation" not in pre, "崩溃点在 brief 提交前，标记必须未绑定"
    monkeypatch.setattr(ab, "write_research_brief", real_brief)

    outcome = await _runner(env, saver).run(tid)
    assert outcome.status == TaskStatus.WAITING_REVIEW.value, outcome
    values = await _checkpoint(env, tid)
    assert values.get("research_generation") == 1, "brief 重跑后应绑定投机 lineage"
    payload = values.get("speculative_research") or {}
    assert payload.get("generation") == 1 and payload.get("notes"), (
        "研究分支必须真的运行过（回归：串行拓扑会跳过它）"
    )

    _submit(env, tid, "approve")
    outcome = await _runner(env, saver).run(tid)
    assert outcome.status == TaskStatus.COMPLETED.value, outcome
    values = await _checkpoint(env, tid)
    assert list(values.get("notes") or []) == list(payload["notes"])


# ===== 7) 取消（关停等价物）中途打断流：无部分污染，重跑收敛 =====


async def test_cancel_mid_speculative_run_then_resume(env, monkeypatch):
    monkeypatch.setenv(SEED_LITE_ENV, "on")
    tid = _new_task(env)

    runner = _runner(env)
    task = asyncio.ensure_future(runner.run(tid))
    await asyncio.sleep(0.08)  # 研究中途（fake 模型下总时长 ~0.3s）
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # 被取消的 attempt 绝不产出终态
    t = env["repo"].get(tid)
    assert t.status not in (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
    values = await _checkpoint(env, tid)
    assert not values.get("final_report"), "被取消的 attempt 不得留下最终报告"

    # 重跑（新 runner + 同一 checkpointer）必须收敛到审查点，且 accept 后一致。
    # 取消可能落在 brief 提交前（checkpoint 无标记 → 按 env 恢复、brief 重跑自洽）
    # 或提交后（标记已绑定 → 按其语义恢复）——两种时序都必须收敛。
    await _drive_to_waiting(env, tid)
    values = await _checkpoint(env, tid)
    payload = values.get("speculative_research") or {}
    assert payload.get("generation") in (1, 2), payload  # 取决于取消落在哪一步
    assert list(values.get("notes") or []) == []

    _submit(env, tid, "approve")
    outcome = await _runner(env).run(tid)
    assert outcome.status == TaskStatus.COMPLETED.value, outcome
    values = await _checkpoint(env, tid)
    assert list(values.get("notes") or []) == list(payload["notes"])
    assert values.get("final_report")
