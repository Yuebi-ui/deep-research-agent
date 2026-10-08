"""阶段 1 + 阶段 3 的数据完整性回归测试（真实 fake-embedding Chroma）。"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage

from deep_research.memory.manager import MemoryManager, _similarity
from deep_research.memory.schemas import Entity, Evidence, MemoryClaim, Contradiction
from deep_research.memory.structured_store import StructuredMemoryStore
from deep_research.testing.fake_llm import set_responder


URL = "https://example.org/research/report-2026"
BODY = ("北京创新实验室报告称2026年增长12%，详见 " + URL + "。\n") * 12


def _payload():
    text = "北京创新实验室在2026年同比增长12%"
    return {
        "entities": [{"name": "北京创新实验室", "type": "organization", "importance": 8}],
        "claims": [{
            "text": text, "entities": ["北京创新实验室"], "source_url": URL,
            "confidence": 0.8,
            "evidence": [{"url": URL, "type": "official_doc", "description": "报告披露同比增长12%"}],
        }],
        "contradictions": [],
    }


def _manager(tmp_path):
    set_responder("evaluator", lambda _: AIMessage(content=json.dumps(_payload(), ensure_ascii=False)))
    return MemoryManager(persist_dir=str(tmp_path / "chroma"))


def test_chinese_similarity_is_not_whitespace_token_jaccard():
    assert _similarity("2026年研发投入增长12%", "2026年研发投入增长13%") > 0.5
    assert _similarity("", "") == 0


def test_chroma_metadata_lists_round_trip_and_readers_work(tmp_path):
    s = StructuredMemoryStore(persist_dir=str(tmp_path / "structured"))
    s.upsert_entities([Entity(id="e1", name="北京实验室", type="organization", aliases=["实验室"] )])
    s.upsert_claims([MemoryClaim(id="c1", text="增长12%", entities=["e1"])])
    s.upsert_evidence(Evidence(id="ev1", claim_id="c1", type="official_doc", description="年报"))
    s.upsert_contradiction(Contradiction(id="ct1", claim_a_id="c1", description="版本冲突"))
    assert s.search_entities("北京实验室", 1)[0].aliases == ["实验室"]
    assert s.search_claims("增长", 1)[0].entities == ["e1"]
    assert [c.id for c in s.get_claims_by_entity("实验室")] == ["c1"]
    assert [e.id for e in s.get_evidence_by_claim("c1")] == ["ev1"]
    assert s.get_contradictions()[0].id == "ct1"
    assert s.get_contradictions_for_claims({"c1"})[0].id == "ct1"


def test_same_report_is_idempotent_and_new_revision_is_retained(tmp_path):
    mgr = _manager(tmp_path)
    first = mgr.store_from_report("北京创新实验室增长", BODY)
    assert mgr._store.get_memory(first)["metadata"]["structured_status"] == "complete"
    assert mgr._structured.stats()["claims"] == 1
    assert mgr.store_from_report("另外一个问题", BODY.replace("。\n", "。\n\n")) == first
    assert mgr._structured.stats()["claims"] == 1
    second = mgr.store_from_report("北京创新实验室增长", BODY.replace("12%", "13%"))
    assert second != first
    assert mgr.count() == 2
    assert mgr._structured.stats()["claims"] == 2


def test_claim_evidence_report_links_and_unverified_status(tmp_path):
    mgr = _manager(tmp_path)
    report_id = mgr.store_from_report("北京创新实验室", BODY)
    claim = mgr.query_claims("增长", top_k=1)[0]
    assert claim.report_id == report_id and claim.verification_status == "unverified"
    assert claim.entities
    assert mgr._structured.get_claims_by_entity("北京创新实验室")[0].id == claim.id
    evidence = mgr._structured.get_evidence_by_claim(claim.id)
    assert len(evidence) == 1 and evidence[0].report_id == report_id
    assert URL in mgr.retrieve_context("北京创新实验室", top_k=1)


def test_invalid_source_url_is_not_laundered_into_evidence(tmp_path):
    bad_url = "https://madeup.example/invalid"
    response = _payload()
    response["claims"][0]["source_url"] = bad_url
    response["claims"][0]["evidence"][0]["url"] = bad_url
    set_responder("evaluator", lambda _: AIMessage(content=json.dumps(response)))
    mgr = MemoryManager(persist_dir=str(tmp_path / "bad"))
    mgr.store_from_report("北京实验室", BODY)
    claim = mgr.query_claims("增长", 1)[0]
    assert claim.source_url is None
    assert not mgr._structured.get_evidence_by_claim(claim.id)


def test_partial_failure_remains_pending_then_retry_is_idempotent(tmp_path, monkeypatch):
    mgr = _manager(tmp_path)
    original = mgr._structured.upsert_claims
    once = {"fail": True}

    def flaky(claims):
        if once["fail"]:
            once["fail"] = False
            raise RuntimeError("temporary disk failure")
        return original(claims)

    monkeypatch.setattr(mgr._structured, "upsert_claims", flaky)
    report_id = mgr.store_from_report("北京实验室", BODY)
    assert mgr._store.get_memory(report_id)["metadata"]["structured_status"] == "pending"
    assert mgr.store_from_report("北京实验室", BODY) == report_id
    assert mgr._store.get_memory(report_id)["metadata"]["structured_status"] == "complete"
    assert mgr._structured.stats()["entities"] == 1
    assert mgr._structured.stats()["claims"] == 1


def test_retrieval_fallback_when_structured_store_raises(tmp_path, monkeypatch):
    mgr = _manager(tmp_path)
    mgr.store_from_report("北京实验室", BODY)

    def unavailable(*_a, **_kw):
        raise RuntimeError("temporary down")

    monkeypatch.setattr(mgr._structured, "search_claims", unavailable)
    assert "历史报告" in mgr.retrieve_context("北京", top_k=2)


def test_structured_retrieval_can_be_rolled_back(tmp_path, monkeypatch):
    mgr = _manager(tmp_path)
    mgr.store_from_report("北京实验室", BODY)
    monkeypatch.setenv("DR_MEMORY_STRUCTURED_RETRIEVAL", "off")
    context = mgr.retrieve_context("北京实验室")
    assert "历史报告" in context
    assert "历史断言" not in context
