"""Phase 5B：research generation / stale-result fencing 的单元测试。

口径：

- lineage 判定（`research_generation >= 1`）只看 checkpoint 状态事实；
- 投机 lineage 的研究收尾必须写隔离字段 `speculative_research`（串行写 `notes`）；
- ACCEPT 时只有 `payload.generation == active_generation` 才允许并入 `notes`，
  否则抛 `ResearchFenceError`（显式失败，绝不静默降级）；
- retry 分类里 fence 错误是**不可重试**的领域错误。
"""

from __future__ import annotations

import pytest

from deep_research.research_seed import (
    ResearchFenceError,
    active_generation,
    build_research_seed,
    build_speculative_payload,
    fenced_accept_update,
    quarantine_update,
    seed_fingerprint,
    speculative_lineage,
)

_BRIEF = "Research LangGraph: architecture, use cases, best practices."


# ===== lineage 判定 =====


@pytest.mark.parametrize("state,expected", [
    (None, False),
    ({}, False),
    ({"research_generation": 0}, False),
    ({"research_generation": 1}, True),
    ({"research_generation": 3}, True),
    ({"research_generation": None}, False),
    ({"research_generation": "2"}, True),
])
def test_speculative_lineage_marker(state, expected):
    assert speculative_lineage(state) is expected


def test_active_generation_coerces_safely():
    assert active_generation({}) == 0
    assert active_generation({"research_generation": "3"}) == 3
    assert active_generation({"research_generation": None}) == 0


# ===== payload / quarantine =====


def test_speculative_payload_is_fingerprinted_and_generation_tagged():
    payload = build_speculative_payload(generation=2, research_brief=_BRIEF, notes=["a", "b"])
    assert payload["generation"] == 2
    assert payload["notes"] == ["a", "b"]
    assert payload["seed_fingerprint"] == seed_fingerprint(build_research_seed(_BRIEF))
    # notes 必须是拷贝（不得暴露调用方列表别名）
    src = ["x"]
    p = build_speculative_payload(generation=1, research_brief=_BRIEF, notes=src)
    src.append("y")
    assert p["notes"] == ["x"]


def test_quarantine_serial_lineage_writes_notes_verbatim():
    state = {"research_brief": _BRIEF}
    out = quarantine_update(state, ["n1", "n2"])
    assert out == {"notes": ["n1", "n2"]}, "串行 lineage 行为必须与历史逐字一致"
    assert "speculative_research" not in out


def test_quarantine_speculative_lineage_isolates_result():
    state = {"research_brief": _BRIEF, "research_generation": 1}
    out = quarantine_update(state, ["n1"])
    assert "notes" not in out, "投机结果在 ACCEPT 前绝不能进入 notes"
    payload = out["speculative_research"]
    assert payload["generation"] == 1 and payload["notes"] == ["n1"]


# ===== fenced accept =====


def test_fenced_accept_admits_matching_generation_once():
    state = {
        "research_generation": 2,
        "speculative_research": {"generation": 2, "notes": ["g2-a", "g2-b"]},
    }
    out = fenced_accept_update(state)
    assert out == {"notes": ["g2-a", "g2-b"]}
    # notes 是拷贝（add reducer 会自行拼接）
    state["speculative_research"]["notes"].append("mutated")
    assert out["notes"] == ["g2-a", "g2-b"]


@pytest.mark.parametrize("state", [
    {},                                                          # 无 lineage / 无 payload
    {"research_generation": 1},                                  # payload 缺失
    {"research_generation": 2, "speculative_research": {"generation": 1, "notes": ["stale"]}},
    {"research_generation": 1, "speculative_research": {"generation": 2, "notes": ["future"]}},
    {"research_generation": 2, "speculative_research": {"notes": ["no-generation"]}},
])
def test_fenced_accept_rejects_stale_or_missing(state):
    with pytest.raises(ResearchFenceError):
        fenced_accept_update(state)


def test_fenced_accept_error_mentions_generations_only():
    """错误信息包含代数与计数（排障够用），不包含研究正文。"""
    state = {
        "research_generation": 2,
        "speculative_research": {"generation": 1, "notes": ["secret research text"]},
    }
    with pytest.raises(ResearchFenceError) as exc:
        fenced_accept_update(state)
    msg = str(exc.value)
    assert "active_generation=2" in msg and "payload_generation=1" in msg
    assert "secret research text" not in msg


# ===== retry 分类 =====


def test_fence_error_is_non_retryable_domain_error():
    from backend.runtime.retry import classify

    decision = classify(ResearchFenceError("x"))
    assert decision.retryable is False
    assert decision.code == "RESEARCH_FENCE_ERROR"
