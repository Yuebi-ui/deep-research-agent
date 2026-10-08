"""Memory 3.0 integration tests using the project's actual Chroma + fake embedding.

Requires requirements.txt and requirements-dev.txt. Tests never call paid models.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from deep_research.context_budget import BudgetLimits, fit_prompt, _TRIM_MARKER
from deep_research.memory.manager import MemoryManager
from deep_research.memory.sections import split_report_sections
from deep_research.memory.retrieval import possible_temporal_change
from deep_research.testing.fake_llm import set_responder


@pytest.fixture(autouse=True)
def reset_extractor():
    yield
    set_responder("evaluator", None)


def test_full_report_sections_with_chroma_keyword_filter(tmp_path):
    target = "星河系统实验室在2026-09-01提升了吞吐量13%"
    url = "https://research.example.com/2026/perf"
    long_report = "# 背景\n\n" + "早期调研资料。" * 1100 + "\n## 实测\n" + target + " " + url
    calls = []

    def responder(messages):
        prompt = messages[0].content
        calls.append(prompt)
        if target not in prompt:
            data = {"entities": [], "claims": [], "contradictions": []}
        else:
            data = {
                "entities": [{"name": "星河系统实验室", "type": "organization"}],
                "claims": [{
                    "text": target, "entities": ["星河系统实验室"], "source_url": url,
                    "valid_from": "2026-09-01",
                    "evidence": [{"type": "official_doc", "url": url, "description": "测试页面"}],
                }],
                "contradictions": [],
            }
        return AIMessage(content=json.dumps(data, ensure_ascii=False))

    set_responder("evaluator", responder)
    manager = MemoryManager(persist_dir=str(tmp_path / "chroma"))
    rid = manager.store_from_report("星河系统实验室吞吐量", long_report)
    assert rid
    assert len(calls) > 2  # not a first-6000-character extraction
    assert manager.get_full_report(rid) == long_report
    assert manager.stats()["semantic"] == 1
    assert manager.stats()["sections"] == len(split_report_sections(long_report))
    hits = manager._sections.search_keyword(target, top_k=5)
    assert any(target in hit["content"] for hit in hits)
    claims = manager._structured.get_claims_by_entity("星河系统实验室")
    assert len(claims) == 1 and claims[0].valid_from == "2026-09-01"
    assert manager._structured.get_evidence_by_claim(claims[0].id)[0].url == url
    context = manager.retrieve_context("星河系统实验室吞吐量", top_k=3)
    assert target in context

    old_count = len(calls)
    manager.store_from_report("重复查询", long_report)
    assert len(calls) == old_count and manager.count() == 1


def test_temporal_relation_does_not_auto_refute_old_claim(tmp_path):
    def responder(messages):
        prompt = messages[0].content
        old = "增长12%" in prompt
        year, number = ("2025", "12") if old else ("2026", "13")
        return AIMessage(content=json.dumps({
            "entities": [{"name": "北京实验室", "type": "organization"}],
            "claims": [{"text": f"北京实验室在{year}年增长{number}%", "entities": ["北京实验室"]}],
            "contradictions": [],
        }, ensure_ascii=False))
    set_responder("evaluator", responder)
    manager = MemoryManager(persist_dir=str(tmp_path / "db"))
    manager.store_from_report("北京实验室增长", "甲" * 140 + "北京实验室在2025年增长12%")
    manager.store_from_report("北京实验室增长", "乙" * 140 + "北京实验室在2026年增长13%")
    claims = manager._structured.get_claims_by_entity("北京实验室")
    assert len(claims) == 2
    changes = [x for x in manager.get_contradictions() if x.relation_type == "possible_change"]
    assert len(changes) == 1 and changes[0].review_status == "unverified"
    assert all(c.verification_status == "unverified" and c.valid_to is None for c in claims)
    assert possible_temporal_change(claims[0].text, claims[1].text)


def test_context_budget_keeps_late_high_relevance_on_second_pass():
    class Counter:
        name = "character-counter"
        def count(self, text: str) -> int: return len(text)
    chunks = [f"unrelated{i:02d}" for i in range(59)] + ["KEYPASSAGE0"]
    content = "\n\n".join(chunks)
    budget = len(_TRIM_MARKER.format(kept=0, total=len(chunks))) + sum(map(len, chunks))
    limits = BudgetLimits(budget + 2, "test", 0, 0, "character-counter")
    text, decision = fit_prompt(
        build_prompt=lambda s: "P:" + s, content=content, query="KEYPASSAGE",
        limits=limits, counter=Counter(),
    )
    assert decision.applied and decision.chunks_kept < len(chunks)
    assert "KEYPASSAGE" in text
    assert decision.final_tokens <= budget + 2
