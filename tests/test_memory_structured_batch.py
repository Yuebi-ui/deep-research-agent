"""E3：structured memory 批量 upsert 的语义等价性测试（Phase 3B）。

验收要求（逐条对应）：

1. sequential vs batch：count / IDs / documents / metadatas 完全一致
2. 固定 query 向量下 retrieval（索引近邻）等价
   —— 注意：structured collections 的 document/query embedding space
   mismatch 是单独记录的 correctness bug（query 走 _embedder 1024 维、
   document 走 chroma 默认 EF 384 维），本实验**不修**，因此等价性用
   「同一显式查询向量」验证索引内容本身。
3. batch failure 不得影响任务完成（manager 层降级为 warning + 计数 0）
"""

from __future__ import annotations

import json

import pytest

from deep_research.memory.manager import MemoryManager
from deep_research.memory.schemas import Contradiction, Entity, MemoryClaim
from deep_research.memory.structured_store import StructuredMemoryStore
from deep_research.testing.fake_llm import set_responder

_ENTITIES = [
    Entity(id=f"e{i}", name=f"实体{i}", type="technology",
           description=f"描述 {i}", importance=float(i % 10 + 1))
    for i in range(15)
]
_CLAIMS = [
    MemoryClaim(id=f"c{i}", text=f"断言内容 {i}", confidence=0.5 + i * 0.01)
    for i in range(10)
]
_CONTRADICTIONS = [
    Contradiction(id=f"ct{i}", description=f"矛盾描述 {i}") for i in range(3)
]


def _get_all(collection) -> dict:
    got = collection.get(include=["documents", "metadatas", "embeddings"])
    return {
        i: {"doc": d, "meta": m, "emb": list(e) if e is not None else None}
        for i, d, m, e in zip(got["ids"], got["documents"], got["metadatas"], got["embeddings"])
    }


def _assert_same_store(a: StructuredMemoryStore, b: StructuredMemoryStore) -> None:
    for coll_a, coll_b in (
        (a._entities, b._entities),
        (a._claims, b._claims),
        (a._contradictions, b._contradictions),
    ):
        assert coll_a.count() == coll_b.count()
        da, db = _get_all(coll_a), _get_all(coll_b)
        assert set(da) == set(db), "IDs 必须一致"
        for i in da:
            assert da[i]["doc"] == db[i]["doc"], f"document 不一致: {i}"
            assert da[i]["meta"] == db[i]["meta"], f"metadata 不一致: {i}"
            va, vb = da[i]["emb"], db[i]["emb"]
            assert va is not None and vb is not None and len(va) == len(vb)
            assert all(abs(x - y) < 1e-6 for x, y in zip(va, vb)), f"向量不一致: {i}"


def test_batch_equals_sequential_store(tmp_path):
    seq = StructuredMemoryStore(persist_dir=str(tmp_path / "seq"))
    bat = StructuredMemoryStore(persist_dir=str(tmp_path / "bat"))

    for e in _ENTITIES:
        seq.upsert_entity(e)
    for c in _CLAIMS:
        seq.upsert_claim(c)
    for ct in _CONTRADICTIONS:
        seq.upsert_contradiction(ct)

    bat.upsert_entities(_ENTITIES)
    bat.upsert_claims(_CLAIMS)
    bat.upsert_contradictions(_CONTRADICTIONS)

    _assert_same_store(seq, bat)


def test_batch_returns_ids_in_order_and_assigns_missing(tmp_path):
    store = StructuredMemoryStore(persist_dir=str(tmp_path))
    batch = [Entity(name=f"n{i}", type="concept", description="d") for i in range(5)]  # 无 id
    ids = store.upsert_entities(batch)
    assert len(ids) == 5 and len(set(ids)) == 5
    assert all(e.id == i for e, i in zip(batch, ids))


def test_empty_batch_is_noop(tmp_path):
    store = StructuredMemoryStore(persist_dir=str(tmp_path))
    assert store.upsert_entities([]) == []
    assert store.upsert_claims([]) == []
    assert store.upsert_contradictions([]) == []
    assert store._entities.count() == 0


def test_index_equivalence_with_fixed_query_vector(tmp_path):
    """用同一显式查询向量检索两个 store，近邻顺序必须一致。"""
    seq = StructuredMemoryStore(persist_dir=str(tmp_path / "seq"))
    bat = StructuredMemoryStore(persist_dir=str(tmp_path / "bat"))
    for e in _ENTITIES:
        seq.upsert_entity(e)
    bat.upsert_entities(_ENTITIES)

    query_vec = _get_all(seq._entities)["e0"]["emb"]  # 显式向量，绕开 mismatch
    res_a = seq._entities.query(query_embeddings=[query_vec], n_results=3)
    res_b = bat._entities.query(query_embeddings=[query_vec], n_results=3)
    assert res_a["ids"][0] == res_b["ids"][0]


def test_batch_failure_degrades_gracefully(tmp_path, monkeypatch):
    """批量写入失败：不得抛异常、不得影响任务完成语义（manager 层降级）。"""
    set_responder(
        "evaluator",
        lambda messages: __import__("langchain_core.messages", fromlist=["AIMessage"]).AIMessage(
            content=json.dumps({
                "comprehensiveness_score": 7, "accuracy_score": 7, "coherence_score": 7,
                "reason": "t", "missing_aspects": [], "need_more_research": False,
                "entities": [{"name": "A", "type": "concept", "description": "d", "importance": 5}],
                "claims": [{"text": "claim", "confidence": 0.8}],
                "contradictions": [],
            })
        ),
    )
    mgr = MemoryManager(persist_dir=str(tmp_path))

    def boom(*args, **kwargs):
        raise RuntimeError("chroma unavailable")

    monkeypatch.setattr(mgr._structured._entities, "upsert", boom)
    monkeypatch.setattr(mgr._structured._claims, "upsert", boom)

    count = mgr.extract_and_store_structured("报告内容" * 50, "rid")
    assert count == {"entities": 0, "claims": 0, "contradictions": 0}


def test_manager_batch_writes_expected_counts(tmp_path):
    set_responder(
        "evaluator",
        lambda messages: __import__("langchain_core.messages", fromlist=["AIMessage"]).AIMessage(
            content=json.dumps({
                "comprehensiveness_score": 7, "accuracy_score": 7, "coherence_score": 7,
                "reason": "t", "missing_aspects": [], "need_more_research": False,
                "entities": [{"name": f"E{i}", "type": "concept", "description": "d", "importance": 5}
                             for i in range(20)],  # 上限 15
                "claims": [{"text": f"C{i}", "confidence": 0.8} for i in range(12)],  # 上限 10
                "contradictions": [{"description": "x"}],  # 上限 5
            })
        ),
    )
    mgr = MemoryManager(persist_dir=str(tmp_path))
    count = mgr.extract_and_store_structured("报告内容" * 50, "rid")

    assert count == {"entities": 15, "claims": 10, "contradictions": 1}
    stats = mgr._structured.stats()
    assert stats["entities"] == 15 and stats["claims"] == 10 and stats["contradictions"] == 1
