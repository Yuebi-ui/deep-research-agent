#!/usr/bin/env python
"""Phase 5A A/B 分析：CONTROL（串行）vs TREATMENT（seed-lite 投机）——只读。

    .venv/bin/python scripts/experiments/phase5a_analyze.py \
        --tag-ctl ctl --tag-trt trt --queries q1 q2 q3 q4 \
        --out artifacts/phase5a/ab_analysis.json

性能侧：E2E / 分段 wall / draft∥research 重叠 / **节点重复执行检测**（投机并行最
危险的失败模式）/ 云端调用与 token / fully-settled。
质量侧：复用 `deep_research.benchmark.quality`（verification 分布、supported-claim
coverage、引用与结构指标）+ 研究深度计数（supervisor/researcher/search）。

口径纪律：summed latency ≠ critical path；本脚本只输出分段时间线（顶层串行链）
与重叠秒数，不做因果外推。
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
    report_metrics,
    verification_metrics,
)
from deep_research.settings import get_engine_settings  # noqa: E402

SEGMENTS = ("write_research_brief", "write_draft_report", "build_research_seed",
            "supervisor_subgraph", "claim_verification", "final_report_generation")

# runner 只记录**子图内部**的节点（没有 supervisor_subgraph 这一行）→ 研究阶段窗口
# 必须用内部节点的包络来表示（与 Phase 4B 的 critical path 口径一致）
RESEARCH_INNER = ("supervisor", "supervisor_tools", "llm_call", "tool_node",
                  "compress_research", "red_team")
# 分支入口节点：这两个（以及 seed 节点）出现 >1 次才说明"分支被重复执行"
BRANCH_NODES = ("write_draft_report", "build_research_seed", "supervisor_subgraph")


def _task_row(thread_id: str) -> dict:
    from backend.db.engine import resolve_database_path
    db = resolve_database_path()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE thread_id = ?", (thread_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _window(rows: list[dict], node: str) -> tuple[float, float] | None:
    spans = [(r["started_at"], r["finished_at"]) for r in rows
             if r.get("node") == node and isinstance(r.get("started_at"), (int, float))
             and isinstance(r.get("finished_at"), (int, float))]
    return (min(s for s, _ in spans), max(e for _, e in spans)) if spans else None


def _window_multi(rows: list[dict], nodes: tuple[str, ...]) -> tuple[float, float] | None:
    """多个内部节点的包络（研究阶段）。"""
    spans = [(r["started_at"], r["finished_at"]) for r in rows
             if r.get("node") in nodes and isinstance(r.get("started_at"), (int, float))
             and isinstance(r.get("finished_at"), (int, float))]
    return (min(s for s, _ in spans), max(e for _, e in spans)) if spans else None


def _overlap(a: tuple[float, float] | None, b: tuple[float, float] | None) -> float:
    if not a or not b:
        return 0.0
    return round(max(0.0, min(a[1], b[1]) - max(a[0], b[0])), 2)


def _enrichment_ms(run_id: str) -> int | None:
    path = get_engine_settings().resolved_data_dir / "baseline_metrics" / run_id / "reliability_events.jsonl"
    for row in _jsonl(path):
        if row.get("event") == "post_completion_enrichment":
            return (row.get("details") or {}).get("elapsed_ms")
    return None


def analyze_run(run_id: str) -> dict:
    run_dir = REPO_ROOT / "artifacts" / "baseline" / run_id
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    nodes = _jsonl(run_dir / "node_metrics.jsonl")
    calls = _jsonl(run_dir / "llm_calls.jsonl")
    searches = _jsonl(run_dir / "search_metrics.jsonl")
    task = _task_row(run["task_id"])

    # ---- 性能 ----
    node_counts: dict[str, int] = {}
    for r in nodes:
        node_counts[r.get("node")] = node_counts.get(r.get("node"), 0) + 1
    segs = {}
    for node in SEGMENTS:
        span = _window_multi(nodes, RESEARCH_INNER) if node == "supervisor_subgraph" else _window(nodes, node)
        segs[node] = round(span[1] - span[0], 1) if span else None
    draft_w = _window(nodes, "write_draft_report")
    research_w = _window_multi(nodes, RESEARCH_INNER)
    e2e_ms = (run.get("timeline") or {}).get("total_latency_ms") or 0
    enrich = _enrichment_ms(run_id)

    local = [c for c in calls if c.get("provider") == "openai_local"]
    cloud = [c for c in calls if c.get("provider") != "openai_local"]
    sup = sorted([c for c in calls if c.get("role") == "supervisor"],
                 key=lambda c: c.get("started_at") or 0)
    writer = [c for c in calls if c.get("role") == "writer"]

    perf = {
        "run_id": run_id,
        "variant": (run.get("experiment") or {}).get("variant"),
        "validity": (run.get("integrity") or {}).get("validity"),
        "status": run.get("status"),
        "attempts": run.get("attempts"),
        "e2e_s": round(e2e_ms / 1000, 1),
        "fully_settled_s": round((e2e_ms + (enrich or 0)) / 1000, 1),
        "enrichment_ms": enrich,
        "segments_s": segs,
        "draft_research_overlap_s": _overlap(draft_w, research_w),
        "node_run_counts": node_counts,
        # 只有"分支入口节点被跑多次"才是投机并行的事故信号（supervisor/llm_call 等
        # 在研究循环里本来就会重复）
        "branch_node_counts": {n: node_counts.get(n, 0) for n in BRANCH_NODES},
        "duplicate_branches": {n: node_counts.get(n, 0) for n in BRANCH_NODES
                               if node_counts.get(n, 0) > 1},
        "llm_calls": len(calls),
        "local_calls": len(local),
        "cloud_calls": len(cloud),
        "input_tokens": (run.get("llm") or {}).get("input_tokens"),
        "output_tokens": (run.get("llm") or {}).get("output_tokens"),
        "est_cost_rmb": (run.get("cloud") or {}).get("estimated_cost_rmb"),
        "claim_losses": (run.get("reliability") or {}).get("claim_losses"),
        "overflow": (run.get("context") or {}).get("overflow_count"),
        # seed-lite 直接影响 supervisor 的首轮上下文（draft 全文 vs 短 seed）
        "supervisor_calls": len(sup),
        "first_supervisor_input_tokens": (sup[0].get("input_tokens") if sup else None),
        "supervisor_input_tokens_total": sum((c.get("input_tokens") or 0) for c in sup) or None,
        "writer_input_tokens": (writer[0].get("input_tokens") if writer else None),
    }

    # ---- 质量 ----
    verification = json.loads(task["verification"]) if task.get("verification") else {}
    vm = verification_metrics(verification.get("details"))
    report = task.get("final_report") or ""
    coverage = claim_coverage(vm["supported_claims"], report)
    rm = report_metrics(report)
    urls = {u for s in searches for u in (s.get("url_keys") or [])}

    quality = {
        "run_id": run_id,
        "variant": perf["variant"],
        "report_chars": rm["length_chars"],
        "report_words": rm["length_words"],
        "h2": rm["h2_count"], "h3": rm["h3_count"],
        "citation_markers": rm["citation_markers"],
        "unique_urls_cited": rm["unique_url_count"],
        "has_conclusion": rm["has_conclusion_section"],
        "claims_total": vm["total_claims"],
        "claims_by_verdict": vm["by_verdict"],
        "unsupported_rate": vm["unsupported_rate"],
        "coverage_covered": coverage.get("covered"),
        "coverage_total": coverage.get("total"),
        "coverage_ratio": (coverage.get("covered") / coverage["total"]) if coverage.get("total") else None,
        "supervisor_rounds": node_counts.get("supervisor", 0),
        "supervisor_tools_rounds": node_counts.get("supervisor_tools", 0),
        "researcher_calls": node_counts.get("llm_call", 0),
        "tool_node_calls": node_counts.get("tool_node", 0),
        "compress_calls": node_counts.get("compress_research", 0),
        "search_calls": len(searches),
        "unique_urls_searched": len(urls),
        "red_team_rounds": node_counts.get("red_team", 0),
    }
    return {"perf": perf, "quality": quality}


def _fmt(v, spec="{:.1f}"):
    return "—" if v is None else spec.format(v)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag-ctl", default="ctl")
    ap.add_argument("--tag-trt", default="trt")
    ap.add_argument("--prefix", default="p5a", help="run-id 前缀（Phase 5B 用 p5b）")
    ap.add_argument("--queries", nargs="+", default=["q1", "q2", "q3", "q4"])
    ap.add_argument("--suffixes", nargs="+", default=[""],
                    help="round 后缀（'' 表示首轮；'-r2' 表示确认轮）")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "artifacts" / "phase5a" / "ab_analysis.json")
    args = ap.parse_args(argv)

    rows = []
    for q in args.queries:
        for suffix in args.suffixes:
            suf = f"-{suffix}" if suffix else ""
            for tag in (args.tag_ctl, args.tag_trt):
                run_id = f"{args.prefix}-{tag}-{q}{suf}"
                run_dir = REPO_ROOT / "artifacts" / "baseline" / run_id / "run.json"
                if not run_dir.exists():
                    print(f"[skip] {run_id} 无产物")
                    continue
                rows.append({**analyze_run(run_id), "query": q, "arm": tag, "round": (suffix or "-r1").lstrip("-")})

    print(f"{'query':<5}{'arm':<5}{'E2E':>8}{'settled':>9}{'draft':>7}{'research':>9}{'overlap':>8}"
          f"{'cv':>6}{'writer':>7}{'dup':>5}{'cloud':>6}{'cost':>7}{'validity':>9}")
    for r in rows:
        p = r["perf"]
        s = p["segments_s"]
        print(f"{r['query']:<5}{r['arm']:<5}{p['e2e_s']:>8.1f}{p['fully_settled_s']:>9.1f}"
              f"{_fmt(s['write_draft_report']):>7}{_fmt(s['supervisor_subgraph']):>9}"
              f"{p['draft_research_overlap_s']:>8.1f}{_fmt(s['claim_verification']):>6}"
              f"{_fmt(s['final_report_generation']):>7}{len(p['duplicate_branches']):>5}"
              f"{p['cloud_calls']:>6}{_fmt(p['est_cost_rmb'], '{:.3f}'):>7}{str(p['validity']):>9}")

    print(f"\n{'query':<5}{'arm':<5}{'claims':>7}{'unsup':>7}{'cover':>7}{'cites':>7}{'urls':>6}"
          f"{'sup_r':>6}{'res_c':>6}{'search':>7}{'u_urls':>7}{'chars':>7}")
    for r in rows:
        q = r["quality"]
        print(f"{r['query']:<5}{q['variant']:<5}{q['claims_total']:>7}"
              f"{_fmt(q['unsupported_rate'], '{:.0%}'):>7}"
              f"{_fmt(q['coverage_ratio'], '{:.0%}'):>7}{q['citation_markers']:>7}{q['unique_urls_cited']:>6}"
              f"{q['supervisor_rounds']:>6}{q['researcher_calls']:>6}{q['search_calls']:>7}"
              f"{q['unique_urls_searched']:>7}{q['report_chars']:>7}")

    print(f"\n{'query':<5}{'arm':<5}{'supervisor calls':>17}{'1st sup in-tok':>15}"
          f"{'sup in-tok总':>13}{'writer in-tok':>14}{'分支执行次数':>26}")
    for r in rows:
        p = r["perf"]
        print(f"{r['query']:<5}{r['arm']:<5}{p['supervisor_calls']:>17}"
              f"{str(p['first_supervisor_input_tokens']):>15}"
              f"{str(p['supervisor_input_tokens_total']):>13}{str(p['writer_input_tokens']):>14}"
              f"{str(p['branch_node_counts']):>26}")

    dups = [r for r in rows if r["perf"]["duplicate_branches"]]
    if dups:
        print("\n⚠️ 分支入口节点被重复执行（投机并行最危险的失败模式）：")
        for r in dups:
            print(f"   {r['perf']['run_id']}: {r['perf']['duplicate_branches']}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"\n-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
