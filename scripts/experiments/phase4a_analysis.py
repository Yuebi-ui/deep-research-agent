#!/usr/bin/env python
"""Phase 4A：把 profiling 采样与 run artifacts 对齐，产出 summary + timeline。

    .venv/bin/python scripts/experiments/phase4a_analysis.py \
        --run-id phase4a-p1 --profile-dir artifacts/phase4a/phase4a-p1 \
        --out artifacts/phase4a/phase4a-p1

只读：run artifacts 在 artifacts/baseline/<run_id>/，采样在 profile-dir/。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from deep_research.profiling.analysis import (  # noqa: E402
    align_samples,
    cloud_role_summary,
    concurrency_stats,
    concurrency_timeline,
    counter_delta,
    load_csv,
    load_jsonl,
    local_request_intervals,
    local_role_summary,
    overlap_factor,
    peak_attribution,
    peak_concurrency_windows,
    prefix_cache_ratio,
    stage_windows,
    window_stats,
)
from deep_research.profiling.scrape import delta_histogram, histogram_mean, histogram_quantile  # noqa: E402

NODE_LABELS = {
    "write_research_brief": "brief",
    "write_draft_report": "draft",
    "supervisor": "supervisor",
    "supervisor_tools": "supervisor_tools",
    "llm_call": "researcher",
    "tool_node": "tool_node",
    "compress_research": "compress",
    "claim_verification": "claim_verification",
    "final_report_generation": "final_writer",
}


def _run_window(node_rows: list[dict], llm_rows: list[dict]) -> tuple[float, float]:
    starts = [r.get("started_at") for r in llm_rows if isinstance(r.get("started_at"), (int, float))]
    ends = [r.get("finished_at") for r in llm_rows if isinstance(r.get("finished_at"), (int, float))]
    for row in node_rows:
        if isinstance(row.get("started_at"), (int, float)):
            starts.append(row["started_at"])
        if isinstance(row.get("finished_at"), (int, float)):
            ends.append(row["finished_at"])
    return (min(starts), max(ends)) if starts and ends else (None, None)


def analyze(run_id: str, profile_dir: Path, run_dir: Path) -> dict:
    llm_rows = load_jsonl(run_dir / "llm_calls.jsonl")
    node_rows = load_jsonl(run_dir / "node_metrics.jsonl")
    gpu_rows = load_csv(profile_dir / "gpu_metrics.csv")
    vllm_rows = load_csv(profile_dir / "vllm_metrics.csv")
    hist_rows = load_jsonl(profile_dir / "vllm_histograms.jsonl")

    samples = align_samples(gpu_rows, vllm_rows)
    intervals = local_request_intervals(llm_rows)
    windows = stage_windows(node_rows)
    start, end = _run_window(node_rows, llm_rows)
    if start is None and samples:
        start, end = samples[0]["ts"], samples[-1]["ts"]

    run_samples = [s for s in samples if start is not None and start <= s["ts"] <= end]

    # 本地推理活跃窗口（本地请求并集）：GPU 均值单独给"活跃期"口径
    active_samples = []
    for s in run_samples:
        if any(iv["start"] <= s["ts"] < iv["end"] for iv in intervals):
            active_samples.append(s)

    concurrency = concurrency_timeline(intervals, [s["ts"] for s in run_samples])

    # per-node：节点窗口内的本地请求 summed vs 节点 wall
    node_profile = {}
    for node, spans in sorted(windows.items()):
        wall_ms = sum((e - s) * 1000.0 for s, e in spans)
        node_intervals = [
            iv for iv in intervals
            if any(s <= iv["start"] <= e for s, e in spans)
        ]
        summed = sum(iv["latency_ms"] for iv in node_intervals)
        node_profile[node] = {
            "label": NODE_LABELS.get(node, node),
            "wall_ms": round(wall_ms, 1),
            "local_calls": len(node_intervals),
            "local_summed_ms": round(summed, 1),
            "overlap_factor": round(overlap_factor(summed, wall_ms), 2) if overlap_factor(summed, wall_ms) else None,
            "peak_local_concurrency": max(concurrency_timeline(node_intervals, [s["ts"] for s in run_samples]) or [0]),
        }

    # 直方图 delta（整个 run 窗口）
    histograms = {}
    if hist_rows:
        first = hist_rows[0]["histograms"]
        last = hist_rows[-1]["histograms"]
        for name in first:
            d = delta_histogram(first.get(name), last.get(name))
            histograms[name.split(":", 1)[1]] = {
                "mean_s": histogram_mean(d),
                "p50_s": histogram_quantile(d, 0.5),
                "p90_s": histogram_quantile(d, 0.9),
                "count": d.get("count") if d else None,
            }

    summary = {
        "run_id": run_id,
        "window": {"start": start, "end": end, "duration_s": (end - start) if start and end else None},
        "samples": {"total": len(samples), "in_run": len(run_samples), "active_local": len(active_samples)},
        "gpu": {
            "whole_run": {k: window_stats(run_samples, k) for k in ("gpu_util_percent", "mem_used_mib", "power_w", "temp_c")},
            "active_local": {k: window_stats(active_samples, k) for k in ("gpu_util_percent", "mem_used_mib", "power_w")},
        },
        "vllm": {
            "running": window_stats(run_samples, "num_requests_running"),
            "waiting": window_stats(run_samples, "num_requests_waiting"),
            "kv_cache_usage_perc": window_stats(run_samples, "kv_cache_usage_perc"),
            "peak_attribution": {
                "kv_cache": peak_attribution(run_samples, windows, "kv_cache_usage_perc"),
                "running": peak_attribution(run_samples, windows, "num_requests_running"),
                "waiting": peak_attribution(run_samples, windows, "num_requests_waiting"),
            },
            "counters": {
                "prompt_tokens": counter_delta(run_samples, "prompt_tokens_total"),
                "generation_tokens": counter_delta(run_samples, "generation_tokens_total"),
                "preemptions": counter_delta(run_samples, "num_preemptions_total"),
                "request_success": counter_delta(run_samples, "request_success_total"),
            },
            "prefix_cache": prefix_cache_ratio(run_samples),
            "histograms": histograms,
        },
        "concurrency": concurrency_stats([s["ts"] for s in run_samples], concurrency),
        "concurrency_peaks": peak_concurrency_windows(intervals),
        "nodes": node_profile,
        "local_roles": local_role_summary(llm_rows),
        "cloud_roles": cloud_role_summary(llm_rows),
    }
    return summary


def write_timeline(profile_dir: Path, run_dir: Path, out_path: Path) -> int:
    """逐采样行的时间线：GPU/vLLM + 本地并发 + 当时活跃 node（§11 对齐）。"""
    from deep_research.profiling.analysis import active_nodes_at

    llm_rows = load_jsonl(run_dir / "llm_calls.jsonl")
    node_rows = load_jsonl(run_dir / "node_metrics.jsonl")
    intervals = local_request_intervals(llm_rows)
    windows = stage_windows(node_rows)
    samples = align_samples(load_csv(profile_dir / "gpu_metrics.csv"), load_csv(profile_dir / "vllm_metrics.csv"))

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "ts", "gpu_util_percent", "mem_used_mib", "power_w", "num_requests_running",
            "num_requests_waiting", "kv_cache_usage_perc", "local_concurrency", "active_nodes",
        ])
        for s in samples:
            writer.writerow([
                f"{s['ts']:.3f}", s.get("gpu_util_percent"), s.get("mem_used_mib"), s.get("power_w"),
                s.get("num_requests_running"), s.get("num_requests_waiting"), s.get("kv_cache_usage_perc"),
                sum(1 for iv in intervals if iv["start"] <= s["ts"] < iv["end"]),
                "|".join(active_nodes_at(windows, s["ts"])),
            ])
    return len(samples)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    run_dir = args.run_dir or REPO_ROOT / "artifacts" / "baseline" / args.run_id
    out_dir = args.out or args.profile_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = analyze(args.run_id, args.profile_dir, run_dir)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    rows = write_timeline(args.profile_dir, run_dir, out_dir / "timeline.csv")

    g, v, c = summary["gpu"], summary["vllm"], summary["concurrency"]

    def _f(value, fmt="{:.1f}"):
        return "-" if value is None else fmt.format(value)

    window_s = summary["window"]["duration_s"]
    print(f"run={args.run_id} window={_f(window_s)}s samples={summary['samples']['in_run']}")
    print(f"GPU(run)  util mean={_f(g['whole_run']['gpu_util_percent']['mean'])}% "
          f"p90={_f(g['whole_run']['gpu_util_percent']['p90'], '{:.0f}')} max={_f(g['whole_run']['gpu_util_percent']['max'], '{:.0f}')}% "
          f"| mem max={_f(g['whole_run']['mem_used_mib']['max'], '{:.0f}')}MiB "
          f"power max={_f(g['whole_run']['power_w']['max'], '{:.0f}')}W")
    print(f"GPU(local-active) util mean={_f(g['active_local']['gpu_util_percent']['mean'])}% "
          f"max={_f(g['active_local']['gpu_util_percent']['max'], '{:.0f}')}%")
    print(f"vLLM running max={v['running']['max']} | waiting max={v['waiting']['max']} mean={_f(v['waiting']['mean'], '{:.2f}')} "
          f"| KV max={v['kv_cache_usage_perc']['max']} mean={_f(v['kv_cache_usage_perc']['mean'], '{:.3f}')}")
    print(f"local concurrency peak={c['peak']} mean={_f(c['mean'], '{:.2f}')} p90={c['p90']}")
    print(f"counters: prompt_tokens={v['counters']['prompt_tokens']} generation_tokens={v['counters']['generation_tokens']} "
          f"preemptions={v['counters']['preemptions']}")
    pc = v["prefix_cache"]
    print(f"prefix cache: hit_rate={pc['hit_rate']} token_ratio={pc['cached_token_ratio']}")
    print(f"timeline rows -> {out_dir / 'timeline.csv'} ({rows})")
    print(f"summary -> {out_dir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
