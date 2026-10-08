"""Phase 4B：受控并发的 burst 执行器与统计（claim_verification admission window）。

职责边界：

- 调度与统计逻辑是**纯函数/可注入**的（`send_fn` 由调用方提供），测试里用假
  sender 即可覆盖，不接触网络；
- 只记录数值、时间戳与"是否通过校验"，**不落任何响应正文**；
- 单个请求失败绝不抛出到调用方（记为该请求的 error 字段），burst 必须跑完。

并发语义：`concurrency` 个槽位（asyncio.Semaphore）直接闸住"同时在飞的本地
请求数"，与生产里给 claim 级并发加信号量是同一个口径（每个活跃 claim 同时
只有 1 个本地调用在飞）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Iterable, Sequence

from deep_research.utils import parse_json_response

_OOM_MARKERS = ("out of memory", "cuda error", "cuda out of memory", "no available memory")
_OVERFLOW_MARKERS = ("maximum context length", "longer than the maximum model length", "context length")


def validate_summary_response(content: str) -> tuple[bool, str | None]:
    """按生产的解析链路校验响应（与 tool.summarize_webpage_content 一致）。

    生产路径：parse_json_response → 合并 key_excerpts（list→str）→ Summary(**data)，
    即要求 summary 与 key_excerpts 两个键都存在且为字符串。
    """
    if not content or not content.strip():
        return False, "empty_response"
    try:
        data = parse_json_response(content)
    except Exception:
        return False, "json_parse_failed"
    if not isinstance(data, dict):
        return False, "json_not_object"
    key_excerpts = data.get("key_excerpts")
    if isinstance(key_excerpts, list):
        key_excerpts = ", ".join(str(x) for x in key_excerpts)
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return False, "missing_summary"
    if not isinstance(key_excerpts, str) or not key_excerpts.strip():
        return False, "missing_key_excerpts"
    return True, None


def classify_failure(status: int | None, error_text: str) -> str:
    """把 HTTP/传输错误归类（硬门槛判定用）。"""
    text = (error_text or "").lower()
    if any(marker in text for marker in _OOM_MARKERS):
        return "oom"
    if any(marker in text for marker in _OVERFLOW_MARKERS):
        return "context_overflow"
    if status is None:
        return "transport_error"
    if status == 429:
        return "rate_limited"
    return f"http_{status}"


async def run_burst(
    requests: Sequence[dict],
    *,
    concurrency: int,
    send_fn: Callable[[dict], dict],
    to_thread: Callable[..., Any] = asyncio.to_thread,
    now: Callable[[], float] = time.monotonic,
) -> dict:
    """以固定并发闸门执行全部请求，返回 {wall_s, results:[...]}。

    `send_fn(spec) -> {"ok": bool, "content": str|None, "usage": dict|None, "status": int|None, "error": str|None}`
    是同步函数（内部做 HTTP），统一丢到线程池执行 —— 与生产里
    `asyncio.to_thread(tavily_search, ...)` 的阻塞语义一致。
    """
    if concurrency < 1:
        raise ValueError("concurrency 必须 >= 1")
    semaphore = asyncio.Semaphore(concurrency)
    t0 = now()

    async def _one(spec: dict) -> dict:
        async with semaphore:
            started = now()
            try:
                outcome = await to_thread(send_fn, spec)
            except Exception as exc:  # noqa: BLE001 —— 单请求失败不得中断 burst
                outcome = {"ok": False, "status": None, "content": None, "usage": None,
                           "error": f"{type(exc).__name__}: {exc}"}
            finished = now()
        usage = outcome.get("usage") or {}
        ok = bool(outcome.get("ok"))
        content = outcome.get("content") or ""
        correct, defect = (False, "not_sent") if not ok else validate_summary_response(content)
        failure_kind = None
        if not ok:
            failure_kind = classify_failure(outcome.get("status"), outcome.get("error") or "")
        return {
            "index": spec["index"],
            "prompt_sha1": spec["prompt_sha1"],
            "prompt_tokens_expected": spec.get("prompt_tokens"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "start_offset_s": round(started - t0, 3),
            "end_offset_s": round(finished - t0, 3),
            "latency_s": round(finished - started, 3),
            "http_status": outcome.get("status"),
            "finish_reason": outcome.get("finish_reason"),
            "truncated": outcome.get("finish_reason") == "length",
            "ok": ok,
            "failure_kind": failure_kind,
            "error": (outcome.get("error") or None),
            "correct": bool(correct),
            "defect": defect,
        }

    results = await asyncio.gather(*(_one(spec) for spec in requests))
    return {"concurrency": concurrency, "wall_s": round(now() - t0, 3), "results": list(results)}


# ===== 统计（纯函数）=====


def _quantile(sorted_values: Sequence[float], q: float) -> float | None:
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, max(0, int(-(-q * len(sorted_values) // 1)) - 1))
    return sorted_values[idx]


def _dist(values: Iterable[float | None]) -> dict:
    vals = sorted(float(v) for v in values if isinstance(v, (int, float)))
    if not vals:
        return {"n": 0, "p50": None, "p90": None, "mean": None, "max": None}
    return {
        "n": len(vals),
        "p50": _quantile(vals, 0.5),
        "p90": _quantile(vals, 0.9),
        "mean": sum(vals) / len(vals),
        "max": vals[-1],
    }


def concurrency_profile(results: Sequence[dict], step: float = 0.5) -> dict:
    """由各请求的 [start, end) 区间重建在飞并发曲线（用于验证闸门真的生效）。"""
    spans = [
        (float(r["start_offset_s"]), float(r["end_offset_s"]))
        for r in results
        if isinstance(r.get("start_offset_s"), (int, float))
        and isinstance(r.get("end_offset_s"), (int, float))
    ]
    if not spans:
        return {"peak": 0, "mean": 0.0, "p90": 0, "series": []}
    end = max(e for _, e in spans)
    series: list[int] = []
    t = 0.0
    while t <= end:
        series.append(sum(1 for s, e in spans if s <= t < e))
        t += step
    ordered = sorted(series)
    return {
        "peak": max(series),
        "mean": round(sum(series) / len(series), 3),
        "p90": _quantile(ordered, 0.9),
        "series": series,
        "step_s": step,
    }


def burst_stats(burst: dict) -> dict:
    """把一次 burst 的原始结果汇总为 sweep 表所需的指标。"""
    results = burst["results"]
    latencies = [r["latency_s"] for r in results if r.get("ok")]
    completion_tokens = [r["completion_tokens"] for r in results if r.get("completion_tokens")]
    prompt_tokens = [r["prompt_tokens"] for r in results if r.get("prompt_tokens")]
    failures = [r for r in results if not r.get("ok")]
    incorrect = [r for r in results if r.get("ok") and not r.get("correct")]
    failure_kinds: dict[str, int] = {}
    for r in failures:
        kind = r.get("failure_kind") or "unknown"
        failure_kinds[kind] = failure_kinds.get(kind, 0) + 1

    wall = burst["wall_s"]
    return {
        "concurrency": burst.get("concurrency"),
        "requests": len(results),
        "wall_s": wall,
        "latency_s": _dist(latencies),
        "prompt_tokens_total": sum(prompt_tokens) if prompt_tokens else None,
        "prompt_tokens_mean": (sum(prompt_tokens) / len(prompt_tokens)) if prompt_tokens else None,
        "completion_tokens_total": sum(completion_tokens) if completion_tokens else None,
        "completion_tokens_mean": (sum(completion_tokens) / len(completion_tokens)) if completion_tokens else None,
        "throughput_req_per_s": (len(results) / wall) if wall else None,
        "throughput_tok_per_s": (
            (sum(completion_tokens) / wall) if completion_tokens and wall else None
        ),
        "failures": len(failures),
        "failure_kinds": failure_kinds,
        "truncated": sum(1 for r in results if r.get("truncated")),
        "incorrect": len(incorrect),
        "correctness_pass": len(failures) == 0 and len(incorrect) == 0,
        "defect_counts": _count_defects(incorrect),
        "concurrency_profile": concurrency_profile(results),
    }


def _count_defects(rows: Sequence[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        defect = r.get("defect") or "unknown"
        counts[defect] = counts.get(defect, 0) + 1
    return counts


def compare_sweep_points(points: Sequence[dict]) -> dict:
    """给定各并发档的 burst_stats，按 hard gates + burst wall 排出候选。

    winner 判据（与 Phase 4B 任务书一致）：
    1) 必须通过全部 hard gates（failure=0、correctness 全过、无 oom/overflow）；
    2) PRIMARY = burst wall（越低越好）；
    3) 若 Top-2 的 wall 差异落在噪声内，取并发更低 / preemption 更少 / queue 尾更低的。
    """
    valid = [p for p in points if p.get("hard_gates_pass")]
    if not valid:
        return {"valid": [], "best": None, "note": "没有任何档位通过 hard gates"}
    ranked = sorted(valid, key=lambda p: p["wall_s"])
    return {"valid": [p["concurrency"] for p in ranked], "best": ranked[0]["concurrency"], "ranked": ranked}
