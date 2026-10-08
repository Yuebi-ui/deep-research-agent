"""Chroma collection 的 embedding schema 守卫（Phase 3C P0）。

职责：

1. 打开/创建 collection 时校验 embedding identity marker；
   - 旧 collection（无 marker）→ :class:`LegacyMemoryCollectionError`（需迁移）
   - marker 与当前配置不匹配 → :class:`EmbeddingSpaceMismatchError`
   **禁止 silent compatibility**：绝不拿着旧 space 的向量直接查询新 query。
2. :class:`ManagedCollection` 把"显式 embedding"变成结构性保证：
   upsert / update / search 全部由它调用 :class:`EmbeddingClient` 计算向量，
   业务代码没有机会绕过这一步让 Chroma 用默认 EF（ONNX MiniLM 384 维）。

迁移入口见 ``scripts/migrate_memory_schema.py``（显式执行，不自动触发）。
"""

from __future__ import annotations

from typing import Any

from chromadb.errors import NotFoundError

from deep_research.memory.embeddings import EmbeddingClient, EmbeddingIdentity
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)

DEFAULT_SPACE = "cosine"


class MemorySchemaError(RuntimeError):
    """memory collection 的 embedding schema 与当前配置不兼容。"""

    def __init__(self, collection_name: str, message: str) -> None:
        self.collection_name = collection_name
        super().__init__(f"[{collection_name}] {message}")


class LegacyMemoryCollectionError(MemorySchemaError):
    """collection 存在但没有任何 embedding identity marker。

    这类 collection 由 Phase 3C 之前的代码创建，embedding space 不可信
    （实测为 Chroma 默认 EF 的 384 维 ONNX 向量）。
    """


class EmbeddingSpaceMismatchError(MemorySchemaError):
    """collection 的 embedding identity 与当前配置不一致。"""


def _migration_hint(name: str) -> str:
    return (
        "请显式执行迁移：.venv/bin/python scripts/migrate_memory_schema.py"
        "（先 --dry-run 查看计划，再 --apply）；迁移会保留全部 "
        "ids/documents/metadatas，仅重算向量。"
    )


def _check_identity(collection_name: str, metadata: dict | None, expected: EmbeddingIdentity) -> None:
    stored = EmbeddingIdentity.from_markers(metadata)
    if stored is None:
        raise LegacyMemoryCollectionError(
            collection_name,
            "existing collection has no embedding identity markers "
            f"(legacy schema, created before Phase 3C); expected {expected.describe()}. "
            + _migration_hint(collection_name),
        )
    if stored != expected:
        raise EmbeddingSpaceMismatchError(
            collection_name,
            f"collection embedding space {stored.describe()} != current config "
            f"{expected.describe()}. " + _migration_hint(collection_name),
        )


class ManagedCollection:
    """对 Chroma collection 的薄封装：写入与检索强制显式 embedding。

    只读访问（get / count / query 等）通过 ``__getattr__`` 透传底层
    collection，避免重复实现 Chroma 全部 API。
    """

    def __init__(self, collection: Any, embedder: EmbeddingClient) -> None:
        self._collection = collection
        self._embedder = embedder

    # ---- 只读透传 ----

    def __getattr__(self, item: str) -> Any:
        return getattr(self._collection, item)

    @property
    def name(self) -> str:
        return self._collection.name

    @property
    def raw(self) -> Any:
        """底层 Chroma collection（仅限诊断/迁移使用）。"""
        return self._collection

    @property
    def identity(self) -> EmbeddingIdentity:
        return self._embedder.identity

    def count(self) -> int:
        return int(self._collection.count())

    # ---- 写入（显式 embedding） ----

    def upsert(
        self,
        *,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict] | None = None,
    ) -> None:
        embeddings = self._embedder.embed(documents)
        self._collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )

    def update(
        self,
        *,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict] | None = None,
    ) -> None:
        embeddings = self._embedder.embed(documents)
        self._collection.update(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )

    def update_metadata(self, *, ids: list[str], metadatas: list[dict]) -> None:
        """仅更新已有记录的标量 metadata；不写文档、无需重算 embedding。"""
        self._collection.update(ids=ids, metadatas=metadatas)

    # ---- 检索（显式 embedding） ----

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        total = self._collection.count()
        if total == 0:
            return []
        query_embedding = self._embedder.embed([query])
        n = min(top_k, total)
        results = self._collection.query(query_embeddings=query_embedding, n_results=n)
        if not results or not results.get("documents") or not results["documents"][0]:
            return []
        metadatas = results.get("metadatas") or [None] * len(results["ids"][0])
        return [
            {"id": doc_id, "content": doc, "metadata": meta or {}}
            for doc_id, doc, meta in zip(
                results["ids"][0], results["documents"][0], metadatas[0]
            )
        ]


def open_managed_collection(
    client: Any,
    name: str,
    embedder: EmbeddingClient,
    *,
    space: str = DEFAULT_SPACE,
) -> ManagedCollection:
    """打开已存在的 collection（校验 identity）或按当前 identity 创建。"""
    identity = embedder.identity
    try:
        collection = client.get_collection(name)
    except NotFoundError:
        collection = client.create_collection(
            name=name,
            embedding_function=None,
            metadata={"hnsw:space": space, **identity.as_markers()},
        )
        logger.info(
            "Created memory collection '%s' (%s, space=%s)", name, identity.describe(), space
        )
        return ManagedCollection(collection, embedder)

    _check_identity(name, collection.metadata, identity)
    return ManagedCollection(collection, embedder)


def inspect_collection(client: Any, name: str, expected: EmbeddingIdentity) -> dict:
    """非抛错的 schema 状态检查，供 health / benchmark preflight 使用。"""
    try:
        collection = client.get_collection(name)
    except NotFoundError:
        return {"collection": name, "status": "absent", "compatible": True, "count": 0}
    stored = EmbeddingIdentity.from_markers(collection.metadata)
    status = "ok"
    compatible = True
    if stored is None:
        status = "legacy_no_markers"
        compatible = False
    elif stored != expected:
        status = "mismatch"
        compatible = False
    return {
        "collection": name,
        "status": status,
        "compatible": compatible,
        "count": int(collection.count()),
        "stored": None if stored is None else {
            "provider": stored.provider,
            "model": stored.model,
            "dimension": stored.dimension,
            "schema_version": stored.schema_version,
        },
        "expected": {
            "provider": expected.provider,
            "model": expected.model,
            "dimension": expected.dimension,
            "schema_version": expected.schema_version,
        },
    }
