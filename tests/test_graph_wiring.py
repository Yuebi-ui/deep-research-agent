"""LangGraph 装配层回归测试。

本文件锁定的是一条**容易踩坑的 LangGraph 语义**，以及它在本项目里造成的
一个真实缺陷（Phase D.5 发现）：

    当节点同时满足
      (a) 返回 Command(goto=X)，且
      (b) 图里还给它声明了静态出边 add_edge(node, Y)
    时，**静态出边会覆盖 Command 的 goto**。

    `deep_research/agents/supervisor.py` 曾经有
    `supervisor_builder.add_edge("red_team", "supervisor")`。
    而 `red_team_node` 在 final_exit 模式下返回 `Command(goto=END)`——
    这条静态边把 END 吃掉了，导致 supervisor 子图**永远无法通过红队路径
    终止**，只能撞到 recursion_limit。

    因为 `red_team_node` 的每一条返回路径都显式指定了 goto，那条静态边
    完全冗余。移除它即恢复 red_team docstring 声明的语义。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from typing_extensions import TypedDict


# ===== 1. 记录 LangGraph 语义本身 =====


class _ProbeState(TypedDict, total=False):
    n: int
    flag: bool


def _build_probe(*, with_static_back_edge: bool):
    def node_a(state: _ProbeState):
        return Command(goto="b", update={"flag": True, "n": state.get("n", 0) + 1})

    def node_b(_state: _ProbeState):
        return Command(goto=END)

    builder = StateGraph(_ProbeState)
    builder.add_node("a", node_a)
    builder.add_node("b", node_b)
    builder.add_edge(START, "a")
    if with_static_back_edge:
        builder.add_edge("b", "a")
    return builder.compile()


def test_static_edge_overrides_command_goto():
    """语义记录：静态出边存在时，Command(goto=END) 被忽略并回环。"""
    graph = _build_probe(with_static_back_edge=True)

    with pytest.raises(Exception) as exc_info:
        graph.invoke({"n": 0}, config={"recursion_limit": 6})

    assert "ecursion" in type(exc_info.value).__name__ or "ecursion" in str(exc_info.value), (
        "预期因静态边覆盖 END 而递归超限"
    )


def test_command_goto_works_without_static_edge():
    """对照组：没有静态出边时，Command(goto=END) 正常生效。"""
    graph = _build_probe(with_static_back_edge=False)
    assert graph.invoke({"n": 0}, config={"recursion_limit": 6})["n"] == 1


# ===== 2. 锁定生产子图不得再有该静态边 =====


def _static_edges_from(node: str) -> list[str]:
    """列出某节点的**静态**出边目标。"""
    from deep_research.agents.supervisor import supervisor_agent

    graph = supervisor_agent.get_graph()
    return [
        edge.target
        for edge in graph.edges
        if edge.source == node and not getattr(edge, "conditional", False)
    ]


def test_red_team_has_no_static_outgoing_edge():
    """red_team 的路由必须完全由它自己返回的 Command 决定。

    一旦有人重新加上 add_edge("red_team", ...)，red_team 的
    Command(goto=END) 就会被覆盖，子图将无法终止。
    """
    assert _static_edges_from("red_team") == [], (
        "red_team 不应有静态出边——它会覆盖 red_team_node 返回的 "
        "Command(goto=END)，导致 supervisor 子图无法终止"
    )


def _subgraph_init() -> dict:
    return {
        "supervisor_messages": [HumanMessage(content="Research: compare A and B")],
        "research_brief": "compare A and B",
        "research_iterations": 0,
        "notes": [],
        "raw_notes": [],
        "quality_history": [],
        "active_critiques": [],
        "draft_report": "# 草稿",
    }


def _visited_nodes(limit: int = 20) -> list[str]:
    """记录子图实际访问过的节点序列。

    注意不能通过 `graph.get_graph().edges` 判断 red_team 是否可达——
    red_team 是由 supervisor_tools 返回的 Command(goto="red_team") 触达的，
    这类路由在 introspection 里不表现为静态/条件边。行为验证才可靠。
    """
    from deep_research.agents.supervisor import supervisor_agent

    async def run() -> list[str]:
        seen: list[str] = []
        async for chunk in supervisor_agent.astream(
            _subgraph_init(), config={"recursion_limit": limit}, stream_mode="updates"
        ):
            seen.extend(chunk.keys())
        return seen

    return asyncio.run(run())


def test_supervisor_subgraph_still_reaches_red_team():
    """移除静态边后，red_team 仍必须被实际访问到。"""
    assert "red_team" in _visited_nodes(), (
        "red_team 未被访问——supervisor_tools 的 exit 分支跳转失败"
    )


def test_supervisor_subgraph_terminates():
    """端到端：子图必须跑到 END，而不是撞 recursion_limit。

    不捕获 GraphRecursionError——未收敛时必须失败。
    """
    seen = _visited_nodes(limit=20)
    assert len(seen) < 20, f"子图未收敛，跑了 {len(seen)} 步"
    assert seen[-1] == "red_team", f"应以 red_team 收尾，实际: {seen[-1]}"
