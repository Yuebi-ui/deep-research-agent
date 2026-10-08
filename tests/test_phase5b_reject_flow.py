"""Phase 5B：真实 graph（离线 fake 模型）上的 reject/regenerate 与 stale fencing。

覆盖：

- ACCEPT 路径：隔离的投机结果在 approve 后**恰好一次**并入 notes 并进入下游；
- REJECT → 新 generation → ACCEPT：旧结果作废，只有新 generation 进入 notes；
- REJECT → REJECT → ACCEPT：代数持续推进，研究分支永不因回环被重复执行；
- late stale 注入（aupdate_state 模拟"R1 迟到"）：
  * R1 在 R2 完成后到达 → ACCEPT 被 fence 拒绝（显式失败，下游零执行）；
  * R1 在 R2 之前到达 → 被 R2 覆盖，ACCEPT 只认 R2；
- 崩溃重放：regen 节点失败后重启（新 graph 实例、同一 saver）只重跑 regen；
  gen1 研究分支失败后重启不产生重复的下游推进。

fake 模型下 `researcher_compressor` 用计数器产生可区分的一代一代研究内容，
因此可以**逐字**断言"哪一代的结果进入了 notes"。
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from deep_research.agent_builder import _create_builder
from deep_research.research_seed import ResearchFenceError, SEED_LITE_ENV
from deep_research.testing.fake_llm import set_responder


# ===== fake：一代一代可区分的研究内容 =====


def _install_generation_aware_compressor() -> None:
    counter = {"n": 0}

    def respond(messages):  # noqa: ANN001
        counter["n"] += 1
        return AIMessage(content=json.dumps({
            "summary": f"compressed-summary-#{counter['n']}",
            "key_excerpts": f"excerpt-#{counter['n']}",
        }))

    set_responder("researcher_compressor", respond)


# ===== 驱动 =====


class Driver:
    def __init__(self, graph, thread_id: str):
        self.graph = graph
        self.cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 80}
        self.order: list[str] = []
        self.interrupts = 0

    async def drain(self, inp) -> None:
        async for chunk in self.graph.astream(inp, config=self.cfg, stream_mode="updates"):
            for key in chunk:
                if key == "__interrupt__":
                    self.interrupts += 1
                    self.order.append("__interrupt__")
                else:
                    self.order.append(key)

    async def state(self):
        return await self.graph.aget_state(self.cfg)

    def count(self, node: str) -> int:
        return self.order.count(node)


def _spec_graph(saver):
    return _create_builder(with_hitl=True, speculative=True).compile(checkpointer=saver)


@pytest.fixture
def spec_env(monkeypatch):
    monkeypatch.setenv(SEED_LITE_ENV, "on")
    _install_generation_aware_compressor()
    yield


def _payload(values) -> dict:
    return copy.deepcopy(values.get("speculative_research") or {})


# ===== 1) ACCEPT：gen1 恰好一次并入 notes =====


def test_accept_admits_generation_1_exactly_once(spec_env):
    async def run() -> None:
        from langgraph.types import Command

        graph = _spec_graph(InMemorySaver())
        d = Driver(graph, "p5b-accept")

        await d.drain({"messages": [HumanMessage(content="Compare LangGraph and CrewAI")]})
        st = await d.state()
        assert st.next == ("human_review",), st.next
        assert st.values.get("research_generation") == 1
        payload1 = _payload(st.values)
        assert payload1.get("generation") == 1 and payload1.get("notes"), payload1
        # 隔离证明：HITL 审查时刻 notes 仍为空 —— 研究结果在 ACCEPT 前不进入下游
        assert list(st.values.get("notes") or []) == [], st.values.get("notes")

        await d.drain(Command(resume={"action": "approve"}))
        st = await d.state()
        assert tuple(st.next) == ()
        assert d.interrupts == 1
        assert d.count("supervisor_subgraph") == 1, d.order
        assert d.count("claim_verification") == 1 and d.count("final_report_generation") == 1, d.order
        # notes == gen1 payload，恰好一次（无翻倍、无缺失）
        assert list(st.values.get("notes") or []) == list(payload1["notes"])
        assert st.values.get("final_report")

    asyncio.run(run())


# ===== 2) REJECT → 新 generation → ACCEPT =====


def test_reject_regenerates_and_admits_only_generation_2(spec_env):
    async def run() -> None:
        graph = _spec_graph(InMemorySaver())
        d = Driver(graph, "p5b-reject-accept")

        await d.drain({"messages": [HumanMessage(content="q")]})
        st = await d.state()
        payload1 = _payload(st.values)

        # 第一次 revise：旧 generation 作废、重跑研究
        from langgraph.types import Command
        await d.drain(Command(resume={"action": "revise", "feedback": "请改结构"}))
        st = await d.state()
        assert st.next == ("human_review",), "regen 后必须回到审查点"
        assert d.interrupts == 2
        assert st.values.get("research_generation") == 2
        payload2 = _payload(st.values)
        assert payload2.get("generation") == 2
        assert payload2["notes"] != payload1["notes"], "新一代研究必须有新内容（fake 计数器）"
        # 关键：研究分支没有被回环重复执行；只多了一次 regenerate
        assert d.count("supervisor_subgraph") == 1, d.order
        assert d.count("write_draft_report") == 1, d.order
        assert d.count("regenerate_research") == 1, d.order
        # ACCEPT 前 notes 仍为空
        assert list(st.values.get("notes") or []) == []

        from langgraph.types import Command as C
        await d.drain(C(resume={"action": "approve"}))
        st = await d.state()
        assert tuple(st.next) == ()
        admitted = list(st.values.get("notes") or [])
        assert admitted == list(payload2["notes"]), "只有 generation 2 的结果可以进入下游"
        for stale in payload1["notes"]:
            assert stale not in admitted, "stale generation 1 结果不得出现"

    asyncio.run(run())


# ===== 3) REJECT → REJECT → ACCEPT =====


def test_reject_reject_accept_advances_generations(spec_env):
    async def run() -> None:
        from langgraph.types import Command

        graph = _spec_graph(InMemorySaver())
        d = Driver(graph, "p5b-reject-reject-accept")

        await d.drain({"messages": [HumanMessage(content="q")]})
        p1 = _payload((await d.state()).values)

        await d.drain(Command(resume={"action": "revise", "feedback": "r1"}))
        p2 = _payload((await d.state()).values)
        await d.drain(Command(resume={"action": "revise", "feedback": "r2"}))
        st = await d.state()
        p3 = _payload(st.values)

        assert d.interrupts == 3
        assert st.values.get("research_generation") == 3
        assert p3["generation"] == 3
        assert d.count("regenerate_research") == 2, d.order
        assert d.count("supervisor_subgraph") == 1, d.order
        assert d.count("write_draft_report") == 1, d.order

        await d.drain(Command(resume={"action": "approve"}))
        st = await d.state()
        admitted = list(st.values.get("notes") or [])
        assert admitted == list(p3["notes"])
        assert all(n not in admitted for n in p1["notes"] + p2["notes"])

    asyncio.run(run())


# ===== 4) late stale：R1 在 R2 完成后到达 → fence 拒绝 =====


def test_late_stale_after_r2_is_fenced_out(spec_env):
    async def run() -> None:
        from langgraph.types import Command

        graph = _spec_graph(InMemorySaver())
        d = Driver(graph, "p5b-late-stale")

        await d.drain({"messages": [HumanMessage(content="q")]})
        p1 = _payload((await d.state()).values)
        await d.drain(Command(resume={"action": "revise", "feedback": "r"}))
        st = await d.state()
        assert st.next == ("human_review",)

        # 模拟"R1 迟到"：晚到的 generation 1 结果覆盖了当前隔离字段
        # （as_node 显式记成 regenerate_research 的写入 —— 陈旧执行者的晚到写）
        await graph.aupdate_state(
            d.cfg, {"speculative_research": p1}, as_node="regenerate_research"
        )
        st = await d.state()
        assert _payload(st.values)["generation"] == 1, "注入未生效，测试前提不成立"

        before = len(d.order)
        with pytest.raises(ResearchFenceError):
            await d.drain(Command(resume={"action": "approve"}))

        # 拒绝进入下游：claim_verification / writer 零执行、notes 保持为空
        downstream = d.order[before:]
        assert "claim_verification" not in downstream, downstream
        assert "final_report_generation" not in downstream, downstream
        st = await d.state()
        assert list(st.values.get("notes") or []) == []
        assert not st.values.get("final_report")

    asyncio.run(run())


# ===== 5) late stale：R1 在 R2 之前到达 → 被 R2 覆盖 =====


def test_stale_write_before_r2_is_superseded(spec_env):
    async def run() -> None:
        from langgraph.types import Command

        graph = _spec_graph(InMemorySaver())
        d = Driver(graph, "p5b-stale-superseded")

        await d.drain({"messages": [HumanMessage(content="q")]})
        p1 = _payload((await d.state()).values)
        # "R1 迟到"发生在 R2 仍将运行之时：注入到 review#1 的隔离字段。
        # as_node 必须显式（review 有两个 join 入边，无法推断）——把它记成
        # regenerate_research 的写入，正是"陈旧执行者晚到"的语义。
        await graph.aupdate_state(
            d.cfg,
            {"speculative_research": {"generation": 1, "notes": ["STALE-R1"]}},
            as_node="regenerate_research",
        )

        await d.drain(Command(resume={"action": "revise", "feedback": "r"}))
        st = await d.state()
        p2 = _payload(st.values)
        assert p2["generation"] == 2 and "STALE-R1" not in p2["notes"], p2

        await d.drain(Command(resume={"action": "approve"}))
        st = await d.state()
        admitted = list(st.values.get("notes") or [])
        assert admitted == list(p2["notes"])
        assert "STALE-R1" not in admitted
        assert all(n not in admitted for n in p1["notes"])

    asyncio.run(run())


# ===== 6) 完成后重放：不得重复推进下游、不得翻倍 notes =====


def test_replay_after_completion_is_noop(spec_env):
    async def run() -> None:
        from langgraph.types import Command

        graph = _spec_graph(InMemorySaver())
        d = Driver(graph, "p5b-replay-noop")

        await d.drain({"messages": [HumanMessage(content="q")]})
        await d.drain(Command(resume={"action": "approve"}))
        st = await d.state()
        notes_after = list(st.values.get("notes") or [])
        order_after = len(d.order)

        await d.drain(None)  # 已完成的 thread 再次投喂：不得执行任何节点
        st = await d.state()
        assert len(d.order) == order_after, d.order[order_after:]
        assert list(st.values.get("notes") or []) == notes_after

    asyncio.run(run())


# ===== 7) 崩溃重放：regen 节点失败 → 重启后只重跑 regen =====


def test_restart_mid_regenerate_replays_only_regen(spec_env, monkeypatch):
    async def run() -> None:
        from langgraph.types import Command
        import deep_research.agent_builder as ab

        saver = InMemorySaver()
        original_regen = ab.regenerate_research

        # 构建 A：regenerate_research 换成必然失败的 stub（模拟 worker 崩溃）
        async def failing_regen(state, config):
            raise RuntimeError("worker crashed during regenerate")

        monkeypatch.setattr(ab, "regenerate_research", failing_regen)
        graph_a = _spec_graph(saver)
        d = Driver(graph_a, "p5b-restart-regen")

        await d.drain({"messages": [HumanMessage(content="q")]})
        p1 = _payload((await d.state()).values)
        with pytest.raises(RuntimeError, match="worker crashed"):
            await d.drain(Command(resume={"action": "revise", "feedback": "r"}))

        st = await d.state()
        assert st.next == ("regenerate_research",), f"崩溃后应停在 regen: {st.next}"
        assert st.values.get("research_generation") == 1, "generation 只在 regen 完成后推进"
        assert _payload(st.values)["generation"] == 1, "旧 payload 保持原样"

        # 构建 B：真实节点，同一 saver（= 进程重启后 resume）。
        # 注意只用 setattr 恢复这一个节点 —— fake 计数器保持连续，因此
        # generation 2 的研究内容必然与 generation 1 不同。
        monkeypatch.setattr(ab, "regenerate_research", original_regen)
        graph_b = _spec_graph(saver)
        d_b = Driver(graph_b, "p5b-restart-regen")

        await d_b.drain(None)  # 以 None 输入从 checkpoint 续跑 pending 的 regen
        st = await d_b.state()
        assert st.next == ("human_review",), f"regen 恢复后应停在审查点: {st.next}"
        assert d_b.count("regenerate_research") == 1, d_b.order
        assert d_b.count("supervisor_subgraph") == 0, "已完成的 gen1 分支不得重放"
        assert d_b.count("write_draft_report") == 0, d_b.order
        assert st.values.get("research_generation") == 2
        p2 = _payload(st.values)
        assert p2["generation"] == 2 and p2["notes"] != p1["notes"]

        await d_b.drain(Command(resume={"action": "approve"}))
        st = await d_b.state()
        assert list(st.values.get("notes") or []) == list(p2["notes"])

    asyncio.run(run())


# ===== 8) 崩溃重放：gen1 研究分支失败 → 重启续跑不产生重复下游 =====


def test_restart_mid_initial_research_resumes_once(spec_env, monkeypatch):
    async def run() -> None:
        import deep_research.agent_builder as ab

        saver = InMemorySaver()
        original_research = ab.supervisor_agent

        async def failing_research(state):
            raise RuntimeError("worker crashed during initial research")

        monkeypatch.setattr(ab, "supervisor_agent", failing_research)
        graph_a = _spec_graph(saver)
        d = Driver(graph_a, "p5b-restart-initial")

        with pytest.raises(RuntimeError, match="crashed during initial research"):
            await d.drain({"messages": [HumanMessage(content="q")]})

        st = await d.state()
        # 初始 superstep 失败：pending 至少包含研究分支（draft 是否已提交以实现为准）
        assert "supervisor_subgraph" in tuple(st.next), f"应停在研究分支: {st.next}"

        monkeypatch.setattr(ab, "supervisor_agent", original_research)
        graph_b = _spec_graph(saver)
        d_b = Driver(graph_b, "p5b-restart-initial")

        await d_b.drain(None)
        st = await d_b.state()
        assert st.next == ("human_review",), f"续跑后应停在审查点: {st.next}"
        assert d_b.count("supervisor_subgraph") == 1, d_b.order
        # 实测语义（与 interrupt 不同）：同 superstep 有节点**抛异常**时，
        # 已完成 sibling 的写入**不提交**，恢复时两条分支都会重放一次。
        # 这是"浪费一次 draft 调用"，不是正确性问题（LastValue 覆盖，无下游重复）。
        assert d_b.count("write_draft_report") == 1, d_b.order
        assert st.values.get("research_generation") == 1
        payload = _payload(st.values)
        assert payload.get("generation") == 1 and payload.get("notes")

        from langgraph.types import Command
        await d_b.drain(Command(resume={"action": "approve"}))
        st = await d_b.state()
        assert tuple(st.next) == ()
        # 下游恰好一次
        assert d_b.count("claim_verification") == 1, d_b.order
        assert d_b.count("final_report_generation") == 1, d_b.order
        assert list(st.values.get("notes") or []) == list(payload["notes"])

    asyncio.run(run())
