"""Phase 4A 分析：把采样时间序列与既有 LLM/node 事件对齐。

核心口径（与任务书 §13/§31 一致）：

- **summed latency**（本地角色各请求耗时之和）只用于解释"并行工作量"，
  **不得**直接当作关键路径；
- **wall / critical path** 由 node_metrics 的窗口给出；
- **overlap factor** = summed / wall，量化并行收益；
- 峰值归因：把 GPU/KV/queue 峰值时间点落到当时的活跃 node 窗口上。
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any, Iterable

LOCAL_PROVIDER = "openai_local"


# ===== 加载 =====


def load_csv(path: Path) -> list[dict[str, Any]]:
    if not Path(path).exists():
        return []
    rows: list[dict[str, Any]] = []
    with open(path, newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            row: dict[str, Any] = {}
            for key, value in raw.items():
                if key == "ts":
                    try:
                        row["ts"] = float(value)
                    except (TypeError, ValueError):
                        row["ts"] = None
                else:
                    try:
                        row[key] = float(value) if value not in (None, "") else None
                    except ValueError:
                        row[key] = None
            if row.get("ts") is not None:
                rows.append(row)
    return rows


def load_jsonl(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


# ===== 对齐 =====


def align_samples(gpu_rows: list[dict], vllm_rows: list[dict], tolerance: float = 1.5) -> list[dict]:
    """按 ts 最近邻合并 GPU 与 vLLM 采样（两者各自 1s 节奏，容差默认 1.5s）。"""
    merged: list[dict] = []
    for g in gpu_rows:
        ts = g.get("ts")
        best, best_d = None, None
        for v in vllm_rows:
            d = abs((v.get("ts") or 0) - ts)
            if best_d is None or d < best_d:
                best, best_d = v, d
        if best is not None and best_d is not None and best_d <= tolerance:
            row = {k: val for k, val in g.items() if k != "ts"}
            row.update({k: val for k, val in best.items() if k != "ts"})
            row["ts"] = ts
            merged.append(row)
    return merged


# ===== 本地请求并发 =====


def local_request_intervals(llm_rows: Iterable[dict]) -> list[dict]:
    intervals = []
    for r in llm_rows:
        if r.get("provider") != LOCAL_PROVIDER:
            continue
        start, end = r.get("started_at"), r.get("finished_at")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        intervals.append({
            "role": r.get("role"), "node": r.get("node"),
            "start": float(start), "end": float(end),
            "latency_ms": r.get("latency_ms") or (end - start) * 1000.0,
            "input_tokens": r.get("input_tokens"), "output_tokens": r.get("output_tokens"),
        })
    intervals.sort(key=lambda x: x["start"])
    return intervals


def concurrency_at(intervals: list[dict], ts: float) -> int:
    """某时刻活跃（已开始未结束）的本地请求数。"""
    return sum(1 for iv in intervals if iv["start"] <= ts < iv["end"])


def concurrency_timeline(intervals: list[dict], timestamps: Iterable[float]) -> list[int]:
    return [concurrency_at(intervals, ts) for ts in timestamps]


def concurrency_stats(timestamps: Iterable[float], values: list[int]) -> dict:
    values = list(values)
    if not values:
        return {"peak": 0, "mean": 0.0, "p90": 0, "n": 0}
    ordered = sorted(values)
    return {
        "peak": max(values),
        "mean": sum(values) / len(values),
        "p90": ordered[min(len(ordered) - 1, int(0.9 * (len(ordered) - 1)))],
        "n": len(values),
    }


def peak_concurrency_windows(intervals: list[dict]) -> list[dict]:
    """扫描线求并发峰值出现的所有时间窗（用于报告"burst 何时发生"）。"""
    events: list[tuple[float, int]] = []
    for iv in intervals:
        events.append((iv["start"], 1))
        events.append((iv["end"], -1))
    events.sort()
    current, peak, peak_times = 0, 0, []
    for ts, delta in events:
        current += delta
        if current > peak:
            peak, peak_times = current, [ts]
        elif current == peak and delta == 1:
            peak_times.append(ts)
    return [{"peak": peak, "at": ts} for ts in peak_times] if peak else []


# ===== 窗口统计 =====


def _quantile(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, max(0, int(math.ceil(q * len(sorted_values)) - 1)))
    return sorted_values[idx]


def window_stats(samples: list[dict], key: str, start: float | None = None, end: float | None = None) -> dict:
    values = [
        s[key] for s in samples
        if isinstance(s.get(key), (int, float))
        and (start is None or s["ts"] >= start)
        and (end is None or s["ts"] <= end)
    ]
    if not values:
        return {"n": 0, "mean": None, "p50": None, "p90": None, "max": None, "min": None}
    ordered = sorted(values)
    return {
        "n": len(values),
        "mean": sum(values) / len(values),
        "p50": _quantile(ordered, 0.5),
        "p90": _quantile(ordered, 0.9),
        "max": ordered[-1],
        "min": ordered[0],
    }


def stage_windows(node_rows: list[dict]) -> dict[str, list[tuple[float, float]]]:
    """node → 出现过的 [start, end] 窗口列表（同名 node 多次出现取并集语义）。"""
    windows: dict[str, list[tuple[float, float]]] = {}
    for row in node_rows:
        node, start, end = row.get("node"), row.get("started_at"), row.get("finished_at")
        if node and isinstance(start, (int, float)) and isinstance(end, (int, float)):
            windows.setdefault(node, []).append((float(start), float(end)))
    return windows


def active_nodes_at(windows: dict[str, list[tuple[float, float]]], ts: float) -> list[str]:
    return sorted(node for node, spans in windows.items() if any(s <= ts <= e for s, e in spans))


# ===== summed vs wall =====


def overlap_factor(summed_ms: float, wall_ms: float) -> float | None:
    """并行重叠系数 = summed / wall（>1 表示存在并行；1 表示纯串行）。"""
    if not wall_ms or wall_ms <= 0:
        return None
    return summed_ms / wall_ms


def local_role_summary(llm_rows: list[dict]) -> dict[str, dict]:
    summary: dict[str, dict] = {}
    for r in llm_rows:
        if r.get("provider") != LOCAL_PROVIDER:
            continue
        role = r.get("role") or "unknown"
        entry = summary.setdefault(role, {
            "calls": 0, "input_tokens": 0, "output_tokens": 0, "summed_latency_ms": 0.0,
            "nodes": set(), "failed": 0,
        })
        entry["calls"] += 1
        entry["input_tokens"] += r.get("input_tokens") or 0
        entry["output_tokens"] += r.get("output_tokens") or 0
        entry["summed_latency_ms"] += r.get("latency_ms") or 0.0
        if r.get("node"):
            entry["nodes"].add(r["node"])
        if not r.get("success"):
            entry["failed"] += 1
    for entry in summary.values():
        entry["nodes"] = sorted(entry["nodes"])
    return summary


def cloud_role_summary(llm_rows: list[dict]) -> dict[str, dict]:
    summary: dict[str, dict] = {}
    for r in llm_rows:
        if r.get("provider") == LOCAL_PROVIDER:
            continue
        role = r.get("role") or "unknown"
        entry = summary.setdefault(role, {
            "provider": r.get("provider"), "model": r.get("model"), "calls": 0,
            "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
            "summed_latency_ms": 0.0, "thinking": set(),
        })
        entry["calls"] += 1
        entry["input_tokens"] += r.get("input_tokens") or 0
        entry["output_tokens"] += r.get("output_tokens") or 0
        entry["reasoning_tokens"] += r.get("reasoning_tokens") or 0
        entry["summed_latency_ms"] += r.get("latency_ms") or 0.0
        entry["thinking"].add(str(r.get("thinking")))
    for entry in summary.values():
        entry["thinking"] = sorted(entry["thinking"])
    return summary


# ===== 计数器 delta =====


def counter_delta(rows: list[dict], key: str) -> float | None:
    values = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
    if len(values) < 2:
        return None
    return values[-1] - values[0]


def prefix_cache_ratio(rows: list[dict]) -> dict:
    hits = counter_delta(rows, "prefix_cache_hits_total")
    queries = counter_delta(rows, "prefix_cache_queries_total")
    cached = counter_delta(rows, "prompt_tokens_cached_total")
    prompt = counter_delta(rows, "prompt_tokens_total")
    return {
        "prefix_cache_hits": hits,
        "prefix_cache_queries": queries,
        "hit_rate": (hits / queries) if hits is not None and queries else None,
        "cached_prompt_tokens": cached,
        "prompt_tokens": prompt,
        "cached_token_ratio": (cached / prompt) if cached is not None and prompt else None,
    }


def peak_attribution(samples: list[dict], windows: dict[str, list[tuple[float, float]]], key: str) -> dict:
    """返回某指标峰值样本的时间、数值与当时活跃的 node 列表。"""
    best = None
    for s in samples:
        value = s.get(key)
        if isinstance(value, (int, float)) and (best is None or value > best[1]):
            best = (s["ts"], value)
    if best is None:
        return {"ts": None, "value": None, "active_nodes": []}
    return {"ts": best[0], "value": best[1], "active_nodes": active_nodes_at(windows, best[0])}
