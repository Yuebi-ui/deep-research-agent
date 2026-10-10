"""基于 ChromaDB + 统一 Embedding 的向量记忆存储。

纯本地运行，ChromaDB 嵌入式模式，Embedding 通过 :class:`EmbeddingClient`
（DashScope text-embedding-v4 / 离线确定性伪向量，见 ``embeddings.py``）。

Phase 3C P0：写入与检索共用同一个 embedding identity，collection 打开时
校验 marker（旧 schema 显式拒绝，迁移入口见 ``scripts/migrate_memory_schema.py``）。
"""

from __future__ import annotations

import os

import chromadb
from chromadb.config import Settings as ChromaSettings

from deep_research.memory.embeddings import (  # noqa: F401  # re-export 兼容旧导入
    EmbeddingClient,
    fake_embedding,
    _EMBEDDING_DIMS,
)
from deep_research.memory.schema_guard import ManagedCollection, open_managed_collection
from deep_research import logging as dr_logging
from deep_research.settings import get_engine_settings

logger = dr_logging.get_logger(__name__)

DEFAULT_COLLECTION = "research_memory"
SECTION_COLLECTION = "memory_sections"


class VectorMemoryStore:
    """管理 ChromaDB 向量存储，负责记忆的增删查。"""

    def __init__(
        self,
        persist_dir: str | None = None,
        collection_name: str = DEFAULT_COLLECTION,
        embedder: EmbeddingClient | None = None,
    ) -> None:
        persist_dir = persist_dir or str(get_engine_settings().resolved_memory_data_dir / "chroma")
        os.makedirs(persist_dir, exist_ok=True)

        self._client = chromadb.PersistentClient(
            path=persist_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self._embedder = embedder or EmbeddingClient()
        self._collection: ManagedCollection = open_managed_collection(
            self._client, collection_name, self._embedder, space="cosine"
        )
        logger.info(
            "VectorMemoryStore initialized at %s (collection=%s, %s)",
            persist_dir,
            collection_name,
            self._embedder.identity.describe(),
        )

    def add_memory(self, doc_id: str, content: str, metadata: dict | None = None) -> None:
        """添加一条记忆到向量库（显式 embedding）。"""
        self._collection.upsert(
            ids=[doc_id],
            documents=[content],
            metadatas=[metadata] if metadata else None,
        )
        logger.info("Added memory: %s", doc_id)

    def add_memories(self, entries: list[dict], batch_size: int = 10) -> None:
        """Batch new section embeddings; reduces provider round trips vs one-by-one."""
        if not entries:
            return
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        for start in range(0, len(entries), batch_size):
            batch = entries[start:start + batch_size]
            self._collection.upsert(
                ids=[item["id"] for item in batch],
                documents=[item["content"] for item in batch],
                metadatas=[item["metadata"] for item in batch],
            )

    def search_memory(self, query: str, top_k: int = 3) -> list[dict]:
        """根据 query 检索最相关的历史记忆（显式 embedding）。"""
        return self._collection.search(query, top_k=top_k)

    def search_keyword(self, query: str, top_k: int = 10) -> list[dict]:
        """Substring probes backed by Chroma's document filter, with bounded results.

        This is deliberately a candidate retriever, not a replacement for dense
        search. Older Chroma builds may not support the filter; the caller can
        continue with dense results on failure.
        """
        from deep_research.memory.retrieval import lexical_score, search_terms

        if top_k <= 0 or not query.strip() or self.count() == 0:
            return []
        candidates: dict[str, dict] = {}
        for term in search_terms(query):
            got = self._collection.get(
                where_document={"$contains": term},
                limit=min(max(top_k * 3, 10), 60),
                include=["documents", "metadatas"],
            )
            for rid, doc, meta in zip(
                got.get("ids") or [], got.get("documents") or [],
                got.get("metadatas") or [],
            ):
                if isinstance(doc, str):
                    candidates[rid] = {"id": rid, "content": doc, "metadata": meta or {}}
        return sorted(
            candidates.values(),
            key=lambda x: (-lexical_score(query, x["content"]), x["id"]),
        )[:top_k]

    def get_report_sections(self, report_id: str) -> list[dict]:
        """Fetch all child windows for one report, with deterministic pagination."""
        results: list[dict] = []
        offset = 0
        while True:
            page = self._collection.get(
                where={"report_id": report_id}, limit=100, offset=offset,
                include=["documents", "metadatas"],
            )
            ids = page.get("ids") or []
            docs = page.get("documents") or []
            metas = page.get("metadatas") or []
            results.extend(
                {"id": rid, "content": doc or "", "metadata": meta or {}}
                for rid, doc, meta in zip(ids, docs, metas)
            )
            if len(ids) < 100:
                break
            offset += len(ids)
        return sorted(results, key=lambda entry: entry["metadata"].get("section_index", -1))

    def get_memory(self, doc_id: str) -> dict | None:
        """按确定性 ID 读取记录，无需生成 query embedding（幂等写入用）。"""
        got = self._collection.get(ids=[doc_id], include=["documents", "metadatas"])
        ids = got.get("ids") or []
        if not ids:
            return None
        return {
            "id": ids[0],
            "content": (got.get("documents") or [""])[0],
            "metadata": (got.get("metadatas") or [{}])[0] or {},
        }

    def count(self) -> int:
        return self._collection.count()

    def update_metadata(self, doc_id: str, metadata: dict) -> None:
        """不改变文本/embedding，只更新状态，防止记忆重试的重复编码。"""
        self._collection.update_metadata(ids=[doc_id], metadatas=[metadata])

    def update_memory(self, doc_id: str, content: str, metadata: dict | None = None) -> None:
        """更新已有记忆（显式 embedding）。"""
        self._collection.update(
            ids=[doc_id],
            documents=[content],
            metadatas=[metadata] if metadata else None,
        )
        logger.info("Updated memory: %s", doc_id)
