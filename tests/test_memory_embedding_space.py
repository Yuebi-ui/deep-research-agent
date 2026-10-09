"""Phase 3C P0：Structured Memory embedding space correctness。

覆盖历史 bug：document 由 Chroma 默认 EF（ONNX MiniLM 384 维）写入、
query 由 DashScope（1024 维）检索 → 维度不匹配 / 空间不一致。

测试分层：

- 离线（默认，本文件主体）：确定性 fake / 语义 fake embedding 验证 plumbing、
  身份一致性、持久化、历史不兼容拒绝、E3 批量语义回归；
- live（@pytest.mark.live，需真实 DashScope）：固定 corpus 的语义检索
  top-k 正确性，见 test_live_semantic_retrieval_with_dashscope。
"""

from __future__ import annotations

import hashlib
import math
import re

import pytest

from deep_research.memory.embeddings import (
    MAX_BATCH_SIZE,
    EmbeddingClient,
    EmbeddingIdentity,
    fake_embedding,
)
from deep_research.memory.schema_guard import (
    EmbeddingSpaceMismatchError,
    LegacyMemoryCollectionError,
    inspect_collection,
)
from deep_research.memory.schemas import Entity, MemoryClaim
from deep_research.memory.structured_store import StructuredMemoryStore

# 固定 corpus（Section 5 Test 2/3 指定）
D1 = "Redis heartbeat lease ownership"
D2 = "GPU inference with vLLM"
D3 = "French cooking and pastry"
HEARTBEAT_QUERY = "How does worker claim heartbeat work?"


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


class SemanticFakeEmbedder:
    """确定性词袋 embedding：共享词越多 cosine 越接近。

    用于在 non-live 测试中真正验证「语义相关优先于无关」，
    而不是只验证「query 不报错」（fake_embedding 哈希向量做不到这一点）。
    """

    DIM = 64

    def __init__(self) -> None:
        self._identity = EmbeddingIdentity(
            provider="fake", model="test-semantic-bow", dimension=self.DIM
        )

    @property
    def identity(self) -> EmbeddingIdentity:
        return self._identity

    @property
    def is_fake(self) -> bool:
        return True

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vec = [0.0] * self.DIM
            for token in _tokenize(text):
                idx = int(hashlib.sha256(token.encode()).hexdigest(), 16) % self.DIM
                vec[idx] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            vectors.append([x / norm for x in vec])
        return vectors


def _make_store(tmp_path, embedder, name="store") -> StructuredMemoryStore:
    return StructuredMemoryStore(persist_dir=str(tmp_path / name), embedder=embedder)


def _stored_vectors(collection) -> dict[str, list[float]]:
    got = collection.get(include=["embeddings"])
    return {
        i: list(e) for i, e in zip(got["ids"], got["embeddings"]) if e is not None
    }


# ===== Test 1 — Embedding Identity =====


def test_write_and_query_share_embedding_identity(tmp_path):
    """写入与检索必须使用同一 provider/model/dimension/schema。

    证据链：
    1. collection marker == embedder identity（provider/model/dim/schema 全等）
    2. 存储向量逐元素等于 embedder 输出（证明写入是显式 embedding，
       而非 Chroma 默认 EF 的 384 维 ONNX 向量）
    3. 检索路径复用同一 embedder（query 不抛维度错误且能命中）
    """
    embedder = EmbeddingClient(force_fake=True)
    store = _make_store(tmp_path, embedder)
    doc = f"Redis: {D1}"
    store.upsert_entities([Entity(id="e1", name="Redis", type="technology", description=D1)])

    markers = EmbeddingIdentity.from_markers(store._entities.raw.metadata)
    assert markers == embedder.identity, "collection marker 必须等于写入 identity"

    stored = _stored_vectors(store._entities)
    expected = embedder.embed([doc])[0]
    assert len(stored["e1"]) == embedder.identity.dimension
    assert stored["e1"] == pytest.approx(expected, abs=1e-5), (
        "存储向量必须来自统一 EmbeddingClient，而不是 Chroma 默认 EF"
    )

    hits = store.search_entities(D1, top_k=1)
    assert [h.id for h in hits] == ["e1"]


def test_all_four_collections_carry_identity_markers(tmp_path):
    store = _make_store(tmp_path, EmbeddingClient(force_fake=True))
    for collection in (store._entities, store._claims, store._evidences, store._contradictions):
        markers = EmbeddingIdentity.from_markers(collection.raw.metadata)
        assert markers is not None and markers == store._embedder.identity


# ===== Test 2 — Dimension =====


def test_fixed_corpus_no_dimension_mismatch(tmp_path):
    """D1/D2/D3 固定 corpus + heartbeat query：不得发生 dimension mismatch。

    修复前的行为：documents 被默认 EF 写成 384 维，
    query 1024 维 → chromadb InvalidArgumentError。
    """
    store = _make_store(tmp_path, EmbeddingClient(force_fake=True))
    store.upsert_entities([
        Entity(id="d1", name="Redis", type="technology", description=D1),
        Entity(id="d2", name="vLLM", type="technology", description=D2),
        Entity(id="d3", name="Pastry", type="concept", description=D3),
    ])
    hits = store.search_entities(HEARTBEAT_QUERY, top_k=3)
    assert len(hits) == 3, "query 必须成功返回（修复前是 InvalidArgumentError）"
    assert all(len(v) == 1024 for v in _stored_vectors(store._entities).values())


# ===== Test 3 — Semantic Retrieval（离线确定性版本）=====


def test_semantic_retrieval_prefers_related_document(tmp_path):
    """query 应优先返回语义相关 document（共享词袋 → 高 cosine）。"""
    store = _make_store(tmp_path, SemanticFakeEmbedder())
    store.upsert_entities([
        Entity(id="d1", name="Redis", type="technology", description=D1),
        Entity(id="d2", name="vLLM", type="technology", description=D2),
        Entity(id="d3", name="Pastry", type="concept", description=D3),
    ])
    hits = store.search_entities(HEARTBEAT_QUERY, top_k=3)
    assert hits[0].id == "d1", f"top-1 应为 heartbeat 相关文档，实际 {[h.id for h in hits]}"
    assert {h.id for h in hits} == {"d1", "d2", "d3"}

    claims = store.search_claims("nothing indexed here", top_k=3)
    assert claims == [], "空 collection 检索应为空而非报错"


@pytest.mark.live
def test_live_semantic_retrieval_with_dashscope(tmp_path):
    """真实 DashScope text-embedding-v4 的端到端语义检索验证。

    显式 opt-in（避免正常测试套件产生付费调用）：
        DR_LIVE_EMBEDDING_TEST=1 ALLOW_LIVE_EXTERNAL_APIS=true \
            .venv/bin/python -m pytest tests/test_memory_embedding_space.py \
            -m live -q
    """
    import os

    if os.environ.get("DR_LIVE_EMBEDDING_TEST") != "1":
        pytest.skip("live embedding 测试需显式开启：DR_LIVE_EMBEDDING_TEST=1")
    store = _make_store(tmp_path, EmbeddingClient(force_fake=False))  # live
    store.upsert_entities([
        Entity(id="d1", name="Redis", type="technology", description=D1),
        Entity(id="d2", name="vLLM", type="technology", description=D2),
        Entity(id="d3", name="Pastry", type="concept", description=D3),
    ])
    hits = store.search_entities(HEARTBEAT_QUERY, top_k=3)
    assert hits, "live 检索必须返回结果"
    assert hits[0].id == "d1", f"live top-1 应为 heartbeat 相关文档，实际 {[h.id for h in hits]}"


# ===== Test 4 — Persistence / Reopen =====


def test_persistence_reopen_retrieval(tmp_path):
    """写入 → 进程级重开（新 store 实例）→ 检索仍正确。"""
    embedder = SemanticFakeEmbedder()
    persist_dir = str(tmp_path / "persist")
    store = StructuredMemoryStore(persist_dir=persist_dir, embedder=embedder)
    store.upsert_entities([
        Entity(id="d1", name="Redis", type="technology", description=D1),
        Entity(id="d2", name="vLLM", type="technology", description=D2),
        Entity(id="d3", name="Pastry", type="concept", description=D3),
    ])
    store.upsert_claims([MemoryClaim(id="c1", text="Redis lease 通过 heartbeat 续约")])
    del store

    reopened = StructuredMemoryStore(persist_dir=persist_dir, embedder=embedder)
    assert reopened.stats()["entities"] == 3
    hits = reopened.search_entities(HEARTBEAT_QUERY, top_k=3)
    assert hits[0].id == "d1"
    claims = reopened.search_claims("heartbeat 续约", top_k=1)
    assert len(claims) == 1
    assert claims[0].id == "c1" and claims[0].text == "Redis lease 通过 heartbeat 续约"


# ===== Test 5 — Historical Mismatch =====


def _insert_legacy_collection(persist_dir: str, name: str, dim: int = 384) -> None:
    """模拟 Phase 3C 之前的 collection：无 marker，向量为旧 space 的 dim 维。"""
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    client = chromadb.PersistentClient(
        path=persist_dir, settings=ChromaSettings(anonymized_telemetry=False)
    )
    legacy = client.create_collection(name=name)  # 无 marker（=旧 schema）
    legacy.add(
        ids=["old1"],
        embeddings=[[0.1] * dim],
        documents=[D1],
        metadatas=[{"name": "Redis", "type": "technology", "description": D1}],
    )


def test_legacy_collection_is_rejected_not_silently_queried(tmp_path):
    persist_dir = str(tmp_path / "legacy")
    _insert_legacy_collection(persist_dir, "memory_entities", dim=384)

    with pytest.raises(LegacyMemoryCollectionError) as excinfo:
        StructuredMemoryStore(persist_dir=persist_dir, embedder=EmbeddingClient(force_fake=True))
    assert "migrate_memory_schema" in str(excinfo.value), "错误信息必须指向显式迁移入口"

    # 非抛错路径（health/preflight）必须报告 incompatible
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    client = chromadb.PersistentClient(
        path=persist_dir, settings=ChromaSettings(anonymized_telemetry=False)
    )
    status = inspect_collection(client, "memory_entities", EmbeddingClient(force_fake=True).identity)
    assert status["compatible"] is False and status["status"] == "legacy_no_markers"


def test_mismatched_marker_collection_is_rejected(tmp_path):
    """schema/model/dimension 任一不同都必须显式拒绝。"""
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    persist_dir = str(tmp_path / "mismatch")
    client = chromadb.PersistentClient(
        path=persist_dir, settings=ChromaSettings(anonymized_telemetry=False)
    )
    stale = EmbeddingIdentity(provider="dashscope", model="text-embedding-v3", dimension=768)
    client.create_collection(name="memory_claims", metadata=stale.as_markers())

    with pytest.raises(EmbeddingSpaceMismatchError) as excinfo:
        StructuredMemoryStore(persist_dir=persist_dir, embedder=EmbeddingClient(force_fake=True))
    msg = str(excinfo.value)
    assert "text-embedding-v3" in msg and "dim=768" in msg


def test_foreign_embedder_cannot_query_another_space(tmp_path):
    """即使 collection 合法，另一个 identity 的 embedder 也不得直接查询。"""
    persist_dir = str(tmp_path / "two-spaces")
    a = StructuredMemoryStore(persist_dir=persist_dir, embedder=SemanticFakeEmbedder())
    a.upsert_entities([Entity(id="e1", name="Redis", type="technology", description=D1)])

    class OtherEmbedder(SemanticFakeEmbedder):
        def __init__(self) -> None:
            super().__init__()
            self._identity = EmbeddingIdentity(provider="fake", model="other", dimension=32)

    with pytest.raises(EmbeddingSpaceMismatchError):
        StructuredMemoryStore(persist_dir=persist_dir, embedder=OtherEmbedder())


# ===== Test 6 — E3 Batch Upsert Regression =====


def test_batch_upsert_keeps_identity_and_content(tmp_path):
    """E3 批量写入回归：ids/documents/metadatas/计数一致，且向量统一 space。"""
    embedder = SemanticFakeEmbedder()
    store = _make_store(tmp_path, embedder)
    entities = [
        Entity(id=f"e{i}", name=f"实体{i}", type="concept", description=f"描述 {i}")
        for i in range(15)
    ]
    claims = [MemoryClaim(id=f"c{i}", text=f"断言 {i}") for i in range(10)]

    entity_ids = store.upsert_entities(entities)
    claim_ids = store.upsert_claims(claims)

    assert entity_ids == [f"e{i}" for i in range(15)]
    assert claim_ids == [f"c{i}" for i in range(10)]
    assert store.stats()["entities"] == 15 and store.stats()["claims"] == 10

    got = store._entities.get(include=["documents", "metadatas", "embeddings"])
    assert got["documents"] == [f"实体{i}: 描述 {i}" for i in range(15)]
    assert [m["name"] for m in got["metadatas"]] == [f"实体{i}" for i in range(15)]
    expected = embedder.embed(got["documents"])
    for stored_vec, want in zip(got["embeddings"], expected):
        assert list(stored_vec) == pytest.approx(want, abs=1e-5)


# ===== Embedding client：DashScope 批量上限（迁移时发现的 provider 约束）=====


def test_embedding_client_chunks_live_batches(monkeypatch):
    """text-embedding-v4 单请求 input 上限 10；client 必须自动分批且保序。"""
    client = EmbeddingClient(force_fake=True)
    client._fake = False  # 走 live 分支，但用 fake SDK 替身

    sent_batches: list[list[str]] = []

    class _FakeEmbeddings:
        def create(self, model, input):  # noqa: A002  # 对齐 OpenAI SDK 签名
            sent_batches.append(list(input))
            payload = [
                type("D", (), {"embedding": fake_embedding(t, dims=8)})()
                for t in input
            ]
            return type("R", (), {"data": payload})()

    class _FakeOpenAI:
        embeddings = _FakeEmbeddings()

    monkeypatch.setattr(client, "_openai", _FakeOpenAI())

    texts = [f"text-{i}" for i in range(23)]
    vectors = client.embed(texts)

    assert [len(b) for b in sent_batches] == [10, 10, 3]
    assert all(len(b) <= MAX_BATCH_SIZE for b in sent_batches)
    assert len(vectors) == 23
    assert vectors[0] == pytest.approx(fake_embedding("text-0", dims=8))
    assert vectors[22] == pytest.approx(fake_embedding("text-22", dims=8))
