"""Writer 上下文去重 + 引用静态核对（无外部 API）。"""

from deep_research.writer_context import build_final_report_prompt, unique_notes
from deep_research.writer_validation import validate_report_citations
from deep_research.prompts.final_report import FINAL_REPORT_PROMPT


def test_writer_removes_exact_duplicate_notes_but_not_conflicting_facts():
    notes = ["2026年收入12%", " 2026年收入12%  ", "2026年收入13%"]
    assert unique_notes(notes) == ["2026年收入12%", "2026年收入13%"]
    prompt, info = build_final_report_prompt(
        research_brief="分析2026年收入", draft_report="初稿内容", notes=notes, date="2026-10-08"
    )
    assert info["notes_total"] == 3 and info["notes_kept"] == 2
    assert prompt.count("2026年收入12%") == 1
    assert "2026年收入13%" in prompt


def test_inline_chinese_citations_are_resolved():
    url = "https://example.org/annual"
    report = f"## 结论\n增长12%[1]。\n\n### 来源列表\n[1] 年报：{url}"
    outcome = validate_report_citations(report, f"来源：{url}")
    assert outcome["ok"] and outcome["body_citations"] == [1]


def test_missing_or_untrusted_sources_report_warnings():
    report = "## 结论\n增长12%[2]\n\n### 来源列表\n[1] 错误来源：https://fake.example/a"
    issues = validate_report_citations(report, "https://valid.example/b")["issues"]
    assert "unresolved_citations" in issues
    assert "url_not_found_in_research_material" in issues


def test_prompt_uses_one_consistent_citation_policy():
    assert "[标题](URL)" not in FINAL_REPORT_PROMPT
    assert "### 来源列表" in FINAL_REPORT_PROMPT


def test_writer_context_budget_has_safe_rollback(monkeypatch):
    monkeypatch.setenv("DR_WRITER_CONTEXT_BUDGET", "off")
    prompt, info = build_final_report_prompt(
        research_brief="问题", draft_report="草稿内容", notes=["来源一", "来源一"], date="2026-10-08"
    )
    assert info["budget"] == "disabled"
    assert "草稿内容" in prompt and prompt.count("来源一") == 1
