"""Phase 5A：research_seed 投影 + 投机（seed-lite）wiring 的测试。

覆盖：
- seed 是 brief 的**确定性投影**（无 LLM、可 fingerprint、明显小于 draft）；
- Parallel-only V3 ignores the historical on/off switch; serial fallback is gone;
- 投机模式下**真图离线跑通**（fake 模型）：fan-out 并行、join 正确、
  supervisor 拿到的是 seed 而不是 draft。

注：历史 seed 环境开关不再改变 V3 执行拓扑。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import HumanMessage

from deep_research.research_seed import (
    SEED_LITE_ENV,
    SEED_VERSION,
    build_research_seed,
    render_seed_message,
    seed_fingerprint,
    seed_lite_speculative_enabled,
    seed_metrics,
)

_BRIEF = (
    "Please research LangGraph: definition, core architecture (nodes, edges, state graph), "
    "differences from linear chains, main use cases, and best practices. "
    "Prefer official documentation and the GitHub repository."
)


# ===== seed 投影 =====


def test_seed_is_deterministic_projection_of_brief():
    s1, s2 = build_research_seed(_BRIEF), build_research_seed(_BRIEF)
    assert s1 == s2
    assert s1["seed_version"] == SEED_VERSION
    assert s1["objective"] == _BRIEF, "不得改写用户/简报原始意图"
    assert s1["draft_available"] is False
    assert seed_fingerprint(s1) == seed_fingerprint(s2)


def test_seed_fingerprint_changes_with_brief_and_ignores_whitespace():
    a = build_research_seed("topic A")
    b = build_research_seed("topic B")
    assert seed_fingerprint(a) != seed_fingerprint(b)
    assert seed_fingerprint(build_research_seed("  x  ")) == seed_fingerprint(build_research_seed("x"))


def test_seed_message_marks_absence_of_draft():
    msg = render_seed_message(build_research_seed(_BRIEF))
    assert _BRIEF in msg
    assert "no draft report" in msg.lower()
    assert "draft report: " not in msg.lower(), "不得伪装成草稿"


def test_seed_is_much_smaller_than_draft():
    """真实 run 的 draft 为 7442–17980 字符 —— seed 必须明显更小。"""
    m = seed_metrics(build_research_seed(_BRIEF), draft_chars=7442)
    assert m["size_ratio_draft_over_seed"] >= 5, m
    assert m["seed_message_chars"] < 1200


def test_seed_handles_missing_brief():
    seed = build_research_seed("")
    assert seed["objective"] == ""
    assert render_seed_message(seed)  # 不抛异常


# ===== 开关 =====


def test_flag_defaults_on(monkeypatch):
    """Phase 5B 定版：未设置 env 时默认走投机拓扑（生产默认路径）。"""
    monkeypatch.delenv(SEED_LITE_ENV, raising=False)
    assert seed_lite_speculative_enabled() is True


def test_flag_explicit_off_is_ignored(monkeypatch):
    """Parallel-only V3 cannot restore serial routing through a stale flag."""
    monkeypatch.setenv(SEED_LITE_ENV, "off")
    assert seed_lite_speculative_enabled() is True


@pytest.mark.parametrize("value", ["on", "1", "true", "YES", "off", "0", "", "whatever"])
def test_obsolete_flag_is_always_parallel(monkeypatch, value):
    monkeypatch.setenv(SEED_LITE_ENV, value)
    assert seed_lite_speculative_enabled() is True


# ===== wiring =====


def _topology(monkeypatch, *, speculative: bool) -> tuple[list[str], list[tuple[str, str]]]:
    from deep_research.agent_builder import _create_builder

    monkeypatch.setenv(SEED_LITE_ENV, "on" if speculative else "off")
    builder = _create_builder(with_hitl=False)
    graph = builder.compile().get_graph()
    nodes = sorted(n.id for n in graph.nodes.values())
    edges = sorted((e.source, e.target) for e in graph.edges)
    return nodes, edges


def test_speculative_topology_fans_out_and_joins(monkeypatch):
    """两条分支必须是 write_research_brief 的**直接后继**（同 superstep = 真并发）。

    踩过的坑：中间加一个 seed 节点会把 research 推到下一个 superstep；
    LangGraph 的 superstep 是屏障 → draft(65s) 与 seed(瞬时) 同 superstep →
    research 仍要等 draft 跑完（实测 overlap = 0.0s，等于没并行）。
    """
    nodes, edges = _topology(monkeypatch, speculative=True)
    assert "build_research_seed" not in nodes, "不得再引入中转节点（会破坏并行）"
    assert ("write_research_brief", "write_draft_report") in edges
    assert ("write_research_brief", "supervisor_subgraph") in edges
    assert ("build_research_seed", "supervisor_subgraph") not in edges
    # V3-only AND-join: all candidate evidence goes through structural precheck.
    assert ("write_draft_report", "precheck_quality_gate") in edges
    assert ("supervisor_subgraph", "precheck_quality_gate") in edges
    assert ("precheck_quality_gate", "claim_verification") not in edges
    # 串行边必须消失（否则 research 又要等 draft）
    assert ("write_draft_report", "supervisor_subgraph") not in edges
    assert ("supervisor_subgraph", "claim_verification") not in edges


def test_stale_flag_off_cannot_restore_serial_graph(monkeypatch):
    nodes, edges = _topology(monkeypatch, speculative=False)
    assert "build_research_seed" not in nodes
    assert ("write_draft_report", "supervisor_subgraph") not in edges
    assert ("write_research_brief", "supervisor_subgraph") in edges
    assert ("write_research_brief", "write_draft_report") in edges


# ===== 真图离线端到端（fake 模型）=====


def _run_graph(order_out: list, *, speculative: bool, supersteps: list | None = None) -> dict:
    from deep_research.agent_builder import _create_builder

    supersteps = supersteps if supersteps is not None else []
    graph = _create_builder(with_hitl=False).compile()
    state = {"messages": [HumanMessage(content="Compare LangGraph and CrewAI")]}

    async def collect() -> dict:
        final: dict = {}
        async for chunk in graph.astream(state, config={"recursion_limit": 60}, stream_mode="updates"):
            for node in chunk:
                order_out.append(node)
            # 记录"同 superstep 的分支集合"：真并行的判据
            supersteps.append(sorted(k for k in chunk if k != "__metadata__"))
            final.update(chunk.get("final_report_generation", {}) or {})
        return final

    return asyncio.run(collect())


def test_v3_no_hitl_is_not_an_auto_accept_path(monkeypatch):
    """V3 may compile without HITL for testing but must not auto-publish."""
    from deep_research.research_seed import ResearchFenceError

    monkeypatch.setenv(SEED_LITE_ENV, "on")
    order: list[str] = []
    with pytest.raises(ResearchFenceError, match="human review"):
        _run_graph(order, speculative=True)
    assert "write_draft_report" in order
    assert "supervisor_subgraph" in order


def test_speculative_hitl_approve_goes_to_claim_verification(monkeypatch):
    """回归：投机模式下 approve 必须直接进 claim_verification。

    曾经 human_review 的批准分支硬编码 `goto="supervisor_subgraph"`（串行模式的
    正确目标）；在投机拓扑里 supervisor_subgraph 的静态出边又指回 human_review，
    于是会**再触发一次 HITL 并重跑研究**（实测踩到：任务出现两次 waiting_review）。
    """
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from deep_research.agent_builder import _create_builder

    monkeypatch.setenv(SEED_LITE_ENV, "on")
    graph = _create_builder(with_hitl=True).compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "p5a-hitl-approve"}, "recursion_limit": 60}
    state = {"messages": [HumanMessage(content="Compare LangGraph and CrewAI")]}
    order: list[str] = []

    async def drive() -> dict:
        async for chunk in graph.astream(state, config=config, stream_mode="updates"):
            order.extend(chunk.keys())
        paused = await graph.aget_state(config)
        assert paused.next == ("human_review",)
        async for chunk in graph.astream(Command(resume={"action": "approve"}),
                                         config=config, stream_mode="updates"):
            order.extend(chunk.keys())
        return dict((await graph.aget_state(config)).values)

    values = asyncio.run(drive())
    assert values.get("final_report"), "approve 后必须跑到最终报告"
    assert order.count("human_review") == 1, f"HITL 被触发了多次：{order}"
    assert "claim_verification" in order, f"approve 后未进入 claim_verification：{order}"
    # join 必须是 AND：HITL 只在研究分支跑完之后触发，且 resume 不得重跑研究
    assert order.count("supervisor_subgraph") == 1, f"研究分支被重跑（重复副作用）：{order}"
    assert order.index("supervisor_subgraph") < order.index("human_review"), order


def test_speculative_branches_overlap_in_time(monkeypatch):
    """真并行的直接证据：两条分支耗时**重叠**（不是相加）。

    用两个各睡 0.3s 的假分支替换真节点：并行 ⇒ 总时长 ≈0.3s；串行 ⇒ ≈0.6s。
    （`stream_mode="updates"` 是**逐节点**产出的，不能用来判断 superstep 分组，
    所以这里用挂钟时间作为判据 —— 与线上 A/B 用的 overlap 指标同一口径。）
    """
    import time

    import deep_research.agent_builder as ab

    monkeypatch.setenv(SEED_LITE_ENV, "on")

    async def slow_draft(state):
        await asyncio.sleep(0.3)
        return {"draft_report": "d"}

    async def slow_research(state):
        # Phase 5B：投机 lineage 的研究分支收尾必须产出带 generation 的
        # payload（隔离在 speculative_research 中），accept 时再并入 notes。
        await asyncio.sleep(0.3)
        return {"speculative_research": {
            "generation": int(state.get("research_generation", 1) or 1),
            "seed_fingerprint": "stub",
            "notes": ["n"],
        }}

    monkeypatch.setattr(ab, "write_draft_report", slow_draft)
    monkeypatch.setattr(ab, "supervisor_agent", slow_research)

    graph = ab._create_builder(with_hitl=False).compile()
    state = {"messages": [HumanMessage(content="q")]}

    async def run():
        t0 = time.monotonic()
        try:
            await graph.ainvoke(state, config={"recursion_limit": 30})
        except Exception as exc:
            from deep_research.research_seed import ResearchFenceError
            # Stubbed research has no valid checklist-bound source ledger:
            # the subsequent V3 fence must reject it, but only *after* both
            # parallel branch calls have completed.
            assert isinstance(exc, ResearchFenceError), exc
        return time.monotonic() - t0

    elapsed = asyncio.run(run())
    assert elapsed < 0.55, f"两条分支没有并行（{elapsed:.2f}s ≈ 0.3+0.3）"


def test_v3_supervisor_receives_seed_not_draft(monkeypatch):
    """The Supervisor gets the independent research seed, not Draft output."""
    from deep_research.agent_builder import _create_builder
    from langgraph.checkpoint.memory import InMemorySaver

    graph = _create_builder(with_hitl=True).compile(checkpointer=InMemorySaver())
    state = {"messages": [HumanMessage(content="Compare LangGraph and CrewAI")]}
    cfg = {"configurable": {"thread_id": "seed-v3"}, "recursion_limit": 60}
    asyncio.run(graph.ainvoke(state, config=cfg))
    out = asyncio.run(graph.aget_state(cfg)).values
    msgs = out.get("supervisor_messages") or []
    texts = [m.content if hasattr(m, "content") else str(m) for m in msgs]
    joined = "\n".join(texts)
    assert "Research seed" in joined, f"Seed was not injected: {texts[:2]}"
    assert "Here is the draft report" not in joined
