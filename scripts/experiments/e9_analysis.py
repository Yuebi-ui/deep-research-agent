#!/usr/bin/env python
"""E9（Phase 3C-3）：draft thinking ON/OFF 的研究深度 + 质量 + 性能汇总。

只读 artifacts / tasks.db（+ 可选 checkpointer），不发任何 LLM 请求。

    .venv/bin/python scripts/experiments/e9_analysis.py \
        --runs e9-a1 e9-b1 e9-a2 e9-b2 e9-a3 e9-b3 \
        --out artifacts/experiments/phase3/e9_analysis.json

输出四块：
1. **research depth（§17 硬护栏）**：supervisor 轮次、researcher 调用/迭代、
   tool_node、search 次数/结果数/唯一 URL、evidence 数、claim/verdict 分布；
2. **draft 直测（§15）**：write_draft_report 节点延迟、draft LLM 的
   provider/model/thinking/latency/tokens、draft 文本结构；
3. **E2E / token / cost（§16）** 与 **最终报告质量（§19）**；
4. **hybrid 角色画像（§25）**：每个 logical role 的 local/cloud、provider、
   model、调用数、token、延迟（Phase 4 baseline 用）。

以及 ON/OFF 的 min/median/max + absolute/percentage delta（§24）
和 paired（A_i ↔ B_i）下游对比（§21）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

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
UPSTREAM_VARIANCE_THRESHOLD = 0.25


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


def _count(rows: list[dict], key: str, value: str) -> int:
    return sum(1 for r in rows if r.get(key) == value)


def analyze_run(run_id: str) -> dict:
    run_dir = ARTIFACTS / run_id
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    llm_rows = _load_rows(run_dir / "llm_calls.jsonl")
    node_rows = _load_rows(run_dir / "node_metrics.jsonl")
    search_rows = _load_rows(run_dir / "search_metrics.jsonl")
    task = _task_row(run["task_id"])

    # ---- draft 直测 ----
    draft_rows = [r for r in llm_rows if r.get("role") == "draft"]
    draft = {
        "calls": len(draft_rows),
        "thinking_flag": sorted({str(r.get("thinking")) for r in draft_rows}),
        "model": sorted({r.get("model") for r in draft_rows}),
        "provider": sorted({r.get("provider") for r in draft_rows}),
        "latency_ms": sum(r.get("latency_ms") or 0 for r in draft_rows) or None,
        "input_tokens": sum(r.get("input_tokens") or 0 for r in draft_rows) or None,
        "output_tokens": sum(r.get("output_tokens") or 0 for r in draft_rows) or None,
        "reasoning_tokens": sum(r.get("reasoning_tokens") or 0 for r in draft_rows) or None,
    }
    draft_report = task.get("draft_report") or ""
    draft_text = report_metrics(draft_report)

    # ---- research depth（§17）----
    url_keys = {u for r in search_rows for u in (r.get("url_keys") or [])}
    verification = json.loads(task["verification"]) if task.get("verification") else {}
    verdicts = verification.get("details") or []
    ver = verification_metrics(verdicts)
    evidence_items = sum(1 for d in verdicts if (d.get("evidence") or "").strip())

    depth = {
        "supervisor_rounds": _count(node_rows, "node", "supervisor") or _count(llm_rows, "role", "supervisor"),
        "supervisor_tools_rounds": _count(node_rows, "node", "supervisor_tools"),
        "researcher_calls": _count(llm_rows, "role", "researcher_main"),
        "researcher_iterations": _count(node_rows, "node", "llm_call"),  # research agent 的 LLM 迭代
        "tool_node_calls": _count(node_rows, "node", "tool_node"),
        "compress_research_calls": _count(node_rows, "node", "compress_research"),
        "search_calls": (run.get("search") or {}).get("calls"),
        "search_results": (run.get("search") or {}).get("results"),
        "unique_urls": len(url_keys),
        "evidence_items": evidence_items,
        "claim_total": ver["total_claims"],
        "claim_by_verdict": ver["by_verdict"],
    }

    node_latency: dict[str, float] = {}
    for row in node_rows:
        node = row.get("node")
        if node:
            node_latency[node] = node_latency.get(node, 0.0) + (row.get("latency_ms") or 0)

    final_report = task.get("final_report") or ""
    final_m = report_metrics(final_report)
    coverage = claim_coverage(ver["supported_claims"], final_report)

    # ---- hybrid 角色画像（§25）----
    hybrid: dict[str, dict] = {}
    for r in llm_rows:
        role = r.get("role") or "unknown"
        entry = hybrid.setdefault(role, {
            "provider": r.get("provider"), "model": r.get("model"), "calls": 0,
            "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "latency_ms": 0,
        })
        entry["calls"] += 1
        entry["input_tokens"] += r.get("input_tokens") or 0
        entry["output_tokens"] += r.get("output_tokens") or 0
        entry["reasoning_tokens"] += r.get("reasoning_tokens") or 0
        entry["latency_ms"] += r.get("latency_ms") or 0
    for entry in hybrid.values():
        entry["is_local"] = entry["provider"] == "openai_local"

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
    gates["all_ok"] = (
        gates["status"] == "completed" and gates["validity"] == "VALID"
        and (gates["llm_failed"] or 0) == 0 and (gates["context_overflow"] or 0) == 0
        and (gates["claim_losses"] or 0) == 0 and (gates["stale_execution_abandoned"] or 0) == 0
    )

    return {
        "run_id": run_id,
        "variant": (run.get("experiment") or {}).get("variant"),
        "gates": gates,
        "draft": {**draft, "report": draft_text},
        "depth": depth,
        "perf": {
            "e2e_latency_ms": (run.get("timeline") or {}).get("total_latency_ms"),
            "node_latency_ms": {k: round(v, 1) for k, v in sorted(node_latency.items())},
            "totals": {
                "input_tokens": (run.get("llm") or {}).get("input_tokens"),
                "output_tokens": (run.get("llm") or {}).get("output_tokens"),
                "reasoning_tokens": (run.get("llm") or {}).get("reasoning_tokens"),
                "local_calls": (run.get("local") or {}).get("calls"),
                "cloud_calls": (run.get("cloud") or {}).get("calls"),
                "cloud_cost_rmb": (run.get("cloud") or {}).get("estimated_cost_rmb"),
            },
        },
        "quality": {
            "final": final_m,
            "supported_claim_coverage": coverage,
            "verification": {k: v for k, v in ver.items() if k not in ("supported_claims", "unsupported_claims")},
        },
        "hybrid_profile": hybrid,
    }


# ===== 汇总 =====


_METRIC_PATHS = {
    "e2e_latency_ms": ["perf", "e2e_latency_ms"],
    "draft_node_ms": ["perf", "node_latency_ms", "write_draft_report"],
    "draft_latency_ms": ["draft", "latency_ms"],
    "draft_input_tokens": ["draft", "input_tokens"],
    "draft_output_tokens": ["draft", "output_tokens"],
    "draft_reasoning_tokens": ["draft", "reasoning_tokens"],
    "draft_chars": ["draft", "report", "length_chars"],
    "draft_sections": ["draft", "report", "h2_count"],
    "draft_citations": ["draft", "report", "citation_markers"],
    "supervisor_rounds": ["depth", "supervisor_rounds"],
    "supervisor_tools_rounds": ["depth", "supervisor_tools_rounds"],
    "researcher_calls": ["depth", "researcher_calls"],
    "researcher_iterations": ["depth", "researcher_iterations"],
    "tool_node_calls": ["depth", "tool_node_calls"],
    "search_calls": ["depth", "search_calls"],
    "search_results": ["depth", "search_results"],
    "unique_urls": ["depth", "unique_urls"],
    "evidence_items": ["depth", "evidence_items"],
    "claim_total": ["depth", "claim_total"],
    "final_report_chars": ["quality", "final", "length_chars"],
    "final_report_sections": ["quality", "final", "h2_count"],
    "final_report_citations": ["quality", "final", "citation_markers"],
    "final_unique_urls": ["quality", "final", "unique_url_count"],
    "final_claim_coverage": ["quality", "supported_claim_coverage", "covered"],
    "total_input_tokens": ["perf", "totals", "input_tokens"],
    "total_output_tokens": ["perf", "totals", "output_tokens"],
    "total_reasoning_tokens": ["perf", "totals", "reasoning_tokens"],
    "cloud_cost_rmb": ["perf", "totals", "cloud_cost_rmb"],
}


def _pick(rows: list[dict], path: list[str]):
    values = []
    for r in rows:
        cur = r
        for key in path:
            cur = (cur or {}).get(key) if isinstance(cur, dict) else None
        values.append(cur)
    return summarize(values)


def summarize_variants(runs: list[dict]) -> dict:
    on = [r for r in runs if r["variant"] == "thinking-on"]
    off = [r for r in runs if r["variant"] == "thinking-off"]
    out = {"thinking-on": {}, "thinking-off": {}, "delta_off_minus_on": {}}
    for name, path in _METRIC_PATHS.items():
        on_s, off_s = _pick(on, path), _pick(off, path)
        out["thinking-on"][name] = on_s
        out["thinking-off"][name] = off_s
        out["delta_off_minus_on"][name] = delta(on_s, off_s)
    return out


def _pct(a, b):
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)) or not a:
        return None
    return (b - a) / a * 100


def paired_compare(a: dict, b: dict) -> dict:
    """A(ON) ↔ B(OFF)：下游链路逐段对比（§21）。

    writer 已固定 thinking OFF，最终报告差异应追溯到 draft → research →
    evidence 链上的第一处分歧。
    """
    stages = {}
    for stage, path in (
        ("draft", ["draft"]),
        ("depth", ["depth"]),
        ("quality", ["quality"]),
    ):
        stages[stage] = {
            key: {"on": a[path[0]].get(key), "off": b[path[0]].get(key)}
            for key in a[path[0]]
            if not isinstance(a[path[0]].get(key), (dict, list))
        }
    input_deltas = {
        key: {"on": a["draft"].get(key), "off": b["draft"].get(key),
              "percent": _pct(a["draft"].get(key), b["draft"].get(key))}
        for key in ("input_tokens", "reasoning_tokens", "output_tokens")
    }
    depth_deltas = {
        key: {"on": a["depth"].get(key), "off": b["depth"].get(key),
              "percent": _pct(a["depth"].get(key), b["depth"].get(key))}
        for key in ("supervisor_rounds", "researcher_iterations", "tool_node_calls",
                    "search_calls", "search_results", "unique_urls", "claim_total")
    }
    contractions = [
        k for k, v in depth_deltas.items()
        if v["percent"] is not None and v["percent"] < -UPSTREAM_VARIANCE_THRESHOLD * 100
    ]
    return {
        "pair": {"on": a["run_id"], "off": b["run_id"]},
        "stages": stages,
        "draft_deltas": input_deltas,
        "depth_deltas": depth_deltas,
        "depth_contraction_fields": contractions,  # OFF 相对 ON 下降 >25% 的深度指标
        "coverage": {
            "on_covered": a["quality"]["supported_claim_coverage"]["covered"],
            "off_covered": b["quality"]["supported_claim_coverage"]["covered"],
            "on_total": a["quality"]["supported_claim_coverage"]["total"],
            "off_total": b["quality"]["supported_claim_coverage"]["total"],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "artifacts" / "experiments" / "phase3" / "e9_analysis.json")
    args = parser.parse_args(argv)

    runs = [analyze_run(r) for r in args.runs]
    ons = [r for r in runs if r["variant"] == "thinking-on"]
    offs = [r for r in runs if r["variant"] == "thinking-off"]
    pairs = [paired_compare(a, b) for a, b in zip(ons, offs)]

    result = {"runs": runs, "summary": summarize_variants(runs), "pairs": pairs}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

    s = result["summary"]

    def _fmt(value) -> str:
        if value is None:
            return "-"
        if isinstance(value, (int, float)) and abs(value) > 10:
            return f"{value:,.0f}"
        return f"{value}"

    print(f"{'metric':<28}{'ON median':>16}{'OFF median':>16}{'Δ%':>9}")
    for name in s["delta_off_minus_on"]:
        d = s["delta_off_minus_on"][name]["median"]
        on_med, off_med = s["thinking-on"][name]["median"], s["thinking-off"][name]["median"]
        pct = f"{d['percent']:+.1f}%" if d.get("percent") is not None else "-"
        print(f"{name:<28}{_fmt(on_med):>16}{_fmt(off_med):>16}{pct:>9}")
    for p in pairs:
        flag = f" DEPTH_CONTRACTION={p['depth_contraction_fields']}" if p["depth_contraction_fields"] else ""
        print(f"pair {p['pair']['on']} ↔ {p['pair']['off']}: "
              f"coverage {p['coverage']['on_covered']}/{p['coverage']['on_total']} vs "
              f"{p['coverage']['off_covered']}/{p['coverage']['off_total']}{flag}")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
