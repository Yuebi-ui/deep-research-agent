#!/usr/bin/env python
"""E8 Writer Replay / Micro A/B（Phase 3C-2 §13）。

目的：**隔离 upstream research variance**——对完全相同的 writer 输入，
分别用 thinking on/off 各跑 N 次，回答"在相同 evidence/state 下，
thinking 是否真的改善 writer 输出"。

方法：
1. 从 Redis checkpointer 读取某个 VALID run 的完整图状态
   （research_brief / notes / draft_report / claim_verification_warning）；
2. 用与 `agent_builder.final_report_generation` **完全相同的代码路径**
   重建 writer prompt（同一 FINAL_REPORT_PROMPT + warning + 同一日期），
   计算 prompt_hash 作为"输入完全一致"的证据；
3. on/off 交错执行（on,off,on,off,...），每次 astream，记录
   latency / usage / 输出哈希；报告全文保存到本地 artifacts（gitignored）；
4. 用 deep_research.benchmark.quality 的确定性指标做质量对比。

不执行 graph、不写 checkpoint、不产生任务——纯 writer-level harness。

    ALLOW_LIVE_EXTERNAL_APIS=true .venv/bin/python scripts/experiments/e8_writer_micro_ab.py \
        --thread <thread_id> --reps 3 --out artifacts/experiments/phase3/e8_micro
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


async def _read_state(thread_id: str) -> dict:
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver
    from deep_research.checkpoint import resolve_redis_url

    async with AsyncRedisSaver.from_conn_string(resolve_redis_url()) as saver:
        tup = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
        state = tup.checkpoint.get("channel_values", {}) if tup else None
    if not state:
        raise SystemExit(f"[FAIL] checkpointer 中找不到 thread {thread_id} 的状态")
    return state


def _build_prompt(state: dict) -> str:
    """与 agent_builder.final_report_generation 完全一致的 prompt 构造。"""
    from deep_research.prompts import FINAL_REPORT_PROMPT
    from deep_research.utils import get_today_str

    notes = state.get("notes", [])
    findings = "\n".join(notes)
    warning = state.get("claim_verification_warning", "")
    report_context = FINAL_REPORT_PROMPT.format(
        research_brief=state.get("research_brief", ""),
        findings=findings,
        date=get_today_str(),
        draft_report=state.get("draft_report", ""),
    )
    return report_context + warning


async def _one_call(model, prompt: str) -> dict:
    from langchain_core.messages import HumanMessage

    t0 = time.time()
    chunks: list[str] = []
    usage = None
    async for chunk in model.astream([HumanMessage(content=prompt)]):
        if chunk.content:
            chunks.append(chunk.content)
        if getattr(chunk, "usage_metadata", None):
            usage = chunk.usage_metadata
    content = "".join(chunks)
    return {
        "latency_ms": int((time.time() - t0) * 1000),
        "content": content,
        "usage": usage,
    }


async def main_async(args: argparse.Namespace) -> dict:
    from deep_research.llm import get_chat_model
    from deep_research.benchmark.quality import claim_coverage, report_metrics, text_hash, summarize, verification_metrics

    # 与 worker 环境一致：流式 usage 上报（.env.server 中为 1）
    os.environ.setdefault("DR_STREAM_USAGE", "1")

    state = await _read_state(args.thread)
    prompt = _build_prompt(state)
    prompt_hash = text_hash(prompt)
    print(f"[micro] thread={args.thread} prompt_chars={len(prompt)} prompt_hash={prompt_hash}")

    # 校验输入的 claim 覆盖基准（来自同一 state 的 verification 数据不可得时留空）
    verification = state.get("verification_report") or {}
    verdicts = verification.get("details") if isinstance(verification, dict) else None
    ver_m = verification_metrics(verdicts) if verdicts else {"supported_claims": [], "unsupported_claims": []}

    variants = {
        "thinking-on": get_chat_model("writer", thinking=True),
        "thinking-off": get_chat_model("writer", thinking=False),
    }
    print("[micro] variants constructed: writer thinking on/off (models: deepseek-v4-pro)")

    results: dict[str, list[dict]] = {"thinking-on": [], "thinking-off": []}
    order = ["thinking-on", "thinking-off"] * args.reps  # 交错，抵御时间漂移
    for round_index, variant in enumerate(order):
        out = await _one_call(variants[variant], prompt)
        content = out["content"]
        usage = out["usage"] or {}
        details = usage.get("output_token_details") or {}
        record = {
            "variant": variant,
            "round": round_index,
            "latency_ms": out["latency_ms"],
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "reasoning_tokens": details.get("reasoning"),
            "content_chars": len(content),
            "content_hash": text_hash(content),
            "report_metrics": report_metrics(content),
            "supported_claim_coverage": {
                k: v for k, v in claim_coverage(ver_m.get("supported_claims") or [], content).items()
                if k != "covered_claims"
            },
        }
        results[variant].append(record)
        (args.out / f"{variant}-{len(results[variant])}.md").write_text(content, encoding="utf-8")
        print(f"  [{variant} #{len(results[variant])}] latency={record['latency_ms']}ms "
              f"out={record['output_tokens']} reasoning={record['reasoning_tokens']} chars={record['content_chars']}")

    summary = {
        "latency_ms": {v: summarize([r["latency_ms"] for r in rows]) for v, rows in results.items()},
        "output_tokens": {v: summarize([r["output_tokens"] for r in rows]) for v, rows in results.items()},
        "reasoning_tokens": {v: summarize([r["reasoning_tokens"] for r in rows]) for v, rows in results.items()},
        "content_chars": {v: summarize([r["content_chars"] for r in rows]) for v, rows in results.items()},
        "citation_markers": {v: summarize([r["report_metrics"]["citation_markers"] for r in rows]) for v, rows in results.items()},
        "unique_urls": {v: summarize([r["report_metrics"]["unique_url_count"] for r in rows]) for v, rows in results.items()},
        "h2_count": {v: summarize([r["report_metrics"]["h2_count"] for r in rows]) for v, rows in results.items()},
    }
    payload = {
        "thread_id": args.thread,
        "prompt_chars": len(prompt),
        "prompt_hash": prompt_hash,
        "reps_per_variant": args.reps,
        "interleaved_order": order,
        "verification_claim_counts": {
            "total": ver_m.get("total_claims"),
            "supported": len(ver_m.get("supported_claims") or []),
            "unsupported": len(ver_m.get("unsupported_claims") or []),
        },
        "results": results,
        "summary": summary,
    }
    (args.out / "micro_ab.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--thread", required=True, help="VALID run 的 thread_id（从 Redis checkpointer 读状态）")
    parser.add_argument("--reps", type=int, default=3, help="每个 variant 的重复次数")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "artifacts" / "experiments" / "phase3" / "e8_micro")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    payload = asyncio.run(main_async(args))
    s = payload["summary"]
    for metric, values in s.items():
        on, off = values["thinking-on"], values["thinking-off"]
        print(f"{metric:<18} ON median={on['median']}  OFF median={off['median']}")
    print(f"-> {args.out / 'micro_ab.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
