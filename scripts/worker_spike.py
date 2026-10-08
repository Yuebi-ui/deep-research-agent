#!/usr/bin/env python
"""Phase F —— Worker Compatibility Spike 驱动（执行包 §26-36）。

回答一个问题：

> 当前 LangGraph + 目标 checkpointer + `astream_events` + HITL
> interrupt/resume，是否能够安全地跨**独立 Worker 进程**恢复？

本脚本刻意以「每次调用都是一个独立进程」的方式工作：stage1 与 stage2
由外层驱动脚本分别以 `subprocess` 启动，因此进程内的内存状态**必然**
丢失——这正是要验证的。

用法：

    python scripts/worker_spike.py stage1 --thread-id <id>   # 启动，停在 human_review
    python scripts/worker_spike.py stage2 --thread-id <id> --action approve
    python scripts/worker_spike.py inspect --thread-id <id>

后端由 CHECKPOINTER_BACKEND 决定（memory / sqlite / redis）。
**用 memory 跑本脚本没有意义**——它无法跨进程存活，脚本会直接拒绝。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# 必须在导入 deep_research 之前设置：多个 agent 模块在 import 期构造模型
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ALLOW_LIVE_EXTERNAL_APIS", "false")

from langchain_core.messages import HumanMessage  # noqa: E402
from langgraph.types import Command  # noqa: E402

from deep_research import checkpoint as checkpoint_factory  # noqa: E402
from deep_research.agent_builder import _create_builder  # noqa: E402


def _config(thread_id: str) -> dict:
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 60,
    }


def _banner(stage: str) -> None:
    """每个进程启动时打印身份信息，便于确认确实是不同进程。"""
    print(json.dumps({
        "stage": stage,
        "pid": os.getpid(),
        "backend": checkpoint_factory.resolve_backend(),
        "sqlite_path": (
            checkpoint_factory.resolve_sqlite_path()
            if checkpoint_factory.resolve_backend() == "sqlite" else None
        ),
    }, ensure_ascii=False))


async def _run(stage: str, thread_id: str, action: str, query: str) -> int:
    backend = checkpoint_factory.resolve_backend()
    if backend == "memory" and stage in ("stage1", "stage2"):
        print(json.dumps({
            "error": "memory 后端无法跨进程存活，用它做本验证没有意义。"
                     "请设置 CHECKPOINTER_BACKEND=sqlite 或 redis。",
        }, ensure_ascii=False))
        return 2

    managed = await checkpoint_factory.init_checkpointer()
    graph = _create_builder(with_hitl=True).compile(checkpointer=managed.saver)
    config = _config(thread_id)

    if stage == "stage1":
        # 首次执行：应停在 human_review。用 astream_events 覆盖 §33 的组合。
        visited = []
        async for event in graph.astream_events(
            {"messages": [HumanMessage(content=query)]}, config=config, version="v2"
        ):
            if event.get("event") == "on_chain_start":
                name = event.get("name")
                if name and name not in visited:
                    visited.append(name)

        snap = await graph.aget_state(config)
        print(json.dumps({
            "stage": "stage1",
            "next": list(snap.next),
            "draft_len": len(snap.values.get("draft_report") or ""),
            "nodes_seen": len(visited),
        }, ensure_ascii=False))
        await checkpoint_factory.reset_checkpointer()
        # 停在 human_review 才算成功
        return 0 if snap.next == ("human_review",) else 1

    if stage == "stage2":
        # 全新进程：先确认能读到上一进程留下的中断状态
        snap_before = await graph.aget_state(config)
        if not snap_before.next:
            print(json.dumps({"stage": "stage2", "error": "未能读到中断状态"}, ensure_ascii=False))
            await checkpoint_factory.reset_checkpointer()
            return 1

        async for _ in graph.astream_events(
            Command(resume={"action": action}), config=config, version="v2"
        ):
            pass

        snap = await graph.aget_state(config)
        report = snap.values.get("final_report")
        print(json.dumps({
            "stage": "stage2",
            "resumed_from": list(snap_before.next),
            "next": list(snap.next),
            "final_report_len": len(report or ""),
            "has_report": bool(report),
        }, ensure_ascii=False))
        await checkpoint_factory.reset_checkpointer()
        return 0 if (not snap.next and report) else 1

    if stage == "inspect":
        snap = await graph.aget_state(config)
        print(json.dumps({
            "stage": "inspect",
            "next": list(snap.next),
            "has_draft": bool(snap.values.get("draft_report")),
            "has_report": bool(snap.values.get("final_report")),
        }, ensure_ascii=False))
        await checkpoint_factory.reset_checkpointer()
        return 0

    return 2


def _orchestrate(backend: str, action: str, query: str) -> int:
    """以独立子进程依次执行 stage1 / stage2，并汇总场景结果。

    子进程 = 真正的「不同 worker 生命周期」；进程内状态必然丢失，
    因此 stage2 若成功，只可能是从持久化 checkpointer 恢复的。
    """
    import subprocess
    import uuid

    thread_id = f"spike-{uuid.uuid4().hex[:8]}"
    env = dict(os.environ, CHECKPOINTER_BACKEND=backend, PYTHONIOENCODING="utf-8")
    me = str(Path(__file__).resolve())

    def run_stage(stage: str) -> tuple[int, dict]:
        proc = subprocess.run(
            [sys.executable, me, stage, "--thread-id", thread_id,
             "--action", action, "--query", query],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
        )
        # 每个 stage 会打印多行 JSON（身份 banner + 结果），合并成一个 payload。
        payload: dict = {}
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("stage") == stage:
                payload.update(data)
        if not payload:
            payload = {"stderr": (proc.stderr or "")[-400:]}
        return proc.returncode, payload

    print("=== Worker Compatibility Spike ===")
    print(f"backend   : {backend}")
    print(f"thread_id : {thread_id}")
    print()

    print("--- Scenario 1 / Stage 1：启动任务并停在 human_review ---")
    rc1, out1 = run_stage("stage1")
    print(f"  pid={out1.get('pid')}  next={out1.get('next')}  "
          f"draft_len={out1.get('draft_len')}  nodes_seen={out1.get('nodes_seen')}")
    print(f"  returncode={rc1}")
    if rc1 != 0:
        print("  ✗ stage1 未停在 human_review")
        print(f"  {out1}")
        print("\n结论: NO-GO")
        return 1

    print()
    print("--- Scenario 1 / Stage 2：**新进程** 恢复同一 thread_id 并完成 ---")
    rc2, out2 = run_stage("stage2")
    print(f"  pid={out2.get('pid')}  resumed_from={out2.get('resumed_from')}  "
          f"next={out2.get('next')}  has_report={out2.get('has_report')}")
    print(f"  returncode={rc2}")
    if rc2 != 0:
        print("  ✗ 跨进程恢复失败")
        print(f"  {out2}")
        print("\n结论: NO-GO")
        return 1

    print()
    print("--- 进程隔离确认 ---")
    same_pid = out1.get("pid") == out2.get("pid")
    print(f"  stage1 pid={out1.get('pid')}  stage2 pid={out2.get('pid')}  "
          f"{'（相同！隔离不成立）' if same_pid else '（不同进程 ✓）'}")

    print()
    print("=== 场景结果 ===")
    print("  [PASS] Scenario 1  HITL 跨进程恢复（worker #1 退出 → worker #2 完成）")
    print(f"  [PASS] Scenario 5  astream_events + {backend} checkpointer 无 NotImplementedError")
    print()
    print("结论: GO（就本场景与后端而言）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase F Worker Compatibility Spike")
    parser.add_argument("stage", choices=["stage1", "stage2", "inspect", "run"])
    parser.add_argument("--thread-id", default=None)
    parser.add_argument("--action", default="approve", choices=["approve", "revise"])
    parser.add_argument("--query", default="Compare LangGraph and CrewAI")
    args = parser.parse_args(argv)

    if args.stage == "run":
        return _orchestrate(
            os.environ.get("CHECKPOINTER_BACKEND", "sqlite"), args.action, args.query
        )

    if not args.thread_id:
        parser.error("--thread-id 为必填（run 除外）")

    print("REPO_ROOT=" + str(REPO_ROOT))
    _banner(args.stage)
    return asyncio.run(_run(args.stage, args.thread_id, args.action, args.query))


if __name__ == "__main__":
    sys.exit(main())
