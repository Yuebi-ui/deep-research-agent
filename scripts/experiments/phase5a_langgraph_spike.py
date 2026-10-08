#!/usr/bin/env python
"""Phase 5A spike：LangGraph 并行分支 + interrupt/HITL 的真实运行时语义（isolated）。

**不 import 任何生产 graph**：只用生产同款的 langgraph 版本（1.2.12）、
interrupt/Command API 与 checkpointer 后端（redis / AsyncRedisSaver），
在独立 thread_id 上跑最小拓扑，用可观测的假节点（asyncio.sleep）回答：

  A. T1（join 之后再 HITL）—— 两个分支是否真并发、join 的 state merge 是否安全、
     resume 是否重放已完成分支；
  B. T2（HITL 在 draft 分支内、与 research 并行）—— 一个分支 interrupt 时，
     另一个仍在运行的分支是被取消、被等待、还是继续跑完并 checkpoint。

    ALLOW_LIVE_EXTERNAL_APIS=true .venv/bin/python scripts/experiments/phase5a_langgraph_spike.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from langgraph.graph import StateGraph, START, END  # noqa: E402
from langgraph.types import Command, interrupt  # noqa: E402
from typing_extensions import Annotated, TypedDict  # noqa: E402
import operator  # noqa: E402

TRACE: list[dict] = []
RUNS: dict[str, int] = {}
RUN_TAG: str = "spike"


def _log(node: str, phase: str, **extra) -> None:
    TRACE.append({"node": node, "phase": phase, "t": round(time.monotonic(), 3), **extra})


def _bump(name: str) -> int:
    RUNS[name] = RUNS.get(name, 0) + 1
    return RUNS[name]


class SpikeState(TypedDict):
    notes: Annotated[list[str], operator.add]
    draft: str
    events: Annotated[list[str], operator.add]
    approved: bool


# ===== 假节点（可观测、可计时）=====


async def research_node(state: SpikeState) -> dict:
    n = _bump("research")
    _log("research", "start", run=n)
    await asyncio.sleep(2.0)          # 模拟 research 主链
    _log("research", "end", run=n)
    return {"notes": [f"notes-{n}"], "events": ["research_done"]}


async def draft_node(state: SpikeState) -> dict:
    n = _bump("draft")
    _log("draft", "start", run=n)
    await asyncio.sleep(0.5)          # 模拟 draft（比 research 短）
    _log("draft", "end", run=n)
    return {"draft": f"draft-{n}", "events": ["draft_done"]}


async def join_node(state: SpikeState) -> dict:
    _log("join", "enter", notes=len(state.get("notes", [])), draft=bool(state.get("draft")))
    return {"events": ["joined"]}


async def hitl_node(state: SpikeState) -> dict:
    """HITL：静态边 hitl → downstream（**不用 Command(goto)**，避免自环）。"""
    n = _bump("hitl")
    _log("hitl", "before_interrupt", run=n)
    decision = interrupt({"message": "review", "draft": state.get("draft", "")})
    action = decision.get("action") if isinstance(decision, dict) else str(decision)
    _log("hitl", "after_resume", run=n, decision=str(action))
    return {
        "approved": action == "approve",
        "events": [f"hitl_{action}"],
        # reject 时尝试「清空投机研究结果」—— 用于验证 add 型 reducer 的可覆盖性
        "notes": [] if action != "approve" else state.get("notes", []),
    }


async def hitl_immediate(state: SpikeState) -> dict:
    """与长任务同 superstep 的 HITL：用来验证 interrupt 是否取消兄弟节点。"""
    _bump("hitl_immediate")
    _log("hitl_immediate", "before_interrupt")
    decision = interrupt({"message": "same-superstep review"})
    _log("hitl_immediate", "after_resume", decision=str(decision))
    return {"events": ["hitl_immediate_done"]}


async def downstream_node(state: SpikeState) -> dict:
    _log("downstream", "enter", notes=len(state.get("notes", [])), approved=state.get("approved"))
    return {"events": ["downstream_done"]}


def build_graph_a(saver):
    """T1：join 之后再 HITL（HITL 与并行分支不在同一 superstep）。"""
    b = StateGraph(SpikeState)
    b.add_node("research", research_node)
    b.add_node("draft", draft_node)
    b.add_node("join", join_node)
    b.add_node("hitl", hitl_node)
    b.add_node("downstream", downstream_node)
    b.add_edge(START, "research")
    b.add_edge(START, "draft")
    b.add_edge("research", "join")
    b.add_edge("draft", "join")
    b.add_edge("join", "hitl")
    b.add_edge("hitl", "downstream")
    b.add_edge("downstream", END)
    return b.compile(checkpointer=saver)


def build_graph_b(saver):
    """T2：HITL 在 draft 分支内，与 research 并行（高风险拓扑）。"""
    b = StateGraph(SpikeState)
    b.add_node("research", research_node)
    b.add_node("draft", draft_node)
    b.add_node("hitl", hitl_node)
    b.add_node("join", join_node)
    b.add_edge(START, "research")
    b.add_edge(START, "draft")
    b.add_edge("draft", "hitl")
    b.add_edge("research", "join")
    b.add_edge("hitl", "join")
    b.add_edge("join", END)
    return b.compile(checkpointer=saver)


def build_graph_d(saver):
    """D：interrupt 与 2s 长任务在**同一个 superstep**（无 join）。"""
    b = StateGraph(SpikeState)
    b.add_node("research", research_node)
    b.add_node("hitl_immediate", hitl_immediate)
    b.add_edge(START, "research")
    b.add_edge(START, "hitl_immediate")
    b.add_edge("research", END)
    b.add_edge("hitl_immediate", END)
    return b.compile(checkpointer=saver)


async def _run_case(name: str, graph, thread_id: str, *, resume: str | None = None) -> dict:
    global TRACE, RUNS
    TRACE, RUNS = [], {}
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 20}
    t0 = time.monotonic()
    try:
        if resume:
            out = await graph.ainvoke(Command(resume={"action": resume}), config=cfg)
        else:
            out = await graph.ainvoke({"notes": [], "events": []}, config=cfg)
        error = None
    except Exception as exc:  # noqa: BLE001
        out, error = None, f"{type(exc).__name__}: {exc}"
    elapsed = round(time.monotonic() - t0, 2)
    interrupted = bool(out and out.get("__interrupt__"))
    return {
        "case": name, "thread_id": thread_id, "run_tag": RUN_TAG, "elapsed_s": elapsed,
        "interrupted": interrupted, "error": error,
        "node_runs": dict(RUNS),
        "trace": list(TRACE),
        "final": {k: v for k, v in (out or {}).items() if k != "__interrupt__"} if out else None,
    }


def _overlap(trace: list[dict], a: str, b: str) -> float | None:
    """两个节点执行区间的重叠秒数。"""
    def span(node):
        starts = [e["t"] for e in trace if e["node"] == node and e["phase"] == "start"]
        ends = [e["t"] for e in trace if e["node"] == node and e["phase"] == "end"]
        return (min(starts), max(ends)) if starts and ends else None
    sa, sb = span(a), span(b)
    if not sa or not sb:
        return None
    return round(max(0.0, min(sa[1], sb[1]) - max(sa[0], sb[0])), 3)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "artifacts" / "phase5a" / "spike.json")
    parser.add_argument("--backend", default="redis", choices=["redis", "sqlite"])
    args = parser.parse_args(argv)

    import deep_research.checkpoint as ckpt

    # 每次 spike 用全新 thread_id：否则会**续跑上一次残留的 checkpoint**，
    # 导致 state 被重复 apply（第一次踩到的坑，已固化为经验）
    global RUN_TAG
    run_tag = f"spike-{int(time.time())}"
    RUN_TAG = run_tag
    managed = await ckpt.create_checkpointer(args.backend)
    saver = managed.saver
    results: list[dict] = []
    try:
        # ---------- A：T1，join 之后 HITL ----------
        g_a = build_graph_a(saver)
        first = await _run_case("A1 (T1 first run: brief→{research∥draft}→join→HITL)",
                                g_a, f"{run_tag}-a1")
        results.append(first)
        resumed = await _run_case("A2 (T1 resume with approve)",
                                  g_a, f"{run_tag}-a1", resume="approve")
        results.append(resumed)

        # ---------- A3：reject 路径（验证 add 型 reducer 能否被清空）----------
        g_a3 = build_graph_a(saver)
        r1 = await _run_case("A3a (T1 first run, will reject)",
                             g_a3, f"{run_tag}-a3")
        results.append(r1)
        r2 = await _run_case("A3b (T1 resume with reject)",
                             g_a3, f"{run_tag}-a3", resume="reject")
        results.append(r2)

        # ---------- B：T2，HITL 与 research 并行 ----------
        g_b = build_graph_b(saver)
        b1 = await _run_case("B1 (T2 first run: {research ∥ draft→HITL}→join)",
                             g_b, f"{run_tag}-b1")
        results.append(b1)
        b2 = await _run_case("B2 (T2 resume with approve)", g_b, f"{run_tag}-b1", resume="approve")
        results.append(b2)

        # ---------- D：interrupt 与长任务同 superstep ----------
        g_d = build_graph_d(saver)
        d1 = await _run_case("D1 (interrupt in same superstep as 2s sibling)",
                             g_d, f"{run_tag}-d1")
        results.append(d1)
        d2 = await _run_case("D2 (resume after same-superstep interrupt)",
                             g_d, f"{run_tag}-d1", resume="approve")
        results.append(d2)

        # ---------- C：进程重启语义（独立 thread：先跑到 interrupt，再换 checkpointer 实例 resume）----------
        g_c0 = build_graph_a(saver)
        c0 = await _run_case("C0 (T1 first run, will restart before resume)",
                             g_c0, f"{run_tag}-c1")
        results.append(c0)
        await managed.aclose()
        managed = await ckpt.create_checkpointer(args.backend)
        g_a2 = build_graph_a(managed.saver)
        c1 = await _run_case("C1 (T1 resume after checkpointer restart)",
                             g_a2, f"{run_tag}-c1", resume="approve")
        results.append(c1)

        # ---------- D：A 拓扑的 state merge / 重放计数 ----------
        summary = {
            "langgraph_version": _lg_version(),
            "backend": args.backend,
            "A_overlap_research_draft_s": _overlap(first["trace"], "research", "draft"),
            "A_first_run_node_runs": first["node_runs"],
            "A_resume_node_runs": resumed["node_runs"],
            "B_first_run_node_runs": b1["node_runs"],
            "B_resume_node_runs": b2["node_runs"],
            "B_first_run_states": _node_phases(b1["trace"]),
            "A_final_state": first["final"],
            "A_resume_final_state": resumed["final"],
            "B_resume_final_state": b2["final"],
            "C_resume_node_runs": c1["node_runs"],
            "C_restart_resume_interrupted": c1["interrupted"],
            "C_restart_resume_state": c1["final"],
            "D_same_superstep_node_runs": d1["node_runs"],
            "D_same_superstep_states": _node_phases(d1["trace"]),
            "D_resume_node_runs": d2["node_runs"],
            "A3_reject_first_state": r1["final"],
            "A3_reject_resume_state": r2["final"],
            "A3_reject_resume_node_runs": r2["node_runs"],
            "A3_reject_resume_interrupted": r2["interrupted"],
        }
    finally:
        try:
            await managed.aclose()
        except Exception:
            pass

    print(json.dumps(summary, ensure_ascii=False, indent=1))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "cases": results},
                                   ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"-> {args.out}")
    return 0


def _node_phases(trace: list[dict]) -> dict:
    """每个节点出现过哪些 phase（用于判断 interrupt 时另一分支是否跑完）。"""
    out: dict[str, list[str]] = {}
    for e in trace:
        out.setdefault(e["node"], []).append(e["phase"])
    return out


def _lg_version() -> str:
    from importlib.metadata import version

    return version("langgraph")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
