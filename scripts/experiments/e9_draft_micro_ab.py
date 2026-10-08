#!/usr/bin/env python
"""E9 Draft Micro A/B（Phase 3C-3 §20）。

固定完全相同的研究简报（从某个 VALID run 的 checkpointer 状态读取），
分别用 draft thinking ON / OFF 各跑 N 次（交错），比较：

* latency / reasoning tokens / output tokens
* draft 结构（长度、章节、引用）
* **major topic coverage**：两臂章节标题集合的重叠（Jaccard）
* **initial research directions**：从 draft 提取的"研究方向"信号
  （章节标题 + 待研究问题的关键词面重叠）

注意（§20 的告诫）：micro 只回答"draft 本身在不同 thinking 下的差异"，
**不能**替代 E2E——draft 的真正价值在于它是否改变后续 research behavior，
最终判定以 E2E 为主。

    ALLOW_LIVE_EXTERNAL_APIS=true .venv/bin/python scripts/experiments/e9_draft_micro_ab.py \
        --thread <thread_id> --reps 3 --out artifacts/experiments/phase3/e9_micro
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


def _build_prompt(research_brief: str) -> str:
    """与 draft_agent.write_draft_report 完全一致的 prompt 构造。"""
    from deep_research.prompts import DRAFT_REPORT_PROMPT
    from deep_research.utils import get_today_str

    return DRAFT_REPORT_PROMPT.format(research_brief=research_brief, date=get_today_str())


async def _one_call(model, prompt: str) -> dict:
    from langchain_core.messages import HumanMessage

    t0 = time.time()
    response = await model.ainvoke([HumanMessage(content=prompt)])
    content = response.content if hasattr(response, "content") else str(response)
    usage = getattr(response, "usage_metadata", None) or {}
    return {"latency_ms": int((time.time() - t0) * 1000), "content": content, "usage": usage}


def _section_titles(text: str) -> list[str]:
    import re

    return [t.strip() for t in re.findall(r"^#{2,3}\s+(.+)$", text, re.M)]


def _topic_jaccard(a: list[str], b: list[str]) -> float | None:
    from deep_research.benchmark.quality import normalize_text

    sa = {normalize_text(t) for t in a if t.strip()}
    sb = {normalize_text(t) for t in b if t.strip()}
    if not sa or not sb:
        return None
    return len(sa & sb) / len(sa | sb)


async def main_async(args: argparse.Namespace) -> dict:
    from deep_research.benchmark.quality import report_metrics, summarize, text_hash
    from deep_research.llm import get_chat_model

    os.environ.setdefault("DR_STREAM_USAGE", "1")

    state = await _read_state(args.thread)
    brief = state.get("research_brief") or ""
    if not brief:
        raise SystemExit("[FAIL] 状态中没有 research_brief")
    prompt = _build_prompt(brief)
    prompt_hash = text_hash(prompt)
    print(f"[micro] thread={args.thread} brief_chars={len(brief)} prompt_chars={len(prompt)} prompt_hash={prompt_hash}")

    variants = {
        "thinking-on": get_chat_model("draft", thinking=True),
        "thinking-off": get_chat_model("draft", thinking=False),
    }

    results: dict[str, list[dict]] = {"thinking-on": [], "thinking-off": []}
    order = ["thinking-on", "thinking-off"] * args.reps
    for round_index, variant in enumerate(order):
        out = await _one_call(variants[variant], prompt)
        content = out["content"]
        usage = out["usage"] or {}
        details = usage.get("output_token_details") or {}
        sections = _section_titles(content)
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
            "section_titles": sections,
        }
        results[variant].append(record)
        (args.out / f"{variant}-{len(results[variant])}.md").write_text(content, encoding="utf-8")
        print(f"  [{variant} #{len(results[variant])}] latency={record['latency_ms']}ms out={record['output_tokens']} "
              f"reasoning={record['reasoning_tokens']} chars={record['content_chars']} sections={len(sections)}")

    # 跨臂 topic 重叠（同一输入下 draft 的"研究方向"是否漂移）
    on_sections = [s for r in results["thinking-on"] for s in r["section_titles"]]
    off_sections = [s for r in results["thinking-off"] for s in r["section_titles"]]
    on_reps, off_reps = results["thinking-on"], results["thinking-off"]
    pairwise = []
    for i, a in enumerate(on_reps):
        for j, b in enumerate(off_reps):
            pairwise.append({
                "on_rep": i + 1, "off_rep": j + 1,
                "topic_jaccard": _topic_jaccard(a["section_titles"], b["section_titles"]),
                "len_delta_percent": (b["content_chars"] - a["content_chars"]) / max(1, a["content_chars"]) * 100,
            })
    within_on = [
        _topic_jaccard(on_reps[i]["section_titles"], on_reps[j]["section_titles"])
        for i in range(len(on_reps)) for j in range(i + 1, len(on_reps))
    ]
    within_off = [
        _topic_jaccard(off_reps[i]["section_titles"], off_reps[j]["section_titles"])
        for i in range(len(off_reps)) for j in range(i + 1, len(off_reps))
    ]

    summary = {
        "latency_ms": {v: summarize([r["latency_ms"] for r in rows]) for v, rows in results.items()},
        "output_tokens": {v: summarize([r["output_tokens"] for r in rows]) for v, rows in results.items()},
        "reasoning_tokens": {v: summarize([r["reasoning_tokens"] for r in rows]) for v, rows in results.items()},
        "content_chars": {v: summarize([r["content_chars"] for r in rows]) for v, rows in results.items()},
        "sections": {v: summarize([len(r["section_titles"]) for r in rows]) for v, rows in results.items()},
        "citations": {v: summarize([r["report_metrics"]["citation_markers"] for r in rows]) for v, rows in results.items()},
    }
    payload = {
        "thread_id": args.thread,
        "brief_chars": len(brief),
        "prompt_chars": len(prompt),
        "prompt_hash": prompt_hash,
        "reps_per_variant": args.reps,
        "interleaved_order": order,
        "results": results,
        "summary": summary,
        "topic_overlap": {
            "cross_arm_pairs": pairwise,
            "cross_arm_median_jaccard": summarize([p["topic_jaccard"] for p in pairwise])["median"],
            "within_on_median_jaccard": summarize(within_on)["median"],
            "within_off_median_jaccard": summarize(within_off)["median"],
            "on_sections": sorted(set(on_sections)),
            "off_sections": sorted(set(off_sections)),
        },
    }
    (args.out / "micro_ab.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--thread", required=True)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "artifacts" / "experiments" / "phase3" / "e9_micro")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    payload = asyncio.run(main_async(args))
    for metric, values in payload["summary"].items():
        print(f"{metric:<18} ON median={values['thinking-on']['median']}  OFF median={values['thinking-off']['median']}")
    t = payload["topic_overlap"]
    print(f"topic jaccard: cross={t['cross_arm_median_jaccard']} within_on={t['within_on_median_jaccard']} "
          f"within_off={t['within_off_median_jaccard']}")
    print(f"-> {args.out / 'micro_ab.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
