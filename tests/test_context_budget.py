"""P1 context budget 测试（Phase 2）。

覆盖任务包要求的边界：

* 恰好低于 / 恰好等于 / 超 1 token
* 极长网页、单块超预算（硬截断）、多搜索结果
* Unicode / 中文、空内容、malformed/巨大 tool result
* local / cloud 不同 context capability（cloud 未知 limit → 不裁剪）
* limit 来源优先级（config > env > probe）
* 预算决策全量记录（budget_events.jsonl）

计数确定性：单测注入「1 字符 = 1 token」计数器，预算等式可逐字符断言。
"""

from __future__ import annotations

import json


from deep_research import context_budget as cb


class CharCounter:
    """确定性计数器：1 字符 = 1 token。"""

    name = "test_char_counter"

    def count(self, text: str) -> int:
        return len(text or "")


COUNTER = CharCounter()


def _limits(limit: int, reserve: int = 100, safety: int = 50) -> cb.BudgetLimits:
    return cb.BudgetLimits(
        context_limit=limit,
        limit_source="test",
        reserved_output=reserve,
        safety_margin=safety,
        counter_name=COUNTER.name,
    )


def _build(content: str) -> str:
    return "P:" + content


# ===== 边界：低于 / 等于 / 超 1 =====


def test_content_below_budget_left_untouched():
    limits = _limits(1000)  # hard ceiling 850, overhead 2 -> budget 848
    content = "x" * 800
    prompt, decision = cb.fit_prompt(
        build_prompt=_build, content=content, query="", limits=limits, counter=COUNTER
    )
    assert prompt == "P:" + content
    assert decision.applied is False
    assert decision.reason == "fits"
    assert decision.final_tokens == 802
    assert decision.final_tokens + limits.reserved_output + limits.safety_margin <= limits.context_limit


def test_content_exactly_at_budget_fits():
    limits = _limits(1000)  # budget = 850 - 2 = 848
    content = "x" * 848
    prompt, decision = cb.fit_prompt(
        build_prompt=_build, content=content, query="", limits=limits, counter=COUNTER
    )
    assert decision.applied is False
    assert decision.final_tokens == 850  # 恰好等于 hard ceiling
    assert prompt == "P:" + content


def test_content_one_token_over_is_trimmed():
    limits = _limits(1000)
    content = "a" * 849
    prompt, decision = cb.fit_prompt(
        build_prompt=_build, content=content, query="", limits=limits, counter=COUNTER
    )
    assert decision.applied is True
    assert decision.reason.startswith("trimmed")
    assert decision.final_tokens <= 850
    assert decision.trimmed_tokens is not None and decision.trimmed_tokens >= 1


def test_budget_exhausted_when_reserve_eats_limit():
    limits = _limits(200, reserve=150, safety=60)  # ceiling -10 → 0 预算
    prompt, decision = cb.fit_prompt(
        build_prompt=_build, content="hello", query="", limits=limits, counter=COUNTER
    )
    assert decision.applied is True
    assert decision.reason == "budget_exhausted"
    assert decision.chunks_kept == 0


# ===== 相关性裁剪语义 =====


def _blocks(n: int, size: int = 300, hot_index: int = 7, hot_word: str = "ZZZ") -> str:
    parts = []
    for i in range(n):
        body = f"block-{i:02d} " + "f" * size
        if i == hot_index:
            body = f"block-{i:02d} {hot_word} " + "g" * size
        parts.append(body)
    return "\n\n".join(parts)


def test_trim_keeps_relevant_block_and_preserves_order():
    limits = _limits(1400)  # ceiling 1250, budget 1248-2=1248? -> overhead 2
    content = _blocks(10, size=300, hot_index=7, hot_word="LangGraph")
    prompt, decision = cb.fit_prompt(
        build_prompt=_build, content=content, query="LangGraph use cases",
        limits=limits, counter=COUNTER,
    )
    assert decision.applied is True
    assert decision.final_tokens <= limits.context_limit - limits.reserved_output - limits.safety_margin
    assert "LangGraph" in prompt, "与 query 相关的块必须被保留"
    assert "[... 内容过长" in prompt, "发生裁剪时必须有显式标记"
    # 时序保持：保留块按原始顺序出现
    kept = [f"block-{i:02d}" for i in range(10) if f"block-{i:02d}" in prompt]
    assert kept == sorted(kept)
    # 被保留的块必须是原文子串（不破坏引用完整性）
    for marker in kept:
        idx = content.index(marker)
        assert content[idx:idx + 500].split("\n\n")[0] in prompt


def test_huge_single_block_hard_truncated_not_dropped():
    limits = _limits(1200)  # ceiling 1050, budget ~1048
    # 无分段长文：窗口切分后每个窗口（4000/2000 字符）都装不下预算，
    # 必须走「保留最相关块 + token 硬截断」路径，而不是整页丢弃
    content = "q " + "z" * 5998
    prompt, decision = cb.fit_prompt(
        build_prompt=_build, content=content, query="q", limits=limits, counter=COUNTER
    )
    assert decision.applied is True
    assert decision.reason == "trimmed_hard_chunk"
    assert decision.final_tokens <= 1050
    assert prompt.startswith("P:\n\n[... 内容过长")
    assert "z" in prompt, "硬截断应保留内容前缀而不是整块丢弃"


def test_unicode_chinese_content_cut_on_block_boundary():
    limits = _limits(700)  # budget ≈ 550
    blocks = []
    for i in range(8):
        word = "深研究" if i == 5 else "填充"
        blocks.append(f"第{i}段 {word}" + "文" * 100)
    content = "\n\n".join(blocks)
    prompt, decision = cb.fit_prompt(
        build_prompt=_build, content=content, query="深研究", limits=limits, counter=COUNTER
    )
    assert decision.applied is True
    assert "第5段" in prompt, "与 query 相关的中文块要保留"
    for i in range(8):
        chunk = blocks[i]
        if f"第{i}段" in prompt:
            assert chunk[:10] in prompt


def test_empty_content_fits():
    limits = _limits(1000)
    prompt, decision = cb.fit_prompt(
        build_prompt=_build, content="", query="", limits=limits, counter=COUNTER
    )
    assert decision.applied is False
    assert decision.reason == "fits"
    assert prompt == "P:"


def test_malformed_tool_result_none_safe():
    """None/非字符串输入不得抛异常（malformed tool result）。"""
    limits = _limits(1000)
    prompt, decision = cb.fit_prompt(
        build_prompt=_build, content=None, query=None, limits=limits, counter=COUNTER,
    )
    assert prompt == "P:"
    assert decision.applied is False


# ===== split / score 基础 =====


def test_split_chunks_windows_long_blocks():
    content = "a" * (cb._MAX_CHUNK_CHARS + 100)
    chunks = cb.split_chunks(content)
    assert len(chunks) == 2
    assert "".join(chunks) == content


def test_relevance_score_prefers_matching_chunk():
    assert cb.relevance_score("LangGraph 是多智能体框架", "LangGraph") > cb.relevance_score(
        "今天天气很好", "LangGraph"
    )
    assert cb.relevance_score("anything", "") == 0.0


# ===== limit 解析（provider/model-aware）=====


def test_cloud_role_without_context_window_is_not_trimmed(monkeypatch):
    """云端 role 拿不到 limit → 不裁剪（不按本地 8192 误伤）。"""
    monkeypatch.delenv(cb.LOCAL_LIMIT_ENV, raising=False)
    monkeypatch.setattr(cb, "get_token_counter", lambda root=None: COUNTER)

    limits = cb.resolve_budget_limits("writer")  # hybrid config: writer → openai
    assert limits is None

    content = "x" * 100000
    prompt, decision = cb.fit_prompt_for_role(
        role="writer", build_prompt=_build, content=content, query="q"
    )
    assert decision.applied is False
    assert decision.reason == "limit_unknown"
    assert prompt == "P:" + content


def test_local_role_env_override_limit(monkeypatch):
    monkeypatch.setenv(cb.LOCAL_LIMIT_ENV, "5000")
    limits = cb.resolve_budget_limits("researcher_summarizer")
    assert limits is not None
    assert limits.context_limit == 5000
    assert limits.limit_source == "env_override"


def test_config_context_window_wins_over_env(monkeypatch):
    monkeypatch.setenv(cb.LOCAL_LIMIT_ENV, "5000")
    cfg = {
        "roles": {"researcher_summarizer": {"backend": "openai_local", "handle": "m1"}},
        "cognition": {"openai_local": {"base_url": "http://x", "models": {"m1": {"context_window": 9000}}}},
        "context_budget": {"reserve_output_tokens": 111, "safety_margin_tokens": 22},
    }
    monkeypatch.setattr("deep_research.utils.load_config", lambda stage_name=None, config_path=None: cfg)
    monkeypatch.setattr(cb, "get_token_counter", lambda root=None: COUNTER)

    limits = cb.resolve_budget_limits("researcher_summarizer")
    assert limits is not None
    assert limits.context_limit == 9000
    assert limits.limit_source == "config"
    assert limits.reserved_output == 111
    assert limits.safety_margin == 22


# ===== 业务接线（tool.py）+ 决策记录 =====


def _set_collector(tmp_path, monkeypatch):
    from deep_research.callbacks.baseline_metrics import (
        BaselineMetricsCollector, RunContext, set_baseline_collector,
    )

    collector = BaselineMetricsCollector(
        RunContext(run_id="budget-test", task_id="t1"), raw_root=tmp_path
    )
    set_baseline_collector(collector)
    return collector


def test_summarizer_applies_budget_and_records_decision(tmp_path, monkeypatch):
    from deep_research.callbacks.baseline_metrics import set_baseline_collector
    from deep_research.tools import tool as dr_tools

    monkeypatch.setenv(cb.LOCAL_LIMIT_ENV, "4000")  # ceiling = 4000-1024-256 = 2720
    monkeypatch.setattr(cb, "get_token_counter", lambda root=None: COUNTER)
    _set_collector(tmp_path, monkeypatch)

    try:
        huge = "有关 LangGraph 的调研材料。" + "内容" * 5000  # 远超预算
        out = dr_tools.summarize_webpage_content(huge, query="LangGraph")
    finally:
        set_baseline_collector(None)

    assert out  # summary 正常返回（Fake LLM 的 JSON）
    # 送给模型的 prompt 必须落在硬上限之内
    sent = dr_tools.summarization_model.calls[-1][0].content
    assert len(sent) <= 2720
    assert "[... 内容过长" in sent

    rows = [
        json.loads(line)
        for line in (tmp_path / "budget-test" / "budget_events.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["applied"] is True
    assert row["reason"].startswith("trimmed")
    assert row["context_limit"] == 4000
    assert row["reserved_output"] == 1024
    assert row["safety_margin"] == 256
    assert row["original_tokens"] > row["final_tokens"]
    assert row["final_tokens"] <= 2720
    assert row["counter"] == COUNTER.name
    assert row["query"] == "LangGraph"


def test_summarizer_short_content_untouched(tmp_path, monkeypatch):
    from deep_research.callbacks.baseline_metrics import set_baseline_collector
    from deep_research.tools import tool as dr_tools
    from deep_research.prompts import SUMMARIZE_PROMPT
    from deep_research.utils import get_today_str

    monkeypatch.setenv(cb.LOCAL_LIMIT_ENV, "4000")
    monkeypatch.setattr(cb, "get_token_counter", lambda root=None: COUNTER)
    _set_collector(tmp_path, monkeypatch)
    try:
        short = "短内容"
        dr_tools.summarize_webpage_content(short, query="q")
    finally:
        set_baseline_collector(None)

    sent = dr_tools.summarization_model.calls[-1][0].content
    assert sent == SUMMARIZE_PROMPT.format(webpage_content=short, date=get_today_str())


def test_process_search_results_budgets_every_page(tmp_path, monkeypatch):
    from deep_research.callbacks.baseline_metrics import set_baseline_collector
    from deep_research.tools import tool as dr_tools

    monkeypatch.setenv(cb.LOCAL_LIMIT_ENV, "4000")  # ceiling 2720
    monkeypatch.setattr(cb, "get_token_counter", lambda root=None: COUNTER)
    _set_collector(tmp_path, monkeypatch)
    try:
        results = {
            "http://a": {"title": "A", "content": "c", "raw_content": "A" * 9000},
            "http://b": {"title": "B", "content": "c", "raw_content": "B" * 9000},
            "http://c": {"title": "C", "content": "no raw"},
        }
        out = dr_tools.process_search_results(results, query="some query")
    finally:
        set_baseline_collector(None)

    assert set(out) == {"http://a", "http://b", "http://c"}
    rows = [
        json.loads(line)
        for line in (tmp_path / "budget-test" / "budget_events.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 2, "有 raw_content 的两页各产生一条预算决策"
    assert all(r["applied"] for r in rows)
