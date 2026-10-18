"""V3-only offline graph behavior with Fake models (no paid APIs).

HITL approval is mandatory; the old no-HITL automatic acceptance path has
been removed. No test should expect an unreviewed final report.
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from deep_research.agent_builder import _create_builder
from deep_research.research_seed import ResearchFenceError
from deep_research.testing.fake_llm import FakeChatModel

_RECURSION_LIMIT = 60


def test_graph_without_hitl_never_auto_approves():
    """Removing HITL can never bypass V3's evidence admission fence."""
    graph = _create_builder(with_hitl=False).compile()
    state = {"messages": [HumanMessage(content="Compare LangGraph and CrewAI")]}
    with pytest.raises(ResearchFenceError, match="human review"):
        asyncio.run(graph.ainvoke(state, config={"recursion_limit": _RECURSION_LIMIT}))


def test_graph_parallel_join_flows_to_v3_precheck():
    builder = _create_builder(with_hitl=True)
    graph = builder.compile().get_graph()
    edges = {(e.source, e.target) for e in graph.edges}
    assert ("write_research_brief", "write_draft_report") in edges
    assert ("write_research_brief", "supervisor_subgraph") in edges
    assert ("write_draft_report", "precheck_quality_gate") in edges
    assert ("supervisor_subgraph", "precheck_quality_gate") in edges
    assert "accept_research" not in graph.nodes
    assert "regenerate_research" not in graph.nodes


# ===== 3. HITL happy path =====


def test_hitl_happy_path_converges_offline():
    """驱动真实图的 HITL 全程（执行包 §24 的 happy_path 场景）。

        research brief → draft → human review → approve → final report → END
    """
    graph = _create_builder(with_hitl=True).compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "hitl-happy"}, "recursion_limit": _RECURSION_LIMIT}
    state = {"messages": [HumanMessage(content="Compare LangGraph and CrewAI")]}

    async def drive() -> tuple[tuple[str, ...], dict]:
        # 第一次执行：应停在 human_review
        async for _ in graph.astream(state, config=config, stream_mode="updates"):
            pass
        paused = await graph.aget_state(config)
        assert paused.next == ("human_review",), f"未停在 human_review: {paused.next}"
        assert paused.values.get("draft_report"), "草稿未生成"

        # approve 后 resume，跑到 END
        async for _ in graph.astream(
            Command(resume={"action": "approve"}), config=config, stream_mode="updates"
        ):
            pass
        done = await graph.aget_state(config)
        return tuple(done.next), dict(done.values)

    next_nodes, values = asyncio.run(drive())

    assert next_nodes == (), f"resume 后未结束，仍待执行: {next_nodes}"
    assert values.get("final_report"), "HITL 流程结束后没有 final_report"


def test_hitl_graph_compiles_offline():
    """带 HITL 的图（产品默认路径）必须能在无 config.yml 下编译。"""
    assert _create_builder(with_hitl=True).compile() is not None


# ===== Fake provider 接线 =====


def test_graph_nodes_are_wired_to_fake_models():
    """确认图中所有模型角色都解析为 Fake。"""
    from deep_research import agent_builder
    from deep_research.agents import (
        evaluator_agent,
        red_team_agent,
        research_agent,
        supervisor,
    )
    from deep_research.agents.draft_agent import draft_model

    for model in (
        supervisor.supervisor_model,
        supervisor.supervisor_model_with_tools,
        research_agent.model_with_tools,
        research_agent.compress_model,
        draft_model,
        red_team_agent.red_team_model,
        evaluator_agent.judge_model,
        agent_builder.writer_model,
    ):
        assert isinstance(model, FakeChatModel), f"未走 Fake: {model!r}"


@pytest.mark.parametrize("role", ["supervisor", "writer", "evaluator", "red_team", "draft"])
def test_every_warmed_role_resolves_offline(role):
    """对应 backend 启动预热循环里的 role 列表。"""
    from deep_research.llm import get_chat_model

    assert isinstance(get_chat_model(role), FakeChatModel)
