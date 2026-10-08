"""Phase 4B：固定 workload manifest + 受控并发 burst 的单元测试。

覆盖：
- workload：确定性渲染、唯一性、指纹、篡改检测、上下文上限；
- burst：并发闸门真的限流、单请求失败被隔离、correctness/failure 统计、
  hard gates 判定、失败分类（oom / overflow）。

全部离线（注入假 counter / 假 sender），不触碰网络与 vLLM。
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from deep_research.profiling.burst import (
    burst_stats,
    classify_failure,
    concurrency_profile,
    run_burst,
    validate_summary_response,
)
from deep_research.profiling.workload import (
    build_manifest,
    manifest_fingerprint,
    render_prompt,
    verify_manifest,
)

_VALID_CONTENT = '{"summary": "一段摘要", "key_excerpts": "引文一, 引文二"}'


def _fake_counter(text: str) -> int:
    """deterministic、可离线：4 字符 ≈ 1 token。"""
    return max(1, len(text) // 4)


def _manifest(shapes=(900, 1200, 1500), max_tokens=100):
    return build_manifest(_fake_counter, shapes=list(shapes), max_tokens=max_tokens)


# ===== workload =====


def test_render_prompt_is_deterministic_and_fits_target():
    p1, n1 = render_prompt(12345, 1200, _fake_counter)
    p2, n2 = render_prompt(12345, 1200, _fake_counter)
    assert p1 == p2 and n1 == n2
    assert n1 <= 1200
    assert _fake_counter(p1) == n1


def test_render_prompt_differs_across_seeds():
    a, _ = render_prompt(1, 900, _fake_counter)
    b, _ = render_prompt(2, 900, _fake_counter)
    assert a != b


def test_render_prompt_rejects_target_below_template_overhead():
    with pytest.raises(ValueError):
        render_prompt(1, 10, _fake_counter)


def test_manifest_is_stable_and_unique():
    m1, m2 = _manifest(), _manifest()
    assert m1["fingerprint"] == m2["fingerprint"]
    assert [r["prompt"] for r in m1["requests"]] == [r["prompt"] for r in m2["requests"]]
    hashes = [r["prompt_sha1"] for r in m1["requests"]]
    assert len(set(hashes)) == len(hashes)


def test_manifest_detects_prompt_tampering_via_sha1():
    manifest = _manifest()
    assert verify_manifest(manifest) == []
    # 改正文但不动申报的 sha1 → 由逐条 sha1 校验抓出
    manifest["requests"][1]["prompt"] = "tampered"
    assert any("sha1" in p for p in verify_manifest(manifest))


def test_manifest_detects_shape_tampering_via_fingerprint():
    manifest = _manifest()
    # 改申报的 sha1（不改正文）→ 指纹与实际内容不一致
    manifest["requests"][1]["prompt_sha1"] = "deadbeef0000"
    problems = verify_manifest(manifest)
    assert any("fingerprint" in p for p in problems)
    assert any("sha1" in p for p in problems)


def test_manifest_rejects_context_overflow():
    with pytest.raises(ValueError, match="上下文上限"):
        # 1000 目标 + 8000 max_tokens 超过 8192
        build_manifest(_fake_counter, shapes=[1000], max_tokens=8000)


def test_manifest_fingerprint_is_order_sensitive():
    m = _manifest()
    rev = list(reversed(m["requests"]))
    assert manifest_fingerprint(rev) != manifest_fingerprint(m["requests"])


# ===== 响应校验 / 失败分类 =====


@pytest.mark.parametrize("content,ok,defect", [
    (_VALID_CONTENT, True, None),
    ("```json\n" + _VALID_CONTENT + "\n```", True, None),
    ('{"summary": "x", "key_excerpts": ["a", "b"]}', True, None),
    ("", False, "empty_response"),
    ("not json at all", False, "json_parse_failed"),
    ('{"summary": "x"}', False, "missing_key_excerpts"),
    ('{"key_excerpts": "y"}', False, "missing_summary"),
    ('{"summary": "  ", "key_excerpts": "y"}', False, "missing_summary"),
])
def test_validate_summary_response(content, ok, defect):
    got_ok, got_defect = validate_summary_response(content)
    assert got_ok is ok
    assert got_defect == defect


def test_classify_failure():
    assert classify_failure(500, "CUDA out of memory") == "oom"
    assert classify_failure(400, "This model's maximum context length is 8192 tokens") == "context_overflow"
    assert classify_failure(429, "") == "rate_limited"
    assert classify_failure(None, "ConnectionError: refused") == "transport_error"
    assert classify_failure(503, "server busy") == "http_503"


# ===== burst 执行 =====


def _specs(n: int) -> list[dict]:
    return [{"index": i, "prompt": f"p{i}", "prompt_sha1": f"h{i}", "prompt_tokens": 10, "max_tokens": 5}
            for i in range(n)]


def test_run_burst_respects_concurrency_gate():
    lock = threading.Lock()
    state = {"inflight": 0, "peak": 0}

    def send(spec):
        with lock:
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
        time.sleep(0.05)
        with lock:
            state["inflight"] -= 1
        return {"ok": True, "status": 200, "content": _VALID_CONTENT,
                "usage": {"prompt_tokens": 10, "completion_tokens": 3}, "error": None}

    for concurrency in (2, 4):
        state.update(inflight=0, peak=0)
        burst = asyncio.run(run_burst(_specs(8), concurrency=concurrency, send_fn=send))
        assert state["peak"] == concurrency
        assert len(burst["results"]) == 8
        assert all(r["ok"] and r["correct"] for r in burst["results"])
        assert concurrency_profile(burst["results"])["peak"] <= concurrency


def test_run_burst_isolates_failures_and_counts_defects():
    def send(spec):
        if spec["index"] == 0:
            raise RuntimeError("boom")
        if spec["index"] == 1:
            return {"ok": False, "status": 500, "content": None, "usage": None,
                    "error": "CUDA out of memory"}
        if spec["index"] == 2:
            return {"ok": True, "status": 200, "content": "not json", "usage": {},
                    "error": None}
        return {"ok": True, "status": 200, "content": _VALID_CONTENT,
                "usage": {"prompt_tokens": 10, "completion_tokens": 3}, "error": None}

    burst = asyncio.run(run_burst(_specs(4), concurrency=2, send_fn=send))
    stats = burst_stats(burst)
    assert stats["requests"] == 4
    assert stats["failures"] == 2
    assert stats["failure_kinds"] == {"transport_error": 1, "oom": 1}
    assert stats["incorrect"] == 1
    assert stats["defect_counts"] == {"json_parse_failed": 1}
    assert stats["correctness_pass"] is False
    # 只有成功返回 usage 的请求计入 token 统计（失败请求不臆造 token 数）
    assert stats["prompt_tokens_total"] == 10
    assert stats["completion_tokens_total"] == 3


def test_burst_stats_all_pass():
    def send(spec):
        return {"ok": True, "status": 200, "content": _VALID_CONTENT,
                "usage": {"prompt_tokens": 100, "completion_tokens": 40}, "error": None}

    burst = asyncio.run(run_burst(_specs(4), concurrency=4, send_fn=send))
    stats = burst_stats(burst)
    assert stats["correctness_pass"] is True
    assert stats["failures"] == 0 and stats["incorrect"] == 0
    assert stats["throughput_tok_per_s"] is not None
    # 假 sender 瞬时返回 → 0.5s 步长的重建曲线只用于"不超过闸门"的健全性检查
    assert stats["concurrency_profile"]["peak"] <= 4


def test_concurrency_profile_counts_overlap():
    results = [
        {"start_offset_s": 0.0, "end_offset_s": 2.0},
        {"start_offset_s": 1.0, "end_offset_s": 3.0},
        {"start_offset_s": 5.0, "end_offset_s": 6.0},
    ]
    profile = concurrency_profile(results, step=1.0)
    assert profile["peak"] == 2
    assert profile["series"][0] == 1 and profile["series"][1] == 2


# ===== 与生产配置的一致性 =====


def test_harness_uses_real_production_prompt_template():
    """prompt 必须是真实 SUMMARIZE_PROMPT 模板（正文插值 + 冻结日期），
    否则 output 形状与生产不可比。"""
    from deep_research.prompts.summarize_webpage import SUMMARIZE_PROMPT
    from deep_research.profiling.workload import FROZEN_DATE

    prompt, _ = render_prompt(42, 900, _fake_counter)
    head, tail = SUMMARIZE_PROMPT.split("{webpage_content}")
    assert prompt.startswith(head)
    assert prompt.endswith(tail.format(date=FROZEN_DATE))
    assert "<webpage_content>" in prompt
