"""Structured Memory embedding schema 迁移（Phase 3C P0）。

迁移 = **重编码**，不是丢弃：

1. 读出现存 collection 的 ids / documents / metadatas；
2. 删除并重建 collection（写入当前 embedding identity marker，space=cosine）；
3. 用当前 :class:`EmbeddingClient` 对原 document 文本重新编码并写回；
4. 校验：count / ids / documents / metadatas 与迁移前完全一致，
   且新向量维度等于当前 identity 的 dimension。

数据保真：只有向量被重算，文本与元数据原样保留。任何校验失败都会抛错，
调用方（``scripts/migrate_memory_schema.py``）在 --apply 前会整目录备份。

本模块不自动运行——只由显式 CLI 或测试调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import chromadb
from chromadb.config import Settings as ChromaSettings
from chromadb.errors import NotFoundError

from deep_research.memory.embeddings import MAX_BATCH_SIZE, EmbeddingClient, EmbeddingIdentity
from deep_research.memory.schema_guard import (
    DEFAULT_SPACE,
    MemorySchemaError,
    open_managed_collection,
)
from deep_research.memory.vector_store import DEFAULT_COLLECTION, SECTION_COLLECTION
from deep_research.memory.structured_store import STRUCTURED_COLLECTIONS
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)

# 迁移需要覆盖的全部 collection（含各自的 hnsw space）
MANAGED_COLLECTIONS: tuple[tuple[str, str], ...] = tuple(
    [(DEFAULT_COLLECTION, DEFAULT_SPACE), (SECTION_COLLECTION, DEFAULT_SPACE)]
    + [(name, DEFAULT_SPACE) for name in STRUCTURED_COLLECTIONS]
)

_MIGRATION_BATCH = MAX_BATCH_SIZE  # 与 provider 单次请求上限一致


@dataclass
class CollectionPlan:
    name: str
    space: str
    action: str  # "skip_ok" | "migrate"
    reason: str
    count: int
    stored: dict | None = None


@dataclass
class MigrationReport:
    persist_dir: str
    applied: bool
    identity: dict
    plans: list[CollectionPlan] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "persist_dir": self.persist_dir,
            "applied": self.applied,
            "target_identity": self.identity,
            "plans": [p.__dict__ for p in self.plans],
            "results": self.results,
        }


def _open_client(persist_dir: str) -> Any:
    return chromadb.PersistentClient(
        path=persist_dir,
        settings=ChromaSettings(anonymized_telemetry=False),
    )


def plan_migration(client: Any, identity: EmbeddingIdentity) -> list[CollectionPlan]:
    """检查每个 collection 的 marker，生成迁移计划（不修改任何数据）。"""
    plans: list[CollectionPlan] = []
    for name, space in MANAGED_COLLECTIONS:
        try:
            collection = client.get_collection(name)
        except NotFoundError:
            plans.append(CollectionPlan(name, space, "skip_ok", "collection 不存在", 0))
            continue
        stored = EmbeddingIdentity.from_markers(collection.metadata)
        count = int(collection.count())
        if stored == identity:
            plans.append(
                CollectionPlan(name, space, "skip_ok", "identity 已匹配", count)
            )
        elif stored is None:
            plans.append(
                CollectionPlan(
                    name, space, "migrate",
                    "legacy collection：无 embedding identity marker", count,
                )
            )
        else:
            plans.append(
                CollectionPlan(
                    name, space, "migrate",
                    f"identity 不匹配：{stored.describe()} != {identity.describe()}", count,
                    stored={
                        "provider": stored.provider,
                        "model": stored.model,
                        "dimension": stored.dimension,
                        "schema_version": stored.schema_version,
                    },
                )
            )
    return plans


def _export(collection: Any) -> tuple[list[str], list[str], list[dict]]:
    got = collection.get(include=["documents", "metadatas"])
    ids = list(got["ids"])
    documents = list(got["documents"])
    metadatas = list(got["metadatas"])
    if len(ids) != len(documents) or len(ids) != len(metadatas):
        raise MemorySchemaError(
            collection.name, "collection.get 返回长度不一致，拒绝迁移"
        )
    missing = [i for i, doc in zip(ids, documents) if doc is None]
    if missing:
        raise MemorySchemaError(
            collection.name, f"{len(missing)} 条记录缺少 document 文本，无法重编码"
        )
    return ids, documents, metadatas


def migrate_collection(
    client: Any,
    name: str,
    embedder: EmbeddingClient,
    space: str = DEFAULT_SPACE,
    *,
    batch_size: int = _MIGRATION_BATCH,
) -> dict:
    """重编码单个 collection；返回迁移前后的对照信息（含校验结果）。"""
    try:
        old = client.get_collection(name)
    except NotFoundError:
        # 不存在 → 直接建一个带 marker 的空 collection
        open_managed_collection(client, name, embedder, space=space)
        return {"collection": name, "action": "created_empty", "count": 0, "verified": True}

    before_count = int(old.count())
    ids, documents, metadatas = _export(old)

    client.delete_collection(name)
    managed = open_managed_collection(client, name, embedder, space=space)

    for start in range(0, len(ids), batch_size):
        chunk_ids = ids[start : start + batch_size]
        chunk_docs = documents[start : start + batch_size]
        chunk_metas = metadatas[start : start + batch_size]
        managed.upsert(ids=chunk_ids, documents=chunk_docs, metadatas=chunk_metas)

    verified = _verify(client, name, ids, documents, metadatas, embedder.identity)
    logger.info(
        "Migrated collection '%s': %d docs re-embedded as %s",
        name, len(ids), embedder.identity.describe(),
    )
    return {
        "collection": name,
        "action": "migrated",
        "count_before": before_count,
        "count": len(ids),
        "verified": verified,
        "identity": embedder.identity.describe(),
    }


def _verify(
    client: Any,
    name: str,
    ids: list[str],
    documents: list[str],
    metadatas: list[dict],
    identity: EmbeddingIdentity,
) -> bool:
    """迁移后校验：ids/documents/metadatas 保真 + 向量维度正确。"""
    collection = client.get_collection(name)
    if int(collection.count()) != len(ids):
        raise MemorySchemaError(name, f"迁移后 count 不一致：{collection.count()} != {len(ids)}")
    got = collection.get(include=["documents", "metadatas", "embeddings"])
    got_map = {
        i: (d, m, e)
        for i, d, m, e in zip(got["ids"], got["documents"], got["metadatas"], got["embeddings"])
    }
    if set(got_map) != set(ids):
        raise MemorySchemaError(name, "迁移后 ids 集合不一致")
    for i, doc, meta in zip(ids, documents, metadatas):
        new_doc, new_meta, new_emb = got_map[i]
        if new_doc != doc:
            raise MemorySchemaError(name, f"{i} document 不一致")
        if (new_meta or {}) != (meta or {}):
            raise MemorySchemaError(name, f"{i} metadata 不一致")
        if new_emb is None or len(new_emb) != identity.dimension:
            dim = None if new_emb is None else len(new_emb)
            raise MemorySchemaError(
                name, f"{i} 向量维度 {dim} != {identity.dimension}"
            )
    return True


def run_migration(
    persist_dir: str,
    embedder: EmbeddingClient,
    *,
    apply: bool = False,
) -> MigrationReport:
    """dry-run（默认）或执行迁移；返回结构化报告。"""
    client = _open_client(persist_dir)
    identity = embedder.identity
    report = MigrationReport(
        persist_dir=persist_dir,
        applied=apply,
        identity={
            "provider": identity.provider,
            "model": identity.model,
            "dimension": identity.dimension,
            "schema_version": identity.schema_version,
        },
    )
    report.plans = plan_migration(client, identity)
    if not apply:
        return report

    for plan in report.plans:
        if plan.action != "migrate":
            report.results.append(
                {"collection": plan.name, "action": "skipped", "reason": plan.reason}
            )
            continue
        report.results.append(migrate_collection(client, plan.name, embedder, plan.space))
    return report
