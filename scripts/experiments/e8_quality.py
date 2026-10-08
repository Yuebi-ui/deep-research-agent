#!/usr/bin/env python
"""E8（Phase 3C-2）：writer thinking ON/OFF 的离线质量 + 性能汇总。

只读 artifacts 与 tasks.db（+ 可选 Redis checkpointer 读图状态），不发任何 LLM 请求。

    .venv/bin/python scripts/experiments/e8_quality.py \
        --runs e8-a1 e8-b1 e8-a2 e8-b2 e8-a3 e8-b3 \
        --out artifacts/experiments/phase3/e8_quality.json

输出：
1. 每个 run 的 correctness gates / 性能 / 质量 / writer 输入工作量；
2. A（on） vs B（off）的 min/median/max + absolute/percentage delta；
3. 逐对（A_i ↔ B_i）结构化对比：claim 覆盖差异、section 差异、指标差；
   upstream variance 检测（writer 输入差异过大时标记，不把报告差异全部归因 writer）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from deep_research.benchmark.quality import (  # noqa: E402
    claim_coverage,
    delta,
    report_metrics,
    summarize,
    verification_metrics,
)

ARTIFACTS = REPO_ROOT / "artifacts" / "baseline"
UPSTREAM_VARIANCE_THRESHOLD = 0.25  # writer 输入指标差异 >25% → 标记


def _load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _task_row(thread_id: str) -> dict:
    from deep_research.settings import get_engine_settings

    db = get_engine_settings().resolved_data_dir / "tasks.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE thread_id = ?", (thread_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _graph_state(thread_id: str) -> dict | None:
    """（可选）从 Redis checkpointer 读取图状态，用于 writer 输入工作量对比。"""
    try:
        import asyncio

        from langgraph.checkpoint.redis.aio import AsyncRedisSaver

        from deep_research.checkpoint import resolve_redis_url

        async def _read():
            async with AsyncRedisSaver.from_conn_string(resolve_redis_url()) as saver:
                tup = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
                return (tup.checkpoint.get("channel_values", {}) if tup else None)

        state = asyncio.run(_read())
    except Exception:
        return None
    if not state:
        return None
    return {
        "research_brief": state.get("research_brief") or "",
        "draft_report": state.get("draft_report") or "",
        "notes": [n for n in (state.get("notes") or []) if isinstance(n, str)],
        "warning": state.get("claim_verification_warning") or "",
        "final_report": state.get("final_report") or "",
    }


# ===== 单 run 分析 =====


def analyze_run(run_id: str, *, with_state: bool = True) -> dict:
    run_dir = ARTIFACTS / run_id
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    llm_rows = _load_rows(run_dir / "llm_calls.jsonl")
    node_rows = _load_rows(run_dir / "node_metrics.jsonl")
    task = _task_row(run["task_id"])

    writer_rows = [r for r in llm_rows if r.get("role") == "writer" and r.get("node") == "final_report_generation"]
    writer = {
        "calls": len(writer_rows),
        "thinking_flag": sorted({str(r.get("thinking")) for r in writer_rows}),
        "latency_ms": sum(r.get("latency_ms") or 0 for r in writer_rows) or None,
        "input_tokens": sum(r.get("input_tokens") or 0 for r in writer_rows) or None,
        "output_tokens": sum(r.get("output_tokens") or 0 for r in writer_rows) or None,
        "reasoning_tokens": sum(r.get("reasoning_tokens") or 0 for r in writer_rows) or None,
    }

    node_latency: dict[str, float] = {}
    for row in node_rows:
        node = row.get("node")
        if not node:
            continue
        node_latency[node] = node_latency.get(node, 0.0) + (row.get("latency_ms") or 0)

    final_report = task.get("final_report") or ""
    draft_report = task.get("draft_report") or ""
    verification = json.loads(task["verification"]) if task.get("verification") else {}
    verdicts = verification.get("details") or []

    report_m = report_metrics(final_report)
    ver_m = verification_metrics(verdicts)
    supported_cov = claim_coverage(ver_m["supported_claims"], final_report)
    unsupported_cov = claim_coverage(ver_m["unsupported_claims"], final_report) if ver_m["unsupported_claims"] else {
        "total": 0, "covered": 0, "coverage": None, "covered_claims": [], "uncovered_claims": [],
    }

    state = _graph_state(run["task_id"]) if with_state else None
    writer_input = {
        "input_tokens": writer["input_tokens"],
        "draft_report_chars": len((state or {}).get("draft_report") or draft_report),
        "research_brief_chars": len((state or {}).get("research_brief") or ""),
        "notes_count": len((state or {}).get("notes") or []),
        "source_count": (run.get("search") or {}).get("results"),
        "verified_claims": ver_m["total_claims"],
        "warning_chars": len((state or {}).get("warning") or ""),
        "state_source": "redis_checkpointer" if state else "tasks_db_only",
    }

    gates = {
        "status": run.get("status"),
        "validity": (run.get("integrity") or {}).get("validity"),
        "llm_failed": (run.get("llm") or {}).get("failed"),
        "context_overflow": (run.get("context") or {}).get("overflow_count"),
        "claim_losses": (run.get("reliability") or {}).get("claim_losses"),
        "stale_execution_abandoned": (run.get("reliability") or {}).get("stale_execution_abandoned"),
        "execution_aborts": (run.get("reliability") or {}).get("execution_aborts"),
        "attempts": run.get("attempts"),
    }
    gates_ok = (
        gates["status"] == "completed"
        and gates["validity"] == "VALID"
        and (gates["llm_failed"] or 0) == 0
        and (gates["context_overflow"] or 0) == 0
        and (gates["claim_losses"] or 0) == 0
        and (gates["stale_execution_abandoned"] or 0) == 0
    )

    return {
        "run_id": run_id,
        "variant": (run.get("experiment") or {}).get("variant"),
        "experiment": run.get("experiment"),
        "gates": {**gates, "all_ok": gates_ok},
        "perf": {
            "e2e_latency_ms": (run.get("timeline") or {}).get("total_latency_ms"),
            "node_latency_ms": {k: round(v, 1) for k, v in sorted(node_latency.items())},
            "writer": writer,
            "totals": {
                "input_tokens": (run.get("llm") or {}).get("input_tokens"),
                "output_tokens": (run.get("llm") or {}).get("output_tokens"),
                "reasoning_tokens": (run.get("llm") or {}).get("reasoning_tokens"),
                "local_calls": (run.get("local") or {}).get("calls"),
                "cloud_calls": (run.get("cloud") or {}).get("calls"),
                "search_calls": (run.get("search") or {}).get("calls"),
                "cloud_cost_rmb": (run.get("cloud") or {}).get("estimated_cost_rmb"),
            },
        },
        "quality": {
            "report": report_m,
            "verification": {k: v for k, v in ver_m.items() if k not in ("supported_claims", "unsupported_claims")},
            # 完整保留 details（逐 claim 的 exact / containment），这是质量结论的原始证据
            "supported_claim_coverage": supported_cov,
            "unsupported_claim_reassertion": unsupported_cov,
        },
        "writer_input": writer_input,
    }


# ===== 对比 =====


def _pct(a, b) -> float | None:
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)) or not a:
        return None
    return (b - a) / a * 100


def paired_compare(a: dict, b: dict) -> dict:
    """A（on） ↔ B（off）逐对对比；输入工作量差异过大时标记 UPSTREAM_VARIANCE。"""
    ra, rb = a["quality"]["report"], b["quality"]["report"]
    ca = set(a["quality"]["supported_claim_coverage"].get("covered_claims") or [])
    cb = set(b["quality"]["supported_claim_coverage"].get("covered_claims") or [])

    on_cont = {d["claim"]: d["containment"] for d in a["quality"]["supported_claim_coverage"].get("details", [])}
    off_cont = {d["claim"]: d["containment"] for d in b["quality"]["supported_claim_coverage"].get("details", [])}
    containment_deltas = [
        {
            "claim": claim,
            "on": on_cont.get(claim),
            "off": off_cont.get(claim),
            "delta": (round(off_cont[claim] - on_cont[claim], 3)
                      if claim in on_cont and claim in off_cont else None),
        }
        for claim in sorted(set(on_cont) | set(off_cont))
    ]
    strong_on_weak_off = [
        d for d in containment_deltas
        if (d["on"] or 0.0) >= 0.8 and (d["off"] if d["off"] is not None else 1.0) < 0.5
    ]

    in_a = a["writer_input"]
    in_b = b["writer_input"]
    input_deltas = {
        key: {"on": in_a.get(key), "off": in_b.get(key), "percent": _pct(in_a.get(key), in_b.get(key))}
        for key in ("input_tokens", "draft_report_chars", "source_count", "notes_count")
    }
    invars = [k for k, v in input_deltas.items() if v["percent"] is not None and abs(v["percent"]) > UPSTREAM_VARIANCE_THRESHOLD * 100]

    return {
        "pair": {"on": a["run_id"], "off": b["run_id"]},
        "report_deltas": {
            key: {"on": ra.get(key), "off": rb.get(key), "percent": _pct(ra.get(key), rb.get(key))}
            for key in (
                "length_chars", "h2_count", "h3_count", "citation_markers",
                "unique_citation_indices", "unique_url_count", "list_items", "table_rows",
            )
        },
        "verification_deltas": {
            key: {"on": a["quality"]["verification"].get(key), "off": b["quality"]["verification"].get(key)}
            for key in ("total_claims", "unsupported_rate")
        },
        "coverage": {
            "on_covered": a["quality"]["supported_claim_coverage"].get("covered"),
            "off_covered": b["quality"]["supported_claim_coverage"].get("covered"),
            "total": a["quality"]["supported_claim_coverage"].get("total"),
            "on_median_containment": a["quality"]["supported_claim_coverage"].get("median_containment"),
            "off_median_containment": b["quality"]["supported_claim_coverage"].get("median_containment"),
            # 比二值 covered 更敏感的逐 claim 对比（ON 强覆盖 → OFF 弱覆盖 的候选丢失项）
            "containment_deltas": containment_deltas,
            "strong_on_weak_off": strong_on_weak_off,
        },
        "verified_claims_in_ON_only": sorted(ca - cb),
        "verified_claims_in_OFF_only": sorted(cb - ca),
        "sections_ON_only": sorted(set(ra.get("h2_titles") or []) - set(rb.get("h2_titles") or [])),
        "sections_OFF_only": sorted(set(rb.get("h2_titles") or []) - set(ra.get("h2_titles") or [])),
        "unsupported_claims_repeated": {
            "on": a["quality"]["unsupported_claim_reassertion"].get("covered"),
            "off": b["quality"]["unsupported_claim_reassertion"].get("covered"),
        },
        "writer_input_deltas": input_deltas,
        "upstream_variance": {"flagged": bool(invars), "fields_over_threshold": invars,
                              "threshold_percent": UPSTREAM_VARIANCE_THRESHOLD * 100},
    }


# ===== 汇总 =====


def summarize_variants(runs: list[dict]) -> dict:
    on = [r for r in runs if r["variant"] == "thinking-on"]
    off = [r for r in runs if r["variant"] == "thinking-off"]

    def _pick(rows, path):
        values = []
        for r in rows:
            cur: Any = r
            for key in path:
                cur = (cur or {}).get(key) if isinstance(cur, dict) else None
            values.append(cur)
        return summarize(values)

    metrics = {
        "e2e_latency_ms": (["perf", "e2e_latency_ms"]),
        "final_report_generation_ms": (["perf", "node_latency_ms", "final_report_generation"]),
        "write_draft_report_ms": (["perf", "node_latency_ms", "write_draft_report"]),
        "supervisor_ms": (["perf", "node_latency_ms", "supervisor"]),
        "claim_verification_ms": (["perf", "node_latency_ms", "claim_verification"]),
        "writer_latency_ms": (["perf", "writer", "latency_ms"]),
        "writer_input_tokens": (["perf", "writer", "input_tokens"]),
        "writer_output_tokens": (["perf", "writer", "output_tokens"]),
        "writer_reasoning_tokens": (["perf", "writer", "reasoning_tokens"]),
        "total_input_tokens": (["perf", "totals", "input_tokens"]),
        "total_output_tokens": (["perf", "totals", "output_tokens"]),
        "total_reasoning_tokens": (["perf", "totals", "reasoning_tokens"]),
        "cloud_cost_rmb": (["perf", "totals", "cloud_cost_rmb"]),
        "report_length_chars": (["quality", "report", "length_chars"]),
        "citation_markers": (["quality", "report", "citation_markers"]),
        "unique_urls": (["quality", "report", "unique_url_count"]),
        "h2_sections": (["quality", "report", "h2_count"]),
        "supported_claim_coverage_count": (["quality", "supported_claim_coverage", "covered"]),
        "unsupported_rate": (["quality", "verification", "unsupported_rate"]),
        "search_calls": (["perf", "totals", "search_calls"]),
        "local_calls": (["perf", "totals", "local_calls"]),
        "cloud_calls": (["perf", "totals", "cloud_calls"]),
    }

    out = {"thinking-on": {}, "thinking-off": {}, "delta_off_minus_on": {}}
    for name, path in metrics.items():
        on_s, off_s = _pick(on, path), _pick(off, path)
        out["thinking-on"][name] = on_s
        out["thinking-off"][name] = off_s
        out["delta_off_minus_on"][name] = delta(on_s, off_s)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", nargs="+", required=True, help="run_id 列表（顺序即配对顺序：A1 B1 A2 B2 ...）")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "artifacts" / "experiments" / "phase3" / "e8_quality.json")
    parser.add_argument("--no-state", action="store_true", help="跳过 Redis checkpointer（只用 tasks.db）")
    args = parser.parse_args(argv)

    runs = [analyze_run(run_id, with_state=not args.no_state) for run_id in args.runs]

    pairs = []
    ons = [r for r in runs if r["variant"] == "thinking-on"]
    offs = [r for r in runs if r["variant"] == "thinking-off"]
    for i, (a, b) in enumerate(zip(ons, offs), start=1):
        pairs.append(paired_compare(a, b))

    result = {
        "runs": runs,
        "summary": summarize_variants(runs),
        "pairs": pairs,
        "pairing_note": "按 variant 内出现顺序配对（A_i ↔ B_i）",
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

    s = result["summary"]
    print(f"{'metric':<32}{'ON median':>14}{'OFF median':>14}{'Δ%':>10}")
    for name in s["delta_off_minus_on"]:
        d = s["delta_off_minus_on"][name]["median"]
        pct = f"{d['percent']:+.1f}%" if d.get("percent") is not None else "-"
        on_med = s["thinking-on"][name]["median"]
        off_med = s["thinking-off"][name]["median"]
        fmt = (lambda v: f"{v:,.0f}") if isinstance(on_med, (int, float)) and abs(on_med) > 10 else (lambda v: f"{v:.3f}" if isinstance(v, float) else str(v))
        print(f"{name:<32}{fmt(on_med) if on_med is not None else '-':>14}{fmt(off_med) if off_med is not None else '-':>14}{pct:>10}")
    for p in pairs:
        flag = " [UPSTREAM_VARIANCE]" if p["upstream_variance"]["flagged"] else ""
        print(f"pair {p['pair']['on']} ↔ {p['pair']['off']}: "
              f"claims covered {p['coverage']['on_covered']} vs {p['coverage']['off_covered']}/{p['coverage']['total']}; "
              f"ON-only claims={len(p['verified_claims_in_ON_only'])} OFF-only={len(p['verified_claims_in_OFF_only'])}{flag}")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
