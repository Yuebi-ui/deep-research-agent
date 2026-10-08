"""Phase 4A：profiling 采集/解析/对齐的单元测试。

覆盖任务书 §32：metrics 解析、缺失指标、直方图分位、时间戳对齐、
run artifact 写入、采集失败不影响调用方、不写任何请求内容、
角色聚合、wall vs summed 计算。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from deep_research.profiling.analysis import (
    align_samples,
    concurrency_at,
    concurrency_stats,
    concurrency_timeline,
    counter_delta,
    local_request_intervals,
    local_role_summary,
    overlap_factor,
    peak_attribution,
    prefix_cache_ratio,
    stage_windows,
    window_stats,
)
from deep_research.profiling.gpu import query_gpu
from deep_research.profiling.sampler import ProfilerSampler
from deep_research.profiling.scrape import (
    delta_histogram,
    extract_snapshot,
    histogram_mean,
    histogram_quantile,
    parse_prometheus,
)

_METRICS_TEXT = """# HELP vllm:num_requests_running Number of requests running
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="qwen3-30b-a3b-local"} 4.0
vllm:num_requests_waiting{engine="0",model_name="qwen3-30b-a3b-local"} 2.0
vllm:kv_cache_usage_perc{engine="0",model_name="qwen3-30b-a3b-local"} 0.61
vllm:prompt_tokens_total{engine="0"} 1000.0
vllm:generation_tokens_total{engine="0"} 50.0
vllm:time_to_first_token_seconds_bucket{engine="0",le="0.5"} 1.0
vllm:time_to_first_token_seconds_bucket{engine="0",le="1.0"} 3.0
vllm:time_to_first_token_seconds_bucket{engine="0",le="+Inf"} 4.0
vllm:time_to_first_token_seconds_sum{engine="0"} 2.4
vllm:time_to_first_token_seconds_count{engine="0"} 4.0
some_garbage_line without_value
vllm:bad_metric{engine="0"} NaN
"""


def test_parse_prometheus_ignores_comments_garbage_and_nan():
    parsed = parse_prometheus(_METRICS_TEXT)
    assert parsed["vllm:num_requests_running"][0][1] == 4.0
    assert "vllm:bad_metric" not in parsed, "NaN 必须被丢弃而不是写进序列"


def test_extract_snapshot_scalars_and_histograms():
    snap = extract_snapshot(_METRICS_TEXT)
    assert snap["scalars"]["vllm:num_requests_running"] == 4.0
    assert snap["scalars"]["vllm:num_requests_waiting"] == 2.0
    assert snap["scalars"]["vllm:kv_cache_usage_perc"] == pytest.approx(0.61)
    hist = snap["histograms"]["vllm:time_to_first_token_seconds"]
    assert hist["count"] == 4.0 and hist["sum"] == pytest.approx(2.4)
    assert hist["buckets"][1.0] == 3.0


def test_missing_metrics_are_none_not_errors():
    snap = extract_snapshot("# TYPE nothing here\n")
    assert snap["scalars"]["vllm:num_requests_running"] is None
    assert snap["histograms"] == {}
    assert histogram_quantile(None, 0.5) is None
    assert histogram_mean({"sum": None, "count": 0, "buckets": {}}) is None


def test_histogram_quantile_interpolates():
    hist = {"sum": 10.0, "count": 10.0, "buckets": {1.0: 5.0, 2.0: 9.0, 5.0: 10.0}}
    assert histogram_quantile(hist, 0.5) == pytest.approx(1.0)  # 第 5 个落在 1.0 桶
    q90 = histogram_quantile(hist, 0.9)
    assert 1.0 <= q90 <= 2.0  # 第 9 个在 (1,2] 桶内插值
    assert histogram_quantile({"sum": 0, "count": 0, "buckets": {}}, 0.5) is None


def test_delta_histogram_is_per_window_difference():
    before = {"sum": 1.0, "count": 2.0, "buckets": {1.0: 2.0, 5.0: 2.0}}
    after = {"sum": 4.0, "count": 5.0, "buckets": {1.0: 3.0, 5.0: 5.0}}
    delta = delta_histogram(before, after)
    assert delta["count"] == 3.0 and delta["buckets"][1.0] == 1.0
    assert delta_histogram(None, after) is None


# ===== GPU =====


def test_query_gpu_parses_csv_and_na():
    class _Proc:
        returncode = 0
        stdout = "87, 42, 21000, 24564, 320.5, 64\n"

    info = query_gpu(runner=lambda *a, **k: _Proc())
    assert info["gpu_util_percent"] == 87.0 and info["mem_used_mib"] == 21000.0
    assert info["power_w"] == 320.5 and info["temp_c"] == 64.0

    class _ProcNA:
        returncode = 0
        stdout = "N/A, N/A, [N/A], 24564, N/A, 60\n"

    info = query_gpu(runner=lambda *a, **k: _ProcNA())
    assert info["gpu_util_percent"] is None and info["mem_total_mib"] == 24564.0


def test_query_gpu_failure_returns_none():
    def _boom(*a, **k):
        raise FileNotFoundError("nvidia-smi")

    assert query_gpu(runner=_boom) is None

    class _ProcFail:
        returncode = 1
        stdout = ""

    assert query_gpu(runner=lambda *a, **k: _ProcFail()) is None


# ===== 采样器：失败隔离 + 落盘 =====


def test_sampler_survives_total_source_failure(tmp_path):
    ticks = iter([100.0, 100.01, 100.02, 100.03, 101.0])

    sampler = ProfilerSampler(
        tmp_path, interval=0.01, gpu_fn=lambda: None, fetch_fn=lambda url, timeout=3.0: None,
        now=lambda: next(ticks, 101.0),  # 递增时钟：max_seconds 才能到期
    )
    sampler.run(max_seconds=0.05)
    assert sampler.samples >= 1, "即使两个数据源都失败，也必须继续采样并落盘"
    assert sampler.errors["gpu"] >= 1 and sampler.errors["vllm"] >= 1
    gpu_rows = (tmp_path / "gpu_metrics.csv").read_text().splitlines()
    assert gpu_rows[0].startswith("ts,gpu_util_percent")
    assert len(gpu_rows) >= 2
    vllm_rows = (tmp_path / "vllm_metrics.csv").read_text().splitlines()
    assert vllm_rows[0].startswith("ts,num_requests_running")
    summary = json.loads((tmp_path / "sampler_summary.json").read_text())
    assert summary["samples"] >= 1


def test_sampler_writes_only_numeric_schema_no_content(tmp_path):
    """采样产物只含 schema 内数值列——不写任何请求/响应文本。"""
    ticks = iter([100.0, 100.0, 100.0 + 0.001])

    sampler = ProfilerSampler(
        tmp_path, interval=0.001,
        gpu_fn=lambda: {"gpu_util_percent": 50.0, "mem_util_percent": 10.0, "mem_used_mib": 100.0,
                        "mem_total_mib": 200.0, "power_w": 100.0, "temp_c": 50.0},
        fetch_fn=lambda url, timeout=3.0: _METRICS_TEXT,
        now=lambda: next(ticks, 200.0),
    )
    sampler.run(max_seconds=0.002)
    text = (tmp_path / "gpu_metrics.csv").read_text() + (tmp_path / "vllm_metrics.csv").read_text()
    assert "prompt" not in text.lower().replace("prompt_tokens", ""), "不得出现请求内容"
    with open(tmp_path / "vllm_metrics.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows and rows[0]["num_requests_running"] == "4.0"


# ===== 对齐与并发 =====


def test_align_samples_nearest_neighbour_with_tolerance():
    gpu = [{"ts": 10.0, "gpu_util_percent": 50.0}, {"ts": 20.0, "gpu_util_percent": 60.0}]
    vllm = [{"ts": 10.4, "num_requests_running": 3.0}]
    merged = align_samples(gpu, vllm, tolerance=1.5)
    assert len(merged) == 1, "超出容差的 GPU 采样不得被强行配对"
    assert merged[0]["ts"] == 10.0 and merged[0]["num_requests_running"] == 3.0
    assert merged[0]["gpu_util_percent"] == 50.0


def _local_row(role, node, start, end, latency_ms=1000, provider="openai_local"):
    return {"role": role, "node": node, "provider": provider, "started_at": start, "finished_at": end,
            "latency_ms": latency_ms, "input_tokens": 100, "output_tokens": 10, "success": True}


def test_local_intervals_and_concurrency():
    rows = [
        _local_row("researcher_summarizer", "tool_node", 0, 10),
        _local_row("researcher_summarizer", "tool_node", 5, 15),
        _local_row("researcher_main", "llm_call", 20, 30),
        _local_row("draft", "write_draft_report", 0, 5, provider="openai"),  # 云端不算
    ]
    intervals = local_request_intervals(rows)
    assert len(intervals) == 3
    assert concurrency_at(intervals, 7) == 2
    assert concurrency_at(intervals, 25) == 1
    assert concurrency_timeline(intervals, [7, 25]) == [2, 1]
    stats = concurrency_stats([7, 25], [2, 1])
    assert stats["peak"] == 2


def test_window_stats_and_peak_attribution():
    samples = [
        {"ts": 0, "kv_cache_usage_perc": 0.1, "num_requests_waiting": 0},
        {"ts": 5, "kv_cache_usage_perc": 0.97, "num_requests_waiting": 3},
        {"ts": 9, "kv_cache_usage_perc": 0.2, "num_requests_waiting": 1},
    ]
    kv = window_stats(samples, "kv_cache_usage_perc")
    assert kv["max"] == pytest.approx(0.97) and kv["mean"] == pytest.approx((0.1 + 0.97 + 0.2) / 3)

    windows = {"claim_verification": [(4.0, 6.0)], "supervisor": [(0.0, 2.0)]}
    attribution = peak_attribution(samples, windows, "kv_cache_usage_perc")
    assert attribution["ts"] == 5 and attribution["active_nodes"] == ["claim_verification"]


def test_overlap_factor_and_stage_windows():
    assert overlap_factor(220_000, 27_000) == pytest.approx(8.15, abs=0.01)
    assert overlap_factor(1000, 0) is None

    rows = [
        {"node": "claim_verification", "started_at": 100.0, "finished_at": 130.0},
        {"node": "claim_verification", "started_at": 200.0, "finished_at": 210.0},
        {"node": "supervisor", "started_at": 50.0, "finished_at": 90.0},
    ]
    windows = stage_windows(rows)
    assert windows["claim_verification"] == [(100.0, 130.0), (200.0, 210.0)]


def test_counter_delta_and_prefix_cache_ratio():
    rows = [
        {"prefix_cache_hits_total": 10, "prefix_cache_queries_total": 100, "prompt_tokens_total": 1000, "prompt_tokens_cached_total": 400},
        {"prefix_cache_hits_total": 30, "prefix_cache_queries_total": 200, "prompt_tokens_total": 3000, "prompt_tokens_cached_total": 1400},
    ]
    assert counter_delta(rows, "prefix_cache_hits_total") == 20
    ratio = prefix_cache_ratio(rows)
    assert ratio["hit_rate"] == pytest.approx(0.2)
    assert ratio["cached_token_ratio"] == pytest.approx(0.5)
    assert counter_delta(rows, "missing_key") is None


def test_local_role_summary_aggregates():
    rows = [
        _local_row("researcher_summarizer", "tool_node", 0, 10, latency_ms=1000),
        _local_row("researcher_summarizer", "claim_verification", 0, 10, latency_ms=2000),
        _local_row("researcher_main", "llm_call", 0, 10, latency_ms=500),
        _local_row("writer", "final_report_generation", 0, 10, latency_ms=5000, provider="openai"),
    ]
    summary = local_role_summary(rows)
    assert set(summary) == {"researcher_summarizer", "researcher_main"}
    assert summary["researcher_summarizer"]["calls"] == 2
    assert summary["researcher_summarizer"]["summed_latency_ms"] == 3000
    assert summary["researcher_summarizer"]["nodes"] == ["claim_verification", "tool_node"]
