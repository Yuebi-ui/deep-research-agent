"""Phase 5A：LangGraph 并行分支 / interrupt / reducer 语义的回归锁定。

这些不变量来自 `scripts/experiments/phase5a_langgraph_spike.py` 在真实
langgraph 1.2.12 + 真实项目 checkpointer 后端上的实测结果。用 InMemorySaver
跑同样的最小拓扑（语义属于框架层，与 saver 实现无关），把结论钉住：

1. 同一 superstep 的分支**真正并发**；
2. 早早就绪的节点**仍要等整个 superstep 结束**（T2 拓扑没有收益）；
3. **interrupt 不会取消同 superstep 的兄弟节点**——它会跑完并被 checkpoint；
4. resume 只重跑被中断的节点，**已完成分支不重放**（不产生重复副作用）；
5. `operator.add` 型 reducer **无法用 `[]` 清空**，回显自身 state 会**翻倍**。
"""

from __future__ import annotations

import asyncio
import operator
import time

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import Annotated, TypedDict

# 缩短的时间常量（语义与真实时长无关）
LONG = 0.30
SHORT = 0.05


class S(TypedDict):
    notes: Annotated[list[str], operator.add]
    draft: str
    events: Annotated[list[str], operator.add]
    runs: Annotated[list[str], operator.add]


def _bump(runs: list[str], name: str) -> list[str]:
    return runs + [name]


async def _long(state: S) -> dict:
    t0 = time.monotonic()
    await asyncio.sleep(LONG)
    return {"notes": ["research-notes"], "events": [f"research_done@{t0:.2f}"]}


async def _short(state: S) -> dict:
    await asyncio.sleep(SHORT)
    return {"draft": "draft-1", "events": ["draft_done"]}


async def _hitl(state: S) -> dict:
    decision = interrupt({"question": "review"})
    action = decision.get("action") if isinstance(decision, dict) else str(decision)
    return {"events": [f"hitl_{action}"], "runs": _bump([], "hitl")}


def _graph_join_then_hitl():
    b = StateGraph(S)
    b.add_node("research", _long)
    b.add_node("draft", _short)
    b.add_node("hitl", _hitl)
    b.add_edge(START, "research")
    b.add_edge(START, "draft")
    b.add_edge("research", "hitl")
    b.add_edge("draft", "hitl")
    b.add_edge("hitl", END)
    return b.compile(checkpointer=InMemorySaver())


def _graph_hitl_parallel_with_research():
    """T2：draft → HITL 与 research 并行（两条边汇聚到 END）。"""
    b = StateGraph(S)
    b.add_node("research", _long)
    b.add_node("draft", _short)
    b.add_node("hitl", _hitl)
    b.add_edge(START, "research")
    b.add_edge(START, "draft")
    b.add_edge("draft", "hitl")
    b.add_edge("research", END)
    b.add_edge("hitl", END)
    return b.compile(checkpointer=InMemorySaver())


async def test_parallel_branches_overlap_in_one_superstep():
    """两分支在同一 superstep 并发：总时长 ≈ max(分支)，不是 sum。"""
    graph = _graph_join_then_hitl()
    t0 = time.monotonic()
    out = await graph.ainvoke({"notes": [], "events": [], "runs": []},
                              config={"configurable": {"thread_id": "t-overlap"}})
    elapsed = time.monotonic() - t0

    assert out["__interrupt__"], "应停在 HITL"
    assert elapsed < LONG + SHORT, f"分支未并发（{elapsed:.2f}s ≥ {LONG + SHORT:.2f}s）"
    assert out["draft"] == "draft-1" and out["notes"] == ["research-notes"], "两分支结果都要合并"


async def test_hitl_cannot_fire_before_superstep_finishes():
    """T2 拓扑：draft 早早结束，但 HITL 仍要等 research 所在 superstep 跑完。

    这就是"把 HITL 放进并行分支"没有收益的运行时原因。
    """
    graph = _graph_hitl_parallel_with_research()
    t0 = time.monotonic()
    out = await graph.ainvoke({"notes": [], "events": [], "runs": []},
                              config={"configurable": {"thread_id": "t-t2"}})
    elapsed = time.monotonic() - t0
    assert out["__interrupt__"]
    assert elapsed >= LONG * 0.9, (
        f"HITL 在 research 结束前就中断了（{elapsed:.2f}s）—— 与实测语义不符"
    )


async def test_interrupt_does_not_cancel_inflight_sibling():
    """interrupt 与长任务同 superstep：兄弟节点**跑完并落 checkpoint**，不被取消。"""
    b = StateGraph(S)
    b.add_node("research", _long)
    b.add_node("hitl", _hitl)
    b.add_edge(START, "research")
    b.add_edge(START, "hitl")
    b.add_edge("research", END)
    b.add_edge("hitl", END)
    graph = b.compile(checkpointer=InMemorySaver())

    t0 = time.monotonic()
    out = await graph.ainvoke({"notes": [], "events": [], "runs": []},
                              config={"configurable": {"thread_id": "t-d1"}})
    elapsed = time.monotonic() - t0

    assert out["__interrupt__"]
    assert elapsed >= LONG * 0.9, "调用方在兄弟节点跑完前就拿到控制权了"
    assert out["notes"] == ["research-notes"], "兄弟节点结果必须落 checkpoint"


async def test_resume_reruns_only_interrupted_node():
    """resume 只重跑被中断的节点；已完成分支不重放（无重复副作用）。"""
    graph = _graph_join_then_hitl()
    cfg = {"configurable": {"thread_id": "t-resume"}}
    first = await graph.ainvoke({"notes": [], "events": [], "runs": []}, config=cfg)
    assert first["__interrupt__"]

    out = await graph.ainvoke(Command(resume={"action": "approve"}), config=cfg)

    assert out["runs"] == ["hitl"], "只有 hitl 被重跑（research/draft 不得重放）"
    assert out["notes"] == ["research-notes"], "research 结果不得翻倍"


async def test_two_plain_edges_are_or_not_join():
    """⚠️ 关键语义：两次单独的 add_edge(a,c)/add_edge(b,c) **不是 join**。

    它按"每条入边各触发一次"执行（c 会被跑多次，且第一次触发不等另一分支）。
    真实项目里踩到过：human_review 在 draft 完成后立刻触发、与 research 并行，
    且 resume 时把仍在 pending 的研究分支**又跑了一遍**（重复副作用）。
    """
    class JS(TypedDict):
        ev: Annotated[list[str], operator.add]

    async def fast(s):
        await asyncio.sleep(SHORT)
        return {"ev": ["fast"]}

    async def slow(s):
        await asyncio.sleep(LONG)
        return {"ev": ["slow"]}

    async def mid(s):
        await asyncio.sleep(SHORT)
        return {"ev": ["mid"]}

    async def sink(s):
        return {"ev": ["sink"]}

    def _graph(joined: bool):
        b = StateGraph(JS)
        for n, f in (("fast", fast), ("slow", slow), ("mid", mid), ("sink", sink)):
            b.add_node(n, f)
        b.add_edge(START, "fast")
        b.add_edge("fast", "mid")
        b.add_edge(START, "slow")
        if joined:
            b.add_edge(["mid", "slow"], "sink")      # AND-join
        else:
            b.add_edge("mid", "sink")
            b.add_edge("slow", "sink")               # OR / 多次触发
        b.add_edge("sink", END)
        return b.compile(checkpointer=InMemorySaver())

    or_out = await _graph(False).ainvoke({"ev": []},
                                         config={"configurable": {"thread_id": "j-or"}})
    and_out = await _graph(True).ainvoke({"ev": []},
                                         config={"configurable": {"thread_id": "j-and"}})

    assert or_out["ev"].count("sink") == 2, f"两条边应触发两次：{or_out['ev']}"
    assert and_out["ev"].count("sink") == 1, f"列表形式应只触发一次：{and_out['ev']}"


async def test_additive_reducer_cannot_be_cleared_and_echo_doubles():
    """`operator.add` reducer：返回 [] 不能清空；回显自身 state 会翻倍。"""
    b = StateGraph(S)
    b.add_node("seed", lambda s: {"notes": ["n1"]})
    b.add_node("echo", lambda s: {"notes": s.get("notes", [])})   # 回显
    b.add_node("clear", lambda s: {"notes": []})                  # 试图清空
    b.add_edge(START, "seed")
    b.add_edge("seed", "echo")
    b.add_edge("echo", "clear")
    b.add_edge("clear", END)
    graph = b.compile(checkpointer=InMemorySaver())

    out = await graph.ainvoke({"notes": [], "events": [], "runs": []},
                              config={"configurable": {"thread_id": "t-reducer"}})
    assert out["notes"] == ["n1", "n1"], (
        "回显应导致翻倍、清空应无效 —— 这是 add reducer 的语义，"
        "投机结果因此必须放在独立字段里"
    )
