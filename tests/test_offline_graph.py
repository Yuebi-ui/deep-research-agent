"""整图离线执行（执行包 Phase D.5，§23-25）。

本文件包含三类测试，**目的不同，不要混淆**：

1. **网络安全测试** —— 整图跑一遍，证明不产生任何外部调用。
   它**不**证明图能收敛。
2. **确定性收敛测试** —— 证明图能跑到 END 并产出 final_report。
   成功条件里**不允许**出现 ``except GraphRecursionError: pass``
   （执行包 §25 明确要求）。
3. **HITL happy path** —— 驱动真实图的 interrupt / resume 全程。

后台：修复 `red_team` 的静态出边之前，supervisor 子图**无法通过红队路径
终止**（见 tests/test_graph_wiring.py 的回归测试）。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from deep_research.agent_builder import _create_builder
from deep_research.testing.fake_llm import FakeChatModel

# 图在 Fake 模型下的确定性执行需要多步，但远小于此上限；
# 留出余量是为了让"未收敛"表现为失败而不是超时。
_RECURSION_LIMIT = 60


def _run_graph(*, with_hitl: bool = False, limit: int = _RECURSION_LIMIT) -> dict:
    """同步跑完整图并返回最终 state。不吞任何异常。"""
    graph = _create_builder(with_hitl=with_hitl).compile()
    state = {"messages": [HumanMessage(content="Compare LangGraph and CrewAI")]}
    return asyncio.run(graph.ainvoke(state, config={"recursion_limit": limit}))


# ===== 1. 网络安全测试 =====


def test_graph_execution_makes_no_external_calls():
    """整图执行不得触发网络守卫。

    注意本测试的**目的仅限网络隔离**：若任何 provider 意外走到真实网络
    路径，conftest 的守卫会抛
    ``RuntimeError: External network access is disabled...``，本测试即失败。

    图是否能收敛由下面的 test_full_graph_converges_offline 负责，两者不可混淆。
    """
    try:
        _run_graph(with_hitl=False, limit=25)
    except Exception as exc:  # noqa: BLE001
        # 收敛问题由专门的测试覆盖，这里只关心"没有外联"
        assert "External network access is disabled" not in str(exc), (
            f"整图执行尝试访问外部网络: {exc}"
        )


# ===== 2. 确定性收敛 =====


def test_full_graph_converges_offline():
    """图必须跑到 END，产出 final_report，且**不得**抛 GraphRecursionError。

    这是执行包 §25 要求的"真正的"收敛测试。它不捕获 GraphRecursionError——
    一旦回归，测试直接失败。
    """
    final = _run_graph(with_hitl=False)

    assert final.get("final_report"), "收敛但未产出 final_report"
    assert isinstance(final["final_report"], str)
    assert final["final_report"].strip()


def test_full_graph_visits_each_node_once():
    """节点顺序与次数必须确定——这是"确定性"的具体含义。"""
    graph = _create_builder(with_hitl=False).compile()
    state = {"messages": [HumanMessage(content="Compare LangGraph and CrewAI")]}

    async def collect() -> list[str]:
        order = []
        async for chunk in graph.astream(
            state, config={"recursion_limit": _RECURSION_LIMIT}, stream_mode="updates"
        ):
            order.extend(chunk.keys())
        return order

    order = asyncio.run(collect())

    assert order == [
        "write_research_brief",
        "write_draft_report",
        "supervisor_subgraph",
        "claim_verification",
        "final_report_generation",
    ], f"节点顺序不符: {order}"


def test_supervisor_subgraph_terminates_via_red_team():
    """回归测试：supervisor 子图必须能通过红队路径终止。

    背景：曾经存在 `add_edge("red_team", "supervisor")` 静态边。LangGraph 中
    静态出边会**覆盖**节点返回的 `Command(goto=END)`，导致 red_team 永远无法
    结束子图，只能撞 recursion_limit。该静态边已移除——本测试锁定这一点。
    """
    from deep_research.agents.supervisor import supervisor_agent

    init = {
        "supervisor_messages": [HumanMessage(content="Research: compare A and B")],
        "research_brief": "compare A and B",
        "research_iterations": 0,
        "notes": [],
        "raw_notes": [],
        "quality_history": [],
        "active_critiques": [],
        "draft_report": "# 草稿",
    }

    async def count_steps() -> int:
        steps = 0
        async for _ in supervisor_agent.astream(
            init, config={"recursion_limit": 20}, stream_mode="updates"
        ):
            steps += 1
        return steps

    steps = asyncio.run(count_steps())
    assert steps < 20, f"子图未收敛，跑了 {steps} 步（疑似撞上 recursion_limit）"


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
