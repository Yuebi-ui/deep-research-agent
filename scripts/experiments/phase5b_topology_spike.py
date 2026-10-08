#!/usr/bin/env python
"""Phase 5B 拓扑 spike —— 在真实 langgraph 1.2.12 上钉死 productionize 需要的运行时事实。

要回答的问题（全部以**可执行实测**为准，不查文档不猜）：

S1. 目标节点同时有 (a) AND-join 入边 add_edge([draft, research], review)
    和 (b) 独立回边 add_edge(regen, review) 时：
      - 第一轮 review 仍只触发一次？
      - review 走 Command(goto="regen") → regen 完成 → review **会再次触发**吗？
      - 连续多轮 revise 是否每次都恰好再触发一次（不重复、不停滞）？
      - draft/research 分支是否**不会**被重复执行？

S2. 在 S1 拓扑上 interrupt / resume：
      - 每次停在 review 时 snapshot.next == ("review",)（runner 兼容的前提）？
      - revise → regen → 第二次 interrupt；approve → downstream，全程正确？

S3. 节点内 inline await subgraph.ainvoke()（未来 regenerate 节点的实现方式）：
      - 每次调用是否**干净**（无内部状态残留）？
      - 内层节点事件是否出现在外层 astream_events 流里（UI/观测）？
      - 外层 config 的 callbacks 如何传给内层（显式传递 vs 上下文继承）？
      - 内层不会写进外层 checkpointer？

S4. 真实 AsyncRedisSaver 上，能否在构建图**之前**从 checkpoint 原始
    channel_values 读到 lineage 标记（runner 用它决定用哪套拓扑 resume）？

用法：.venv/bin/python scripts/experiments/phase5b_topology_spike.py
（纯本地；Redis 用 db 1，不碰生产 db 0；不需要任何 LLM）
"""

from __future__ import annotations

import asyncio
import json
import operator
import time
from typing import Any, Annotated

from langchain_core.callbacks import AsyncCallbackHandler
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

RESULTS: dict[str, Any] = {}

REDIS_URL = "redis://127.0.0.1:6379/1"


# ============================================================
# S1 + S2：join + 独立回边，指向同一个 review 目标
# ============================================================


class S(TypedDict):
    ev: Annotated[list[str], operator.add]
    gen: int


async def fan(s: S) -> dict:
    return {"ev": ["fan"], "gen": 1}


async def draft_branch(s: S) -> dict:
    await asyncio.sleep(0.05)
    return {"ev": ["draft"]}


async def research_branch(s: S) -> dict:
    await asyncio.sleep(0.05)
    return {"ev": ["research"]}


async def review(s: S) -> Command:
    decision = interrupt({"q": "review", "gen": s.get("gen")})
    action = decision.get("action")
    if action == "revise":
        # 真实实现里这里先改写草稿，再 goto regenerate 节点
        return Command(goto="regen", update={"ev": ["review_revise"]})
    return Command(goto="downstream", update={"ev": ["review_approve"]})


async def regen(s: S) -> dict:
    await asyncio.sleep(0.05)
    return {"ev": ["regen"], "gen": s.get("gen", 1) + 1}


async def downstream(s: S) -> dict:
    return {"ev": ["downstream"]}


def build_cycle_graph():
    b = StateGraph(S)
    b.add_node("fan", fan)
    b.add_node("draft", draft_branch)
    b.add_node("research", research_branch)
    b.add_node("review", review)
    b.add_node("regen", regen)
    b.add_node("downstream", downstream)

    b.add_edge(START, "fan")
    b.add_edge("fan", "draft")
    b.add_edge("fan", "research")
    b.add_edge(["draft", "research"], "review")   # AND-join
    b.add_edge("regen", "review")                 # 独立回边（回边只有这一个来源）
    b.add_edge("downstream", END)
    # 注意：review 没有任何**静态出边**（全部走 Command(goto)），
    # 否则静态边会覆盖 Command（tests/test_graph_wiring.py 钉过的语义）。
    return b.compile(checkpointer=InMemorySaver())


async def s1_s2_cycle() -> None:
    graph = build_cycle_graph()
    cfg = {"configurable": {"thread_id": "p5b-cycle"}, "recursion_limit": 60}

    async def drain(inp) -> None:
        async for _ in graph.astream(inp, config=cfg, stream_mode="updates"):
            pass

    # 第一轮：跑到第一个 review interrupt
    await drain({"ev": [], "gen": 0})
    st = await graph.aget_state(cfg)
    rounds = [{"phase": "initial", "next": tuple(st.next), "ev": list(st.values["ev"]),
               "gen": st.values.get("gen")}]

    # revise → regen → 第二个 interrupt
    await drain(Command(resume={"action": "revise"}))
    st = await graph.aget_state(cfg)
    rounds.append({"phase": "revise1", "next": tuple(st.next), "ev": list(st.values["ev"]),
                   "gen": st.values.get("gen")})

    # 再 revise → 第三个 interrupt
    await drain(Command(resume={"action": "revise"}))
    st = await graph.aget_state(cfg)
    rounds.append({"phase": "revise2", "next": tuple(st.next), "ev": list(st.values["ev"]),
                   "gen": st.values.get("gen")})

    # approve → downstream → END
    await drain(Command(resume={"action": "approve"}))
    st = await graph.aget_state(cfg)
    rounds.append({"phase": "approve", "next": tuple(st.next), "ev": list(st.values["ev"]),
                   "gen": st.values.get("gen")})

    ev = list(st.values["ev"])
    RESULTS["S1S2"] = {
        "rounds": rounds,
        "counts": {
            "draft": ev.count("draft"),
            "research": ev.count("research"),
            "review_revise": ev.count("review_revise"),
            "review_approve": ev.count("review_approve"),
            "regen": ev.count("regen"),
            "downstream": ev.count("downstream"),
        },
        "checks": {
            "next_is_review_at_each_wait": all(r["next"] == ("review",) for r in rounds[:3]),
            "branch_not_rerun": ev.count("draft") == 1 and ev.count("research") == 1,
            "review_fired_once_per_round": (
                ev.count("review_revise") == 2 and ev.count("review_approve") == 1
            ),
            "regen_ran_twice": ev.count("regen") == 2,
            "downstream_ran_once": ev.count("downstream") == 1,
            "ended": tuple(st.next) == (),
        },
    }


# ============================================================
# S3：节点内 inline subgraph.ainvoke()
# ============================================================


class Inner(TypedDict):
    seed: str
    log: Annotated[list[str], operator.add]


class Outer(TypedDict):
    log: Annotated[list[str], operator.add]


async def inner_node(s: Inner) -> dict:
    return {"log": ["inner:" + s.get("seed", "")]}


def build_inner():
    b = StateGraph(Inner)
    b.add_node("inner_node", inner_node)
    b.add_edge(START, "inner_node")
    b.add_edge("inner_node", END)
    return b.compile()  # 刻意不带 checkpointer（与 supervisor_agent 相同）


class Collector(AsyncCallbackHandler):
    def __init__(self) -> None:
        self.started: list[tuple[str, str | None]] = []

    async def on_chain_start(self, serialized, inputs, *, run_id=None, parent_run_id=None, **kw):
        name = (serialized or {}).get("name") or kw.get("name") or ""
        self.started.append((str(name), str(parent_run_id) if parent_run_id else None))


async def s3_wrapper() -> None:
    inner = build_inner()

    async def wrapper(s: Outer, config) -> dict:
        # 模拟 regenerate 节点的调用方式：显式传 callbacks，剥离 configurable
        child_cfg = {
            "callbacks": (config or {}).get("callbacks"),
            "recursion_limit": 30,
        }
        out1 = await inner.ainvoke({"seed": "A", "log": []}, config=child_cfg)
        out2 = await inner.ainvoke({"seed": "A", "log": []}, config=child_cfg)
        return {"log": [f"wrapper1={out1['log']}", f"wrapper2={out2['log']}"]}

    b = StateGraph(Outer)
    b.add_node("wrapper", wrapper)
    b.add_edge(START, "wrapper")
    b.add_edge("wrapper", END)
    graph = b.compile(checkpointer=InMemorySaver())

    collector = Collector()
    cfg = {"configurable": {"thread_id": "p5b-wrapper"}, "callbacks": [collector]}
    out = await graph.ainvoke({"log": []}, config=cfg)

    inner_events = [name for name, _ in collector.started if name == "inner_node"]
    RESULTS["S3"] = {
        "output": out["log"],
        "fresh_state": out["log"][0] == "wrapper1=['inner:A']" and out["log"][1] == "wrapper2=['inner:A']",
        "inner_events_seen_by_parent_callback": len(inner_events),
        "inner_events_have_parent_run": any(
            p for name, p in collector.started if name == "inner_node"
        ),
    }


# ============================================================
# S4：checkpoint 原始 channel_values 读取（runner 的 lineage 标记判据）
# ============================================================


class L(TypedDict):
    research_generation: int
    payload: dict


async def s4_redis_marker() -> None:
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    saver = AsyncRedisSaver(redis_url=REDIS_URL)
    await saver.__aenter__()
    await saver.asetup()
    try:
        b = StateGraph(L)

        async def seed_marker(s: L) -> dict:
            return {"research_generation": 2, "payload": {"generation": 2}}

        b.add_node("seed_marker", seed_marker)
        b.add_edge(START, "seed_marker")
        b.add_edge("seed_marker", END)
        graph = b.compile(checkpointer=saver)
        cfg = {"configurable": {"thread_id": "p5b-marker-probe"}}
        await graph.ainvoke({"research_generation": 0, "payload": {}}, config=cfg)

        tup = await saver.aget_tuple(cfg)
        cv = (tup.checkpoint or {}).get("channel_values", {}) if tup else {}
        fresh = await saver.aget_tuple({"configurable": {"thread_id": "p5b-marker-missing"}})

        RESULTS["S4"] = {
            "marker_visible": cv.get("research_generation"),
            "payload_visible": cv.get("payload"),
            "missing_thread_is_none": fresh is None,
            "channel_keys": sorted(cv.keys()),
        }
    finally:
        await saver.__aexit__(None, None, None)


# ============================================================
# S5：update_state 注入"late stale"结果（真图 fence 测试的基础设施验证）
# ============================================================


class F(TypedDict):
    active_gen: int
    payload: dict
    log: Annotated[list[str], operator.add]


async def f_review(s: F) -> Command:
    decision = interrupt("review")
    if decision.get("action") == "inject_then_off":
        return Command(goto=END, update={"log": ["off"]})
    active = s.get("active_gen", 0)
    got = (s.get("payload") or {}).get("generation")
    if got != active:
        raise RuntimeError(f"FENCE_MISMATCH active={active} payload={got}")
    return Command(goto=END, update={"log": ["accepted"]})


def build_fence_probe():
    b = StateGraph(F)
    b.add_node("review", f_review)
    b.add_edge(START, "review")
    b.add_edge("review", END)
    return b.compile(checkpointer=InMemorySaver())


async def s5_late_stale() -> None:
    graph = build_fence_probe()
    cfg = {"configurable": {"thread_id": "p5b-fence"}}

    async def drain(inp) -> None:
        async for _ in graph.astream(inp, config=cfg, stream_mode="updates"):
            pass

    await drain({"active_gen": 2, "payload": {"generation": 2}, "log": []})
    # 注入晚到的 stale 结果（generation 1 覆盖 generation 2）—— 模拟污染
    await graph.aupdate_state(cfg, {"payload": {"generation": 1}})
    st = await graph.aget_state(cfg)
    injected = (st.values.get("payload") or {}).get("generation")

    # resume approve：fence 应当拒绝（抛 RuntimeError → graph 错误）
    error = None
    try:
        await drain(Command(resume={"action": "approve"}))
    except Exception as exc:  # noqa: BLE001
        error = type(exc).__name__

    st2 = await graph.aget_state(cfg)
    RESULTS["S5"] = {
        "injected_generation": injected,
        "mismatch_raised": error,
        "log_after": list(st2.values.get("log", [])),
    }


async def main() -> None:
    t0 = time.monotonic()
    await s1_s2_cycle()
    await s3_wrapper()
    await s4_redis_marker()
    await s5_late_stale()
    RESULTS["elapsed_s"] = round(time.monotonic() - t0, 2)
    print(json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
