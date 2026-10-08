"""Prometheus 文本解析与 vLLM metrics 快照提取（Phase 4A）。

只解析、只读。指标名来自对运行中 vLLM 0.19.1 `/metrics` 的**实际枚举**
（见 Phase 4A 报告 §7），不猜测名称——不存在的指标显式缺席，由调用方标记
NOT EXPOSED。
"""

from __future__ import annotations

import math
import re
from typing import Any

# ===== 指标清单（经运行中实例验证存在）=====

# 标量（gauge / counter）：每次采样取一个值
SCALAR_METRICS: tuple[str, ...] = (
    "vllm:num_requests_running",
    "vllm:num_requests_waiting",
    "vllm:kv_cache_usage_perc",
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
    "vllm:prefix_cache_queries_total",
    "vllm:prefix_cache_hits_total",
    "vllm:prompt_tokens_cached_total",
    "vllm:prompt_tokens_recomputed_total",
    "vllm:num_preemptions_total",
    "vllm:request_success_total",
)

# 直方图：记录 sum/count/桶，用于区间内的分位数与 duration 汇总
HISTOGRAM_METRICS: tuple[str, ...] = (
    "vllm:time_to_first_token_seconds",
    "vllm:request_queue_time_seconds",
    "vllm:request_prefill_time_seconds",
    "vllm:request_decode_time_seconds",
    "vllm:e2e_request_latency_seconds",
    "vllm:request_time_per_output_token_seconds",
    "vllm:iteration_tokens_total",
)

_SAMPLE_RE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>[^\s]+)$")
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
_SUFFIXES = ("_bucket", "_sum", "_count", "_created")


def _parse_value(raw: str) -> float | None:
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_prometheus(text: str) -> dict[str, list[tuple[dict[str, str], float]]]:
    """把 Prometheus 文本解析为 {metric_name: [(labels, value), ...]}。

    只保留有限数值；忽略 HELP/TYPE 注释与无法解析的行（采集器绝不因
    单行异常整体失败）。
    """
    out: dict[str, list[tuple[dict[str, str], float]]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_RE.match(line)
        if not match:
            continue
        value = _parse_value(match.group("value"))
        if value is None:
            continue
        labels = dict(_LABEL_RE.findall(match.group("labels") or ""))
        out.setdefault(match.group("name"), []).append((labels, value))
    return out


def _first_value(parsed: dict[str, list[tuple[dict[str, str], float]]], name: str) -> float | None:
    entries = parsed.get(name)
    return entries[0][1] if entries else None


def extract_snapshot(text: str) -> dict[str, Any]:
    """从一次 /metrics 抓取中提取标量 + 直方图结构。

    直方图的 sum/count/bucket 都取自**同一份文本**，保证内部一致。
    """
    parsed = parse_prometheus(text)
    scalars = {name: _first_value(parsed, name) for name in SCALAR_METRICS}
    histograms: dict[str, dict[str, Any]] = {}
    for name in HISTOGRAM_METRICS:
        buckets = {
            float(labels["le"]): value
            for labels, value in parsed.get(f"{name}_bucket", [])
            if "le" in labels
        }
        total = _first_value(parsed, f"{name}_count")
        s = _first_value(parsed, f"{name}_sum")
        if buckets or total is not None or s is not None:
            histograms[name] = {"sum": s, "count": total, "buckets": buckets}
    return {"scalars": scalars, "histograms": histograms}


# ===== 直方图运算 =====


def delta_histogram(before: dict | None, after: dict | None) -> dict | None:
    """两快照之差（都是累计量），用于"单次 run 区间"的统计。"""
    if before is None or after is None:
        return None
    buckets = {
        le: after.get("buckets", {}).get(le, 0.0) - before.get("buckets", {}).get(le, 0.0)
        for le in sorted(set(after.get("buckets", {})) | set(before.get("buckets", {})))
    }
    sum_before, sum_after = before.get("sum"), after.get("sum")
    count_before, count_after = before.get("count"), after.get("count")
    return {
        "sum": None if sum_after is None or sum_before is None else sum_after - sum_before,
        "count": None if count_after is None or count_before is None else count_after - count_before,
        "buckets": buckets,
    }


def histogram_quantile(hist: dict | None, q: float) -> float | None:
    """Prometheus 风格的分位数近似（在桶边界上线性插值）。

    与 prometheus 官方实现同思路：用量 delta 后的桶做插值；桶不足以定位
    （q 落在 +Inf 桶）时返回最后一个有限桶上界。
    """
    if not hist:
        return None
    buckets = {float(le): v for le, v in (hist.get("buckets") or {}).items() if math.isfinite(float(le))}
    if not buckets:
        return None
    le_sorted = sorted(buckets)
    total = hist.get("count")
    if total is None:
        inf_buckets = [v for le, v in (hist.get("buckets") or {}).items() if not math.isfinite(float(le))]
        total = inf_buckets[0] if inf_buckets else buckets[le_sorted[-1]]
    if not total or total <= 0:
        return None
    rank = q * total
    prev_le, prev_cum = 0.0, 0.0
    for le in le_sorted:
        cum = buckets[le]
        if cum >= rank:
            if cum == prev_cum:
                return le
            frac = (rank - prev_cum) / (cum - prev_cum)
            return prev_le + frac * (le - prev_le)
        prev_le, prev_cum = le, cum
    return le_sorted[-1]


def histogram_mean(hist: dict | None) -> float | None:
    if not hist or not hist.get("count"):
        return None
    return (hist.get("sum") or 0.0) / hist["count"]
