"""LangGraph routing regression and the V3-only Supervisor subgraph contract.

The old Supervisor child Red Team has been removed: all adversarial checks live
at the parent graph and are triggered only by the V3 Quality Gate.
"""

from __future__ import annotations

import pytest
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



# ===== 2. Current production graph contract =====


def test_supervisor_subgraph_no_longer_contains_red_team():
    """Prevent reintroducing the duplicate LLM review loop."""
    from deep_research.agents.supervisor import supervisor_agent

    nodes = set(supervisor_agent.get_graph().nodes)
    assert "supervisor" in nodes
    assert "supervisor_tools" in nodes
    assert "red_team" not in nodes
    assert "draft_evaluator" not in nodes
    assert "draft_refiner" not in nodes


def test_supervisor_completion_ends_without_back_edge():
    """ResearchComplete is the sole Supervisor exit; no unconditional return."""
    from deep_research.agents.supervisor import supervisor_agent

    graph = supervisor_agent.get_graph()
    static_edges = {(edge.source, edge.target) for edge in graph.edges
                    if not getattr(edge, "conditional", False)}
    assert ("supervisor_tools", "supervisor") not in static_edges
    assert ("supervisor", "supervisor_tools") not in static_edges
