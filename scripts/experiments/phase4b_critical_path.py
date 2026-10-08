#!/usr/bin/env python
"""Phase 4B：E2E critical path 归因（只读 run artifacts）。

    .venv/bin/python scripts/experiments/phase4b_critical_path.py \
        --runs phase4a-p1 phase4a-p2 phase4a-p3 --out artifacts/phase4b/critical_path

方法（证据分级见 `deep_research.profiling.critical_path`）：

- **proven**：顶层节点链由 graph 定义保证串行（agent_builder 的 add_edge），
  因此每个顶层段 wall 100% 落在 critical path 上；
- **attributed**：段内调用按最大重叠簇划分，簇 makespan 归因给最后收尾的
  role（binder）——并行批不按 summed 计；
- **unattributed**：段间与簇间空隙单独列出，不硬凑成谁的时间。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from deep_research.profiling.critical_path import (  # noqa: E402
    attribute_blocking,
    chain_gaps,
)

MILESTONES = (
    "write_research_brief",
    "write_draft_report",
    "human_review",
    "claim_verification",
    "final_report_generation",
)
RESEARCH_INNER = ("supervisor", "supervisor_tools", "llm_call", "tool_node",
                  "compress_research", "red_team")
LOCAL_PROVIDER = "openai_local"


def _load(run_dir: Path, name: str) -> list[dict]:
    path = run_dir / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _segments(node_rows: list[dict], t0: float) -> list[dict]:
    """顶层串行段（milestones + 研究阶段包络）。"""
    segs: list[dict] = []
    for node in MILESTONES:
        spans = [(r["started_at"], r["finished_at"]) for r in node_rows if r["node"] == node]
        if spans:
            segs.append({"node": node, "kind": "milestone",
                         "start": min(s for s, _ in spans), "end": max(e for _, e in spans)})
    inner = [(r["started_at"], r["finished_at"]) for r in node_rows if r["node"] in RESEARCH_INNER]
    if inner:
        segs.append({"node": "supervisor_subgraph", "kind": "composite",
                     "start": min(s for s, _ in inner), "end": max(e for _, e in inner),
                     "nodes": sorted({r["node"] for r in node_rows if r["node"] in RESEARCH_INNER})})
    segs.sort(key=lambda s: s["start"])
    for seg in segs:
        seg["wall_s"] = round(seg["end"] - seg["start"], 2)
        seg["t0_rel"] = round(seg["start"] - t0, 1)
        seg["t1_rel"] = round(seg["end"] - t0, 1)
    return segs


def _intervals(calls: list[dict], start: float, end: float) -> list[dict]:
    out = []
    for c in calls:
        s, e = c.get("started_at"), c.get("finished_at")
        if not isinstance(s, (int, float)) or not isinstance(e, (int, float)):
            continue
        if s >= start - 0.05 and e <= end + 0.05:  # 完全落在段内
            out.append({
                "role": c.get("role") or "unknown",
                "node": c.get("node"),
                "provider": c.get("provider"),
                "model": c.get("model"),
                "start": float(s), "end": float(e),
                "local": c.get("provider") == LOCAL_PROVIDER,
                "tokens_in": c.get("input_tokens"),
                "tokens_out": c.get("output_tokens"),
            })
    return out


def _node_rows_in(node_rows: list[dict], start: float, end: float) -> list[dict]:
    return [r for r in node_rows
            if isinstance(r.get("started_at"), (int, float))
            and r["started_at"] >= start - 0.05 and r["finished_at"] <= end + 0.05]


def _role_table(calls: list[dict], segments: list[dict], e2e: float) -> list[dict]:
    """全局 role 表：summed（并行工作量）vs blocking（段内归因到该 role 的阻塞时间）。

    blocking 只在段内归因后累加 —— 跨段的调用本来就串行，直接相加即可。
    """
    summed: dict[str, float] = {}
    counts: dict[str, int] = {}
    providers: dict[str, str] = {}
    out_tokens: dict[str, int] = {}
    blocking: dict[str, float] = {}
    for c in calls:
        role = c.get("role") or "unknown"
        s, e = c.get("started_at"), c.get("finished_at")
        if isinstance(s, (int, float)) and isinstance(e, (int, float)):
            summed[role] = summed.get(role, 0.0) + (e - s)
        counts[role] = counts.get(role, 0) + 1
        providers[role] = c.get("provider")
        out_tokens[role] = out_tokens.get(role, 0) + (c.get("output_tokens") or 0)
    for seg in segments:
        for role, value in (seg["attribution"]["blocking_s"] or {}).items():
            blocking[role] = blocking.get(role, 0.0) + value
    rows = []
    for role in sorted(summed, key=lambda r: -blocking.get(r, 0.0)):
        rows.append({
            "role": role,
            "provider": providers.get(role),
            "calls": counts.get(role, 0),
            "summed_s": round(summed.get(role, 0.0), 1),
            "blocking_s": round(blocking.get(role, 0.0), 1),
            "blocking_pct_of_e2e": round(100 * blocking.get(role, 0.0) / e2e, 1),
            "output_tokens": out_tokens.get(role, 0),
        })
    return rows


def analyze_run(run_id: str, run_dir: Path) -> dict:
    nodes = _load(run_dir, "node_metrics.jsonl")
    calls = _load(run_dir, "llm_calls.jsonl")
    searches = _load(run_dir, "search_metrics.jsonl")
    nodes = [r for r in nodes if isinstance(r.get("started_at"), (int, float))
             and isinstance(r.get("finished_at"), (int, float))]
    t0 = min(r["started_at"] for r in nodes)
    t1 = max(r["finished_at"] for r in nodes)
    e2e = t1 - t0

    segs = _segments(nodes, t0)
    gaps = chain_gaps(segs)
    covered = sum(s["wall_s"] for s in segs)

    detail = []
    for seg in segs:
        ivs = _intervals(calls, seg["start"], seg["end"])
        attribution = attribute_blocking(ivs) if ivs else {
            "blocking_s": {}, "summed_s": {}, "calls": {}, "coverage_s": 0.0,
            "span_s": 0.0, "gap_s": 0.0, "clusters": []}
        inner_nodes = _node_rows_in(nodes, seg["start"], seg["end"])
        detail.append({
            "node": seg["node"],
            "wall_s": seg["wall_s"],
            "t0_rel": seg["t0_rel"], "t1_rel": seg["t1_rel"],
            "pct_of_e2e": round(100 * seg["wall_s"] / e2e, 1),
            "attribution": attribution,
            "inner_nodes": sorted(
                {n["node"]: round(sum(r["finished_at"] - r["started_at"]
                                      for r in inner_nodes if r["node"] == n["node"]), 2)
                 for n in inner_nodes}.items(), key=lambda kv: -kv[1]),
        })

    local_calls = [c for c in calls if c.get("provider") == LOCAL_PROVIDER]
    cloud_calls = [c for c in calls if c.get("provider") != LOCAL_PROVIDER]
    role_table = _role_table(calls, detail, e2e)
    return {
        "run_id": run_id,
        "e2e_s": round(e2e, 1),
        "covered_by_top_level_s": round(covered, 2),
        "covered_pct": round(100 * covered / e2e, 1),
        "unattributed_gap_s": round(e2e - covered, 2),
        "gaps": gaps,
        "role_table": role_table,
        "segments": detail,
        "totals": {
            "local_calls": len(local_calls),
            "cloud_calls": len(cloud_calls),
            "local_summed_s": round(sum((c["finished_at"] - c["started_at"]) for c in local_calls), 1),
            "cloud_summed_s": round(sum((c["finished_at"] - c["started_at"]) for c in cloud_calls), 1),
            "search_metrics_rows": len(searches),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--run-root", type=Path, default=REPO_ROOT / "artifacts" / "baseline")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "artifacts" / "phase4b" / "critical_path")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    reports = []
    for run_id in args.runs:
        report = analyze_run(run_id, args.run_root / run_id)
        reports.append(report)
        (args.out / f"{run_id}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"\n=== {run_id}  E2E={report['e2e_s']}s  "
              f"顶层覆盖={report['covered_pct']}%  未归因gap={report['unattributed_gap_s']}s")
        print(f"{'segment':<26}{'t0':>7}{'t1':>7}{'wall':>8}{'%E2E':>7}  attribution(blocking)")
        for seg in report["segments"]:
            blocking = ", ".join(f"{k}:{v:.1f}s" for k, v in list(seg["attribution"]["blocking_s"].items())[:4])
            print(f"{seg['node']:<26}{seg['t0_rel']:>7.1f}{seg['t1_rel']:>7.1f}"
                  f"{seg['wall_s']:>8.1f}{seg['pct_of_e2e']:>6.1f}%  {blocking}")
        if report["gaps"]:
            print("  gaps:", ", ".join(f"{g['after']}→{g['before']}:{g['gap_s']}s" for g in report["gaps"]))
        print(f"  {'role':<22}{'calls':>6}{'summed':>9}{'blocking':>10}{'%E2E':>7}")
        for row in report["role_table"]:
            print(f"  {row['role']:<22}{row['calls']:>6}{row['summed_s']:>9.1f}"
                  f"{row['blocking_s']:>10.1f}{row['blocking_pct_of_e2e']:>6.1f}%")

    (args.out / "summary.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"\n-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
