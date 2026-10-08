"""Phase 3C-3 / E9：研究深度对比工具的单元测试。

E9 的判定依赖两个纯逻辑：
- draft micro 的 topic 重叠（章节标题 Jaccard）；
- paired 下游对比里"depth contraction"的触发条件（OFF 相对 ON 下降 >25%）。
两者都必须自身可复算、行为固定。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_section_titles_and_topic_jaccard():
    micro = _load("scripts/experiments/e9_draft_micro_ab.py", "e9_micro_mod")

    text = "# Title\n## 1. Introduction\nbody\n### Details\n## 2. Use Cases\n"
    assert micro._section_titles(text) == ["1. Introduction", "Details", "2. Use Cases"]

    a = ["1. Introduction", "2. Use Cases", "3. Conclusion"]
    b = ["1. Introduction", "2. Architecture", "3. Conclusion"]
    assert micro._topic_jaccard(a, b) == pytest.approx(2 / 4)
    assert micro._topic_jaccard(a, a) == 1.0
    assert micro._topic_jaccard([], a) is None


def _run(run_id, variant, *, depth=None, draft=None, covered=7, total=7):
    return {
        "run_id": run_id,
        "variant": variant,
        "draft": {"input_tokens": 1000, "reasoning_tokens": 500, "output_tokens": 3000, **(draft or {})},
        "depth": {
            "supervisor_rounds": 3, "researcher_iterations": 10, "tool_node_calls": 8,
            "search_calls": 14, "search_results": 30, "unique_urls": 20, "claim_total": 10,
            **(depth or {}),
        },
        "quality": {"supported_claim_coverage": {"covered": covered, "total": total}},
    }


def test_paired_compare_flags_depth_contraction():
    analysis = _load("scripts/experiments/e9_analysis.py", "e9_analysis_mod")

    healthy = analysis.paired_compare(_run("a1", "thinking-on"), _run("b1", "thinking-off", depth={"researcher_iterations": 9, "search_calls": 13}))
    assert healthy["depth_contraction_fields"] == [], "±1 的自然波动不得判为收缩"

    contracted = analysis.paired_compare(
        _run("a2", "thinking-on"),
        _run("b2", "thinking-off", depth={"researcher_iterations": 6, "search_calls": 7, "unique_urls": 12}),
    )
    assert set(contracted["depth_contraction_fields"]) == {"researcher_iterations", "search_calls", "unique_urls"}

    # 上升方向不触发（缩水才触发）
    grown = analysis.paired_compare(_run("a3", "thinking-on"), _run("b3", "thinking-off", depth={"researcher_iterations": 16}))
    assert grown["depth_contraction_fields"] == []


def test_paired_compare_reports_draft_and_coverage_deltas():
    analysis = _load("scripts/experiments/e9_analysis.py", "e9_analysis_mod2")
    pair = analysis.paired_compare(
        _run("a1", "thinking-on"),
        _run("b1", "thinking-off", draft={"reasoning_tokens": 0, "output_tokens": 2400}, covered=6),
    )
    assert pair["draft_deltas"]["reasoning_tokens"]["off"] == 0
    assert pair["draft_deltas"]["output_tokens"]["percent"] == pytest.approx(-20.0)
    assert pair["coverage"] == {"on_covered": 7, "off_covered": 6, "on_total": 7, "off_total": 7}


def test_summarize_variants_median_and_delta():
    analysis = _load("scripts/experiments/e9_analysis.py", "e9_analysis_mod3")
    runs = [
        _run("a1", "thinking-on"), _run("a2", "thinking-on"),
        _run("b1", "thinking-off", depth={"search_calls": 7}), _run("b2", "thinking-off", depth={"search_calls": 9}),
    ]
    s = analysis.summarize_variants(runs)
    assert s["thinking-on"]["search_calls"]["median"] == 14
    assert s["thinking-off"]["search_calls"]["median"] == 8
    assert s["delta_off_minus_on"]["search_calls"]["median"]["absolute"] == -6
