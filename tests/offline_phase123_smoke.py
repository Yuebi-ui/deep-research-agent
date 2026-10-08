"""Dependency-free smoke harness using minimal strict Chroma/LangChain stand-ins.

Run from project root with `python tests/offline_phase123_smoke.py` when full runtime
requirements are unavailable. These are NOT real Chroma integration tests;
use `pytest tests/test_memory_phase123.py tests/test_writer_phase2.py` in deployment.
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if (ROOT / "core" / "deep_research").is_dir():
    ROOT = ROOT / "core"  # 原始 review bundle 使用 core/ 包裹真实仓库根目录


def fake_package(name: str, root: Path):
    module = types.ModuleType(name)
    module.__path__ = [str(root)]
    sys.modules[name] = module
    return module


app = fake_package("deep_research", ROOT / "deep_research")
fake_package("deep_research.memory", ROOT / "deep_research" / "memory")
fake_package("deep_research.prompts", ROOT / "deep_research" / "prompts")

logger_module = types.ModuleType("deep_research.logging")
logger_module.get_logger = logging.getLogger
sys.modules[logger_module.__name__] = logger_module
app.logging = logger_module

settings = types.ModuleType("deep_research.settings")
settings.get_engine_settings = lambda: SimpleNamespace(resolved_data_dir=Path(tempfile.gettempdir()))
sys.modules[settings.__name__] = settings

utils = types.ModuleType("deep_research.utils")
utils.parse_json_response = json.loads
sys.modules[utils.__name__] = utils

langchain = types.ModuleType("langchain_core")
langchain.__path__ = []
sys.modules["langchain_core"] = langchain
messages = types.ModuleType("langchain_core.messages")
messages.HumanMessage = lambda content: SimpleNamespace(content=content)
sys.modules[messages.__name__] = messages

response_holder = {"payload": {}, "calls": 0}


class FakeLLM:
    def invoke(self, messages):
        response_holder["calls"] += 1
        return SimpleNamespace(content=json.dumps(response_holder["payload"], ensure_ascii=False))


llm = types.ModuleType("deep_research.llm")
llm.get_chat_model_for_task = lambda _: FakeLLM()
sys.modules[llm.__name__] = llm


class FakeIdentity:
    @classmethod
    def from_markers(cls, marker):
        if marker is None:
            return None
        return cls()

    def as_markers(self):
        return {"dr_embedding_provider": "fake", "dr_embedding_schema_version": 1}

    def describe(self):
        return "offline fake"

    def __eq__(self, other):
        return isinstance(other, FakeIdentity)


class EmbeddingClient:
    def __init__(self, *args, **kwargs):
        self.identity = FakeIdentity()

    def embed(self, texts):
        return [[float((len(text) + i) % 17) for i in range(4)] for text in texts]


embed = types.ModuleType("deep_research.memory.embeddings")
embed.EmbeddingClient = EmbeddingClient
embed.EmbeddingIdentity = FakeIdentity
embed.fake_embedding = lambda text, dims=1024: [0.0] * dims
embed._EMBEDDING_DIMS = 1024
sys.modules[embed.__name__] = embed


class NotFoundError(Exception):
    pass


class FakeCollection:
    def __init__(self, name, metadata):
        self.name = name
        self.metadata = metadata
        self.records = {}

    def count(self):
        return len(self.records)

    def upsert(self, *, ids, embeddings, documents, metadatas=None):
        for n, rid in enumerate(ids):
            meta = (metadatas or [{}] * len(ids))[n]
            assert all(isinstance(v, (str, int, float, bool)) for v in meta.values()), meta
            self.records[rid] = {"document": documents[n], "metadata": meta, "embedding": embeddings[n]}

    def update(self, *, ids, embeddings=None, documents=None, metadatas=None):
        for n, rid in enumerate(ids):
            assert rid in self.records
            record = self.records[rid]
            if documents is not None:
                record["document"] = documents[n]
            if embeddings is not None:
                record["embedding"] = embeddings[n]
            if metadatas is not None:
                meta = metadatas[n]
                assert all(isinstance(v, (str, int, float, bool)) for v in meta.values())
                record["metadata"] = meta

    def get(self, *, ids=None, where=None, include=None, **kwargs):
        entries = list(self.records.items())
        if ids is not None:
            entries = [(k, v) for k, v in entries if k in ids]
        if where is not None:
            def matches(meta, clause):
                if "$or" in clause:
                    return any(matches(meta, branch) for branch in clause["$or"])
                if "$and" in clause:
                    return all(matches(meta, branch) for branch in clause["$and"])
                for key, value in clause.items():
                    if isinstance(value, dict) and "$in" in value:
                        if meta.get(key) not in value["$in"]:
                            return False
                    elif meta.get(key) != value:
                        return False
                return True
            entries = [(k, v) for k, v in entries if matches(v["metadata"], where)]
        where_document = kwargs.get("where_document")
        if where_document:
            substring = where_document.get("$contains", "")
            entries = [(k, v) for k, v in entries if substring in v["document"]]
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit")
        entries = entries[offset: offset + limit if limit is not None else None]
        return {
            "ids": [key for key, _ in entries],
            "documents": [val["document"] for _, val in entries],
            "metadatas": [val["metadata"] for _, val in entries],
            "embeddings": [val["embedding"] for _, val in entries],
        }

    def query(self, *, query_embeddings, n_results):
        entries = list(self.records.items())[:n_results]
        return {
            "ids": [[k for k, _ in entries]],
            "documents": [[val["document"] for _, val in entries]],
            "metadatas": [[val["metadata"] for _, val in entries]],
        }


_CLIENTS = {}


class FakeClient:
    def __init__(self, path, settings):
        self.collections = _CLIENTS.setdefault(path, {})

    def get_collection(self, name):
        if name not in self.collections:
            raise NotFoundError(name)
        return self.collections[name]

    def create_collection(self, name, embedding_function=None, metadata=None):
        collection = FakeCollection(name, metadata)
        self.collections[name] = collection
        return collection


chromadb = types.ModuleType("chromadb")
chromadb.PersistentClient = FakeClient
sys.modules["chromadb"] = chromadb
chromadb_config = types.ModuleType("chromadb.config")
chromadb_config.Settings = lambda **kwargs: kwargs
sys.modules[chromadb_config.__name__] = chromadb_config
chromadb_errors = types.ModuleType("chromadb.errors")
chromadb_errors.NotFoundError = NotFoundError
sys.modules[chromadb_errors.__name__] = chromadb_errors

from deep_research.memory.manager import MemoryManager, _similarity  # noqa: E402
from deep_research.memory.schemas import Entity, MemoryClaim, Evidence, Contradiction  # noqa: E402
from deep_research.memory.structured_store import StructuredMemoryStore  # noqa: E402
from deep_research.writer_validation import validate_report_citations  # noqa: E402
from deep_research.writer_context import unique_notes, build_final_report_prompt  # noqa: E402

URL = "https://example.org/research/report-2026"
BODY = (f"北京创新实验室报告称2026年增长12%，详见 {URL}。\n" * 12)


def payload(url=URL):
    return {
        "entities": [{"name": "北京创新实验室", "type": "organization", "importance": 8}],
        "claims": [{
            "text": "北京创新实验室在2026年同比增长12%",
            "entities": ["北京创新实验室"], "source_url": url,
            "evidence": [{"url": url, "type": "official_doc", "description": "报告披露同比增长12%"}],
        }],
        "contradictions": [{"description": "历史冲突（未关联）"}],
    }


def new_manager(name, **kwargs):
    response_holder["payload"] = kwargs.get("payload", payload())
    return MemoryManager(persist_dir=f"offline-memory-{name}")


def run():
    count = 0

    def check(label, fn):
        nonlocal count
        fn()
        count += 1
        print(f"PASS {count:02d} {label}")

    def test_1():
        assert _similarity("2026年投入增长12%", "2026年投入增长13%") > .5
        assert _similarity("", "") == 0
    check("Chinese text similarity and blank inputs", test_1)

    store = StructuredMemoryStore(persist_dir="offline-direct")

    def test_2():
        store.upsert_entities([Entity(id="e1", name="实验室", type="organization", aliases=["别名"])])
        store.upsert_claims([MemoryClaim(id="c1", text="增长12%", entities=["e1"])])
        store.upsert_evidence(Evidence(id="ev1", claim_id="c1", type="official_doc", description="年报"))
        store.upsert_contradiction(Contradiction(id="ct1", claim_a_id="c1", description="冲突"))
        assert store.search_entities("实验室", 1)[0].aliases == ["别名"]
        assert store.search_claims("增长", 1)[0].entities == ["e1"]
        assert store.get_claims_by_entity("别名")[0].id == "c1"
        assert store.get_evidence_by_claim("c1")[0].id == "ev1"
        assert store.get_contradictions()[0].id == "ct1"
        assert store.get_contradictions_for_claims({"c1"})[0].id == "ct1"
    check("Scalar Chroma metadata + dict deserialization + entity links", test_2)

    manager = new_manager("idempotent")
    first = manager.store_from_report("北京创新实验室", BODY)

    def test_3():
        assert first is not None
        assert manager._store.get_memory(first)["metadata"]["structured_status"] == "complete"
        assert manager._structured.stats()["claims"] == 1
    check("Stable report ID and completed extraction", test_3)

    def test_4():
        before = response_holder["calls"]
        assert manager.store_from_report("另外的问题", BODY.replace("。\n", "。\n\n")) == first
        assert response_holder["calls"] == before
        assert manager.count() == 1
    check("Idempotent retry and whitespace-equivalent Chinese text", test_4)

    def test_5():
        second = manager.store_from_report("北京创新实验室", BODY.replace("12%", "13%"))
        assert first != second
        assert manager.count() == 2 and manager._structured.stats()["claims"] == 2
    check("Different report versions are not overwritten", test_5)

    def test_6():
        claims = manager._structured.get_claims_by_entity("北京创新实验室")
        assert len(claims) == 2
        for claim in claims:
            assert claim.verification_status == "unverified"
            assert claim.report_id
            evidences = manager._structured.get_evidence_by_claim(claim.id)
            assert len(evidences) == 1 and evidences[0].report_id == claim.report_id
    check("Claim -> Entity / Report / Evidence provenance", test_6)

    def test_7():
        context = manager.retrieve_context("北京创新实验室", 2)
        assert "历史研究线索_未经当前核查" in context
        assert "历史断言" in context and "候选证据" in context
    check("Structured + semantic hybrid retrieval", test_7)

    def test_8():
        assert manager._structured.stats()["contradictions"] == 2
        assert all(not ct.claim_a_id for ct in manager.get_contradictions())
    check("Legacy contradictions preserved without invented links", test_8)

    def test_9():
        bad = new_manager("bad-url", payload=payload("https://madeup.invalid/article"))
        bad.store_from_report("北京创新实验室", BODY)
        claim = bad.query_claims("增长", 1)[0]
        assert claim.source_url is None
        assert not bad._structured.get_evidence_by_claim(claim.id)
    check("Hallucinated URLs cannot turn into evidence", test_9)

    def test_10():
        retry = new_manager("retry")
        original = retry._structured.upsert_claims
        broken = {"once": True}

        def flaky(items):
            if broken["once"]:
                broken["once"] = False
                raise RuntimeError("disk temporarily unavailable")
            return original(items)

        retry._structured.upsert_claims = flaky
        record_id = retry.store_from_report("北京创新实验室", BODY)
        assert retry._store.get_memory(record_id)["metadata"]["structured_status"] == "pending"
        retry.store_from_report("北京创新实验室", BODY)
        assert retry._store.get_memory(record_id)["metadata"]["structured_status"] == "complete"
        assert retry._structured.stats()["entities"] == 1
        assert retry._structured.stats()["claims"] == 1
    check("Partial batch failure remains retryable and idempotent", test_10)

    def test_11():
        assert unique_notes(["报告12%", " 报告12% ", "报告13%", ""]) == ["报告12%", "报告13%"]
        prompt, info = build_final_report_prompt(
            research_brief="研究增长", draft_report="初稿", notes=["报告12%", "报告12%", "报告13%"],
            date="2026-10-08",
        )
        assert prompt.count("报告12%") == 1
        assert info["notes_kept"] == 2
    check("Writer dedup with conflicting facts retained", test_11)

    def test_12():
        good = f"结论增长[1]。\n\n### 来源列表\n[1] 报告：{URL}"
        assert validate_report_citations(good, BODY)["ok"]
        bad = "结论增长[2]。\n\n### 来源列表\n[1] 虚构：https://faked.example/a"
        issues = validate_report_citations(bad, BODY)["issues"]
        assert "unresolved_citations" in issues
        assert "url_not_found_in_research_material" in issues
    check("Chinese citations and unsupported-link warnings", test_12)

    def test_13():
        mgr = new_manager("fallback")
        mgr.store_from_report("北京创新实验室", BODY)
        mgr._structured.search_claims = lambda *a, **kw: (_ for _ in ()).throw(OSError("down"))
        assert "历史报告" in mgr.retrieve_context("北京创新实验室")
    check("Structured retrieval failure degrades to semantic", test_13)

    def test_14():
        seq = StructuredMemoryStore(persist_dir="offline-batch-seq")
        batch = StructuredMemoryStore(persist_dir="offline-batch-bat")
        entities = [Entity(id=f"en{i}", name=f"技术{i}", type="technology") for i in range(4)]
        claims = [MemoryClaim(id=f"cl{i}", text=f"技术{i}增长", entities=[f"en{i}"]) for i in range(4)]
        for item in entities:
            seq.upsert_entity(item)
        for item in claims:
            seq.upsert_claim(item)
        batch.upsert_entities(entities)
        batch.upsert_claims(claims)
        assert seq._entities.get() == batch._entities.get()
        assert seq._claims.get() == batch._claims.get()
    check("Sequential vs batch metadata/embedding equality", test_14)

    def test_15():
        data = payload()
        data["claims"].append({"text": "北京创新实验室2026年同比下降2%", "entities": ["北京创新实验室"]})
        data["contradictions"] = [{
            "claim_a": data["claims"][0]["text"], "claim_b": data["claims"][1]["text"],
            "description": "增长与下降冲突",
        }]
        related = new_manager("linked-contradiction", payload=data)
        related.store_from_report("北京创新实验室", BODY)
        conflicts = related.get_contradictions()
        assert len(conflicts) == 1 and conflicts[0].claim_a_id and conflicts[0].claim_b_id
        assert "历史冲突" in related.retrieve_context("实验室", top_k=3)
    check("Linked contradictions join claims and appear in retrieval", test_15)

    def test_16():
        empty = new_manager("boundary")
        assert empty.store_from_report("", "") is None
        assert empty.store_from_report("a", "short") is None
        assert empty.retrieve_context("", top_k=0) == ""
        assert empty.count() == 0
    check("Empty and very short inputs are no-ops", test_16)

    def test_17():
        recovery = new_manager("invalid-extraction", payload={"claims": "invalid"})
        identifier = recovery.store_from_report("topic", BODY)
        assert recovery._store.get_memory(identifier)["metadata"]["structured_status"] == "pending"
        response_holder["payload"] = payload()
        recovery.store_from_report("topic", BODY)
        assert recovery._store.get_memory(identifier)["metadata"]["structured_status"] == "complete"
    check("Unparseable LLM schema is retried, not marked complete", test_17)

    def test_18():
        # 在不安装模型库的环境中，仍使用源码中的真实 fit_prompt 路径。
        models = types.ModuleType("deep_research.model_capabilities")
        models.local_backend_info = lambda: ("http://localhost", "dummy")
        models.probe_local_model = lambda *args: None
        models.resolve_tokenizer_path = lambda *args: None
        sys.modules[models.__name__] = models
        settings.get_engine_settings = lambda: SimpleNamespace(stage="default", resolved_data_dir=Path(tempfile.gettempdir()))
        utils.load_config = lambda **kwargs: {
            "roles": {"writer": {"backend": "openai_local", "handle": "local-writer"}},
            "cognition": {"openai_local": {"models": {"local-writer": {"context_window": 2400}}}},
            "context_budget": {"reserve_output_tokens": 300, "safety_margin_tokens": 100},
        }
        # context_budget 可能被前一个 fallback 测试首次导入；绑定新 settings stub。
        import deep_research.context_budget as budget_module
        budget_module.get_engine_settings = settings.get_engine_settings
        prompt, stats = build_final_report_prompt(
            research_brief="测试报告总结", draft_report="草稿信息，" * 300,
            notes=["记录部分，" * 500, "价格数据和来源 " * 220], date="2026-10-08",
        )
        from deep_research.context_budget import get_token_counter
        assert isinstance(stats["budget"], dict)
        assert stats["budget"]["context_limit"] == 2400
        assert get_token_counter().count(prompt) <= 2000
        assert stats["budget"]["applied"] is True
    check("Writer real context-budget algorithm obeys model limit", test_18)

    def test_19():
        import os
        os.environ["DR_MEMORY_STRUCTURED_RETRIEVAL"] = "off"
        try:
            assert "历史断言" not in manager.retrieve_context("北京创新实验室")
            assert "历史报告" in manager.retrieve_context("北京创新实验室")
        finally:
            os.environ.pop("DR_MEMORY_STRUCTURED_RETRIEVAL", None)
        os.environ["DR_WRITER_CONTEXT_BUDGET"] = "off"
        try:
            prompt, info = build_final_report_prompt(
                research_brief="增长", draft_report="旧草稿", notes=["文本", "文本"], date="2026-10-08"
            )
            assert info["budget"] == "disabled" and prompt.count("文本") == 1
        finally:
            os.environ.pop("DR_WRITER_CONTEXT_BUDGET", None)
    check("Feature flags safely revert expensive paths", test_19)

    print(f"OFFLINE SMOKE: {count} PASSED, 0 FAILED (fake store; not real Chroma integration)")


if __name__ == "__main__":
    run()
