"""基于 ChromaDB 多 Collection 的结构化记忆存储。

所有文档和查询向量通过同一个 ManagedCollection / EmbeddingClient 生成；
只允许 Chroma 支持的标量 metadata。列表字段以 JSON 字符串往返序列化。
兼容之前保存为标量 metadata 的历史记录（无需重建向量）。
"""

from __future__ import annotations

import json
import uuid
from typing import TypeVar

import chromadb
from chromadb.config import Settings as ChromaSettings

from deep_research.memory.embeddings import EmbeddingClient
from deep_research.memory.schemas import Entity, MemoryClaim, Evidence, Contradiction
from deep_research.memory.schema_guard import ManagedCollection, open_managed_collection
from deep_research.settings import get_engine_settings
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)

STRUCTURED_COLLECTIONS = (
    "memory_entities",
    "memory_claims",
    "memory_evidence",
    "memory_contradictions",
)

_LIST_FIELDS = {"aliases", "entities"}
_T = TypeVar("_T", Entity, MemoryClaim, Evidence, Contradiction)


def _clean_metadata(meta: dict) -> dict:
    """Chroma metadata 只允许 str/int/float/bool；复杂值编码为 JSON 标量。"""
    result = {}
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, (list, dict, tuple)):
            result[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        elif isinstance(value, (str, int, float, bool)):
            result[key] = value
        else:
            raise TypeError(f"unsupported Chroma metadata type for {key}: {type(value).__name__}")
    return result


def _restore_metadata(meta: dict) -> dict:
    """还原实体/断言的列表字段；兼容原来不含此字段的历史数据。"""
    result = dict(meta)
    for key in _LIST_FIELDS:
        value = result.get(key)
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
                result[key] = decoded if isinstance(decoded, list) else []
            except (TypeError, ValueError):
                logger.warning("Invalid %s metadata encountered; treating as empty list", key)
                result[key] = []
    return result


def _models_from_get(model: type[_T], payload: dict) -> list[_T]:
    """Chroma get() 的 metadatas 已经是 dict，不可以再 json.loads(dict)。"""
    ids = payload.get("ids") or []
    metadatas = payload.get("metadatas") or []
    return [
        model(**{**_restore_metadata(metadata), "id": record_id})
        for record_id, metadata in zip(ids, metadatas)
        if isinstance(metadata, dict)
    ]


class StructuredMemoryStore:
    """管理 Entity / Claim / Evidence / Contradiction 四个 Chroma 集合。"""

    def __init__(
        self, persist_dir: str | None = None, embedder: EmbeddingClient | None = None
    ) -> None:
        persist_dir = persist_dir or str(get_engine_settings().resolved_data_dir / "chroma")
        self._client = chromadb.PersistentClient(
            path=persist_dir, settings=ChromaSettings(anonymized_telemetry=False)
        )
        self._embedder = embedder or EmbeddingClient()
        self._entities: ManagedCollection = open_managed_collection(
            self._client, "memory_entities", self._embedder
        )
        self._claims: ManagedCollection = open_managed_collection(
            self._client, "memory_claims", self._embedder
        )
        self._evidences: ManagedCollection = open_managed_collection(
            self._client, "memory_evidence", self._embedder
        )
        self._contradictions: ManagedCollection = open_managed_collection(
            self._client, "memory_contradictions", self._embedder
        )
        logger.info(
            "StructuredMemoryStore initialized: e=%d c=%d ev=%d ct=%d (%s)",
            self._entities.count(), self._claims.count(), self._evidences.count(),
            self._contradictions.count(), self._embedder.identity.describe(),
        )

    def _search_collection(self, collection: ManagedCollection, query: str, top_k: int = 5) -> list[dict]:
        return collection.search(query, top_k=top_k) if query and top_k > 0 else []

    def upsert_entities(self, entities: list[Entity]) -> list[str]:
        if not entities:
            return []
        for entity in entities:
            entity.id = entity.id or uuid.uuid4().hex[:12]
        self._entities.upsert(
            ids=[entity.id for entity in entities],
            documents=[f"{entity.name}: {entity.description}" for entity in entities],
            metadatas=[_clean_metadata(entity.model_dump(mode="json")) for entity in entities],
        )
        return [entity.id for entity in entities]

    def upsert_entity(self, entity: Entity) -> str:
        return self.upsert_entities([entity])[0]

    def search_entities(self, query: str, top_k: int = 5) -> list[Entity]:
        return [
            Entity(**_restore_metadata(r["metadata"]))
            for r in self._search_collection(self._entities, query, top_k)
            if r.get("metadata")
        ]

    def upsert_claims(self, claims: list[MemoryClaim]) -> list[str]:
        if not claims:
            return []
        for claim in claims:
            claim.id = claim.id or uuid.uuid4().hex[:12]
        # A Claim can recur across a report's windows, or be retried after a
        # partial crash.  Never reset a previously validated Claim or discard
        # an already grounded source just because a later window lacks it.
        prior = {
            obj.id: obj for obj in _models_from_get(
                MemoryClaim, self._claims.get(ids=[c.id for c in claims], include=["metadatas"])
            )
        }
        for claim in claims:
            old = prior.get(claim.id)
            if old is None:
                continue
            claim.entities = list(dict.fromkeys([*old.entities, *claim.entities]))
            claim.source_url = claim.source_url or old.source_url
            claim.source_title = claim.source_title or old.source_title
            claim.valid_from = claim.valid_from or old.valid_from
            claim.valid_to = claim.valid_to or old.valid_to
            claim.source_published_at = claim.source_published_at or old.source_published_at
            claim.supersedes_claim_id = claim.supersedes_claim_id or old.supersedes_claim_id
            claim.observed_at = old.observed_at or claim.observed_at
            claim.first_seen = old.first_seen
            if old.verification_status != "unverified":
                claim.verification_status = old.verification_status
        self._claims.upsert(
            ids=[claim.id for claim in claims],
            documents=[claim.text for claim in claims],
            metadatas=[_clean_metadata(claim.model_dump(mode="json")) for claim in claims],
        )
        return [claim.id for claim in claims]

    def upsert_claim(self, claim: MemoryClaim) -> str:
        return self.upsert_claims([claim])[0]

    def get_claim_by_id(self, claim_id: str) -> MemoryClaim | None:
        if not claim_id:
            return None
        got = _models_from_get(MemoryClaim, self._claims.get(ids=[claim_id], include=["metadatas"]))
        return got[0] if got else None

    def search_claims(self, query: str, top_k: int = 5) -> list[MemoryClaim]:
        return [
            MemoryClaim(**_restore_metadata(r["metadata"]))
            for r in self._search_collection(self._claims, query, top_k)
            if r.get("metadata")
        ]

    def search_claims_keyword(self, query: str, top_k: int = 10) -> list[MemoryClaim]:
        """Lexical candidates from Chroma document contains filters."""
        from deep_research.memory.retrieval import lexical_score, search_terms

        if not query.strip() or top_k <= 0 or self._claims.count() == 0:
            return []
        matched: dict[str, MemoryClaim] = {}
        for term in search_terms(query):
            got = self._claims.get(
                where_document={"$contains": term}, limit=min(max(3 * top_k, 10), 60),
                include=["metadatas"],
            )
            for claim in _models_from_get(MemoryClaim, got):
                matched[claim.id] = claim
        return sorted(
            matched.values(), key=lambda c: (-lexical_score(query, c.text), c.id),
        )[:top_k]

    def get_claims_by_entity(self, entity_name: str) -> list[MemoryClaim]:
        """按精确实体名称/别名/ID 查关联 Claim。

        Claim.entities 存的是 ID 列表，Chroma metadata 不支持对 JSON 数组
        做 `$contains` 过滤，因此逐页扫描 metadata，避免返回错误的空结果。
        规模很大时应迁移为独立关系索引（见实施说明）。
        """
        name = entity_name.strip().casefold()
        if not name:
            return []
        entity_ids = {entity_name}
        for entity in _models_from_get(Entity, self._entities.get(include=["metadatas"])):
            if entity.id.casefold() == name or entity.name.casefold() == name or any(
                alias.casefold() == name for alias in entity.aliases
            ):
                entity_ids.add(entity.id)
        claims = _models_from_get(MemoryClaim, self._claims.get(include=["metadatas"]))
        return [
            claim for claim in claims
            if any(ref in entity_ids or ref.casefold() == name for ref in claim.entities)
        ]

    def upsert_evidences(self, evidences: list[Evidence]) -> list[str]:
        if not evidences:
            return []
        for evidence in evidences:
            evidence.id = evidence.id or uuid.uuid4().hex[:12]
        self._evidences.upsert(
            ids=[evidence.id for evidence in evidences],
            documents=[evidence.description or evidence.url or evidence.id for evidence in evidences],
            metadatas=[_clean_metadata(evidence.model_dump(mode="json")) for evidence in evidences],
        )
        return [evidence.id for evidence in evidences]

    def upsert_evidence(self, evidence: Evidence) -> str:
        return self.upsert_evidences([evidence])[0]

    def get_evidence_by_claim(self, claim_id: str) -> list[Evidence]:
        if not claim_id:
            return []
        got = self._evidences.get(where={"claim_id": claim_id}, include=["metadatas"])
        return _models_from_get(Evidence, got)

    def search_evidence(self, query: str, top_k: int = 5) -> list[Evidence]:
        return [
            Evidence(**_restore_metadata(r["metadata"]))
            for r in self._search_collection(self._evidences, query, top_k)
            if r.get("metadata")
        ]

    def upsert_contradictions(self, contradictions: list[Contradiction]) -> list[str]:
        if not contradictions:
            return []
        for contradiction in contradictions:
            contradiction.id = contradiction.id or uuid.uuid4().hex[:12]
        existing = {obj.id: obj for obj in _models_from_get(
            Contradiction, self._contradictions.get(
                ids=[c.id for c in contradictions], include=["metadatas"],
            ),
        )}
        for contradiction in contradictions:
            previous = existing.get(contradiction.id)
            if previous and previous.review_status != "unverified":
                contradiction.review_status = previous.review_status
                contradiction.resolution = previous.resolution or contradiction.resolution
        self._contradictions.upsert(
            ids=[contradiction.id for contradiction in contradictions],
            documents=[contradiction.description or contradiction.id for contradiction in contradictions],
            metadatas=[_clean_metadata(contradiction.model_dump(mode="json")) for contradiction in contradictions],
        )
        return [contradiction.id for contradiction in contradictions]

    def upsert_contradiction(self, contradiction: Contradiction) -> str:
        return self.upsert_contradictions([contradiction])[0]

    def get_contradiction_by_id(self, relation_id: str) -> Contradiction | None:
        if not relation_id:
            return None
        got = _models_from_get(Contradiction, self._contradictions.get(
            ids=[relation_id], include=["metadatas"],
        ))
        return got[0] if got else None

    def get_contradictions(self) -> list[Contradiction]:
        if self._contradictions.count() == 0:
            return []
        return _models_from_get(Contradiction, self._contradictions.get(include=["metadatas"]))

    def get_contradictions_for_claims(self, claim_ids: set[str]) -> list[Contradiction]:
        """只读取关联的冲突；支持 `$in/$or` 的 Chroma 走索引过滤。

        老版本 Chroma 若不支持组合查询，退化为只读全量过滤，不丢结果。
        """
        ids = sorted(cid for cid in claim_ids if cid)
        if not ids or self._contradictions.count() == 0:
            return []
        try:
            got = self._contradictions.get(
                where={"$or": [
                    {"claim_a_id": {"$in": ids}},
                    {"claim_b_id": {"$in": ids}},
                ]},
                include=["metadatas"],
            )
            return _models_from_get(Contradiction, got)
        except Exception as exc:
            logger.debug("Chroma contradiction relation filter unavailable: %s", exc)
            return [
                item for item in self.get_contradictions()
                if item.claim_a_id in claim_ids or item.claim_b_id in claim_ids
            ]

    def stats(self) -> dict:
        return {
            "entities": self._entities.count(),
            "claims": self._claims.count(),
            "evidence": self._evidences.count(),
            "contradictions": self._contradictions.count(),
        }
