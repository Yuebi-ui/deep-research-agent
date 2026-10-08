"""Phase 5B：reject/regenerate 回路的 LangGraph 语义回归锁定。

这些不变量来自 `scripts/experiments/phase5b_topology_spike.py` 在真实
langgraph 1.2.12 上的实测结果。最小拓扑与生产 wiring 同构：

    fan → {draft, research} → AND-join → review
    review --revise(Command)--> regen → review      （独立回边）
    review --approve(Command)--> downstream

锁定四件事：

1. join 入边 + 独立回边指向同一目标：第一轮只触发一次；之后每轮 regen
   完成**恰好再触发一次** review（不重复、不停滞）；
2. 每轮等待时 `snapshot.next == ("review",)`（runtime 的 resume 判定依赖它）；
3. draft/research 分支**绝不**因回环被重复执行；
4. 节点内 inline `subgraph.ainvoke()`：每次调用状态干净（不继承上一次的
   reducer 累积），且内层节点事件出现在外层事件流（UI/观测保留）。
"""

from __future__ import annotations

import operator

from langchain_core.callbacks import AsyncCallbackHandler
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import Annotated, TypedDict


class S(TypedDict):
    ev: Annotated[list[str], operator.add]
    gen: int


async def _fan(s: S) -> dict:
    return {"ev": ["fan"], "gen": 1}


async def _draft(s: S) -> dict:
    return {"ev": ["draft"]}


async def _research(s: S) -> dict:
    return {"ev": ["research"]}


async def _review(s: S) -> Command:
    decision = interrupt({"q": "review"})
    action = decision.get("action") if isinstance(decision, dict) else str(decision)
    if action == "revise":
        return Command(goto="regen", update={"ev": ["review_revise"]})
    return Command(goto="downstream", update={"ev": ["review_approve"]})


async def _regen(s: S) -> dict:
    return {"ev": ["regen"], "gen": s.get("gen", 1) + 1}


async def _downstream(s: S) -> dict:
    return {"ev": ["downstream"]}


def _cycle_graph():
    b = StateGraph(S)
    b.add_node("fan", _fan)
    b.add_node("draft", _draft)
    b.add_node("research", _research)
    b.add_node("review", _review)
    b.add_node("regen", _regen)
    b.add_node("downstream", _downstream)
    b.add_edge(START, "fan")
    b.add_edge("fan", "draft")
    b.add_edge("fan", "research")
    b.add_edge(["draft", "research"], "review")   # AND-join
    b.add_edge("regen", "review")                 # 独立回边（唯一来源 regen）
    b.add_edge("downstream", END)
    # review 不得有静态出边（静态边会覆盖 Command(goto)，见 test_graph_wiring）
    return b.compile(checkpointer=InMemorySaver())


async def _drain(graph, inp, cfg) -> None:
    async for _ in graph.astream(inp, config=cfg, stream_mode="updates"):
        pass


async def test_join_plus_back_edge_fires_review_once_per_round():
    graph = _cycle_graph()
    cfg = {"configurable": {"thread_id": "p5b-cycle"}, "recursion_limit": 60}

    await _drain(graph, {"ev": [], "gen": 0}, cfg)
    st = await graph.aget_state(cfg)
    assert st.next == ("review",), f"第一轮未停在 review: {st.next}"
    assert st.values.get("gen") == 1

    # 连续两次 revise：每轮 regen 恰好再触发一次 review
    for expected_gen in (2, 3):
        await _drain(graph, Command(resume={"action": "revise"}), cfg)
        st = await graph.aget_state(cfg)
        assert st.next == ("review",), f"revise 后未回到 review: {st.next}"
        assert st.values.get("gen") == expected_gen

    await _drain(graph, Command(resume={"action": "approve"}), cfg)
    st = await graph.aget_state(cfg)

    ev = list(st.values["ev"])
    assert ev.count("draft") == 1, f"draft 分支被重复执行: {ev}"
    assert ev.count("research") == 1, f"research 分支被重复执行: {ev}"
    assert ev.count("regen") == 2 and ev.count("review_revise") == 2, ev
    assert ev.count("review_approve") == 1 and ev.count("downstream") == 1, ev
    assert tuple(st.next) == ()


# ===== 节点内 inline subgraph.ainvoke（regenerate_research 的实现方式）=====


class Inner(TypedDict):
    seed: str
    log: Annotated[list[str], operator.add]


class Outer(TypedDict):
    log: Annotated[list[str], operator.add]


async def _inner_node(s: Inner) -> dict:
    return {"log": ["inner:" + s.get("seed", "")]}


def _inner_graph():
    b = StateGraph(Inner)
    b.add_node("inner_node", _inner_node)
    b.add_edge(START, "inner_node")
    b.add_edge("inner_node", END)
    return b.compile()  # 无 checkpointer —— 与 supervisor_agent 相同


class _Collector(AsyncCallbackHandler):
    def __init__(self) -> None:
        self.names: list[str] = []

    async def on_chain_start(self, serialized, inputs, **kw):
        name = (serialized or {}).get("name") or kw.get("name") or ""
        self.names.append(str(name))


async def test_inline_subgraph_is_fresh_every_call_and_emits_events():
    inner = _inner_graph()

    async def wrapper(s: Outer, config) -> dict:
        child_cfg = {"callbacks": (config or {}).get("callbacks"), "recursion_limit": 30}
        out1 = await inner.ainvoke({"seed": "A", "log": []}, config=child_cfg)
        out2 = await inner.ainvoke({"seed": "A", "log": []}, config=child_cfg)
        return {"log": [f"{out1['log']}", f"{out2['log']}"]}

    b = StateGraph(Outer)
    b.add_node("wrapper", wrapper)
    b.add_edge(START, "wrapper")
    b.add_edge("wrapper", END)
    graph = b.compile(checkpointer=InMemorySaver())

    collector = _Collector()
    out = await graph.ainvoke(
        {"log": []},
        config={"configurable": {"thread_id": "p5b-inline"}, "callbacks": [collector]},
    )

    # 每次调用从显式输入起步，绝不继承上一次的 reducer 累积
    assert out["log"] == ["['inner:A']", "['inner:A']"], out["log"]
    # 内层节点事件必须出现在外层事件流（UI/观测/成本归因依赖它）
    assert collector.names.count("inner_node") == 2, collector.names
