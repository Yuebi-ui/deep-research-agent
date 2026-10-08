"""Phase 3C-2 / E8：质量指标与配对判定工具的单元测试。

这些函数是 E8 结论的"客观证据链"，必须自身可复算、行为固定：
- 报告结构/引用指标；
- claim 覆盖（保守的 shingle 匹配语义）；
- 统计与 delta（median/absolute/percent）；
- judge 标签 → variant 的映射（位置随机化不得引入方向性错误）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from deep_research.benchmark.quality import (
    claim_coverage,
    delta,
    normalize_text,
    report_metrics,
    summarize,
    text_hash,
    verification_metrics,
    writer_input_metrics,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

_REPORT = """# Title

## 1. Introduction
LangGraph provides stateful orchestration for multi-actor LLM applications [1].

## 2. Architecture
- StateGraph nodes [1]
- Checkpointing every super-step [2]

| feature | value |
|---|---|
| checkpoint | yes |

## 3. Conclusion
See https://example.com/a and https://example.com/b [2].
"""


def test_report_metrics_structure_and_citations():
    m = report_metrics(_REPORT)
    assert m["non_empty"] is True
    assert m["h1_count"] == 1 and m["h2_count"] == 3
    assert m["h2_titles"][0].startswith("1. Introduction")
    assert m["citation_markers"] == 4 and m["unique_citation_indices"] == 2
    assert m["url_count"] == 2 and m["unique_url_count"] == 2
    assert m["list_items"] == 2 and m["table_rows"] == 2  # header + 1 数据行（分隔行不计）
    assert m["has_intro_section"] and m["has_conclusion_section"]
    assert report_metrics("")["non_empty"] is False


def test_claim_coverage_exact_paraphrase_and_control():
    # 1) 原样引用 → exact 命中
    claim = "LangGraph provides stateful orchestration for multi-actor LLM applications"
    cov = claim_coverage([claim], _REPORT)
    assert cov["covered"] == 1 and cov["exact_matches"] == 1
    assert cov["details"][0]["exact"] is True and cov["details"][0]["containment"] >= 0.9

    # 2) 改写（无 5-gram 原样窗口）但复用实词 → 由 containment 判为覆盖
    paraphrase = "Orchestration of stateful multi-actor LLM applications is provided by StateGraph nodes"
    p = claim_coverage([paraphrase], _REPORT)
    assert p["details"][0]["exact"] is False, "改写不应有原样 shingle 命中"
    assert p["covered"] == 1, "高 containment 的改写应判为覆盖（校准规则）"
    assert p["details"][0]["containment"] >= 0.7

    # 3) 无关断言（对照集实测最高 0.56）→ 未覆盖
    control = "Redis heartbeat lease ownership prevents duplicate worker execution during failover"
    c = claim_coverage([control], _REPORT)
    assert c["covered"] == 0 and c["details"][0]["containment"] < 0.7
    assert c["uncovered_claims"] == [control]

    # 4) 完全无关的复述 → 未覆盖
    rewritten = claim_coverage(
        ["The framework enables agent workflows that keep memory between steps"], _REPORT
    )
    assert rewritten["covered"] == 0

    # 5) 短 claim 走整串包含
    assert claim_coverage(["stateful orchestration"], _REPORT)["covered"] == 1
    assert claim_coverage([], _REPORT)["coverage"] is None


def test_verification_metrics_counts_and_lists():
    verdicts = [
        {"claim_text": "A", "verdict": "SUPPORTED"},
        {"claim_text": "B", "verdict": "PARTIAL"},
        {"claim_text": "C", "verdict": "UNSUPPORTED"},
        {"claim_text": "D", "verdict": "UNSUPPORTED"},
    ]
    m = verification_metrics(verdicts)
    assert m["total_claims"] == 4
    assert m["by_verdict"] == {"SUPPORTED": 1, "PARTIAL": 1, "UNSUPPORTED": 2}
    assert m["unsupported_rate"] == 0.5
    assert m["supported_claims"] == ["A", "B"]
    assert m["unsupported_claims"] == ["C", "D"]


def test_summarize_and_delta_math():
    s = summarize([10, 30, 20])
    assert (s["min"], s["median"], s["max"]) == (10, 20, 30)
    assert summarize([10, 20, 30, 40])["median"] == 25  # 偶数取均值
    assert summarize([])["median"] is None
    assert summarize([None, 5])["n"] == 1

    d = delta({"median": 100.0}, {"median": 70.0})["median"]
    assert d["absolute"] == -30.0 and d["percent"] == pytest.approx(-30.0)
    d0 = delta({"median": 0}, {"median": 5})["median"]
    assert d0["absolute"] == 5 and d0["percent"] is None, "基线为 0 时不得伪造百分比"


def test_writer_input_metrics_holds_no_content():
    m = writer_input_metrics(
        research_brief="x" * 100, draft_report="y" * 500, notes=["n1", "n2"],
        warning="w" * 10, source_count=7, prompt="secret body",
    )
    assert m["research_brief_chars"] == 100 and m["draft_report_chars"] == 500
    assert m["notes_count"] == 2 and m["notes_chars"] == 4 and m["source_count"] == 7
    assert m["prompt_chars"] == len("secret body")
    assert m["prompt_hash"] == text_hash("secret body")
    assert "secret body" not in str(m), "指标里不得出现原文"


def test_normalize_text_handles_cjk_and_punct():
    assert normalize_text("Hello, World! 你好") == "hello world 你 好"


# ===== judge 映射（位置随机化）=====


def _load_judge():
    spec = importlib.util.spec_from_file_location(
        "e8_paired_judge_cli", REPO_ROOT / "scripts" / "experiments" / "e8_paired_judge.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_judge_label_mapping_respects_randomized_order():
    judge = _load_judge()
    verdict = {"coverage": "1", "unsupported": "2", "structure": "tie",
               "citations": "1", "overall": "2", "confidence": 0.8, "reasons": "r"}

    # 顺序未翻转：报告1 = ON
    m = judge.map_verdict(verdict, "thinking-on", "thinking-off")
    assert m["coverage"] == "thinking-on" and m["unsupported"] == "thinking-off"
    assert m["structure"] == "tie" and m["overall"] == "thinking-off"

    # 顺序翻转：报告1 = OFF —— 同一个 "1" 标签必须映射到 thinking-off
    m_flip = judge.map_verdict(verdict, "thinking-off", "thinking-on")
    assert m_flip["coverage"] == "thinking-off" and m_flip["unsupported"] == "thinking-on"

    assert judge.map_verdict(None, "a", "b") is None
    assert judge.map_verdict({"overall": "garbage"}, "a", "b")["overall"] is None
