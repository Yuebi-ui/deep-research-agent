"""Phase 3C P0：memory embedding schema 迁移的正确性测试。

覆盖 Section 5 Test 5 的「明确 migration path」一半：
- legacy / mismatch collection 被 plan 识别为需要迁移；
- 迁移后 ids / documents / metadatas 保真、marker 正确、检索可用；
- 兼容 collection 被跳过（不被无谓重写）；
- CLI 安全阀：fake 模式下 --apply 默认拒绝。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import chromadb
from chromadb.config import Settings as ChromaSettings

from deep_research.memory.embeddings import EmbeddingIdentity
from deep_research.memory.migration import plan_migration, run_migration
from deep_research.memory.structured_store import StructuredMemoryStore
from tests.test_memory_embedding_space import D1, D2, SemanticFakeEmbedder

REPO_ROOT = Path(__file__).resolve().parents[1]


def _client(persist_dir: str):
    return chromadb.PersistentClient(
        path=persist_dir, settings=ChromaSettings(anonymized_telemetry=False)
    )


def _seed_legacy(persist_dir: str) -> dict:
    """模拟旧 schema：memory_entities 无 marker、384 维旧 space 向量。"""
    client = _client(persist_dir)
    legacy = client.create_collection("memory_entities")
    ids = ["old-1", "old-2"]
    documents = [f"Redis: {D1}", f"vLLM: {D2}"]
    metadatas = [
        {"id": "old-1", "name": "Redis", "type": "technology", "description": D1, "importance": 7.0},
        {"id": "old-2", "name": "vLLM", "type": "technology", "description": D2, "importance": 8.0},
    ]
    legacy.add(ids=ids, embeddings=[[0.1] * 384, [0.2] * 384], documents=documents, metadatas=metadatas)
    return {"ids": ids, "documents": documents, "metadatas": metadatas}


def test_plan_marks_legacy_for_migration(tmp_path):
    persist_dir = str(tmp_path / "chroma")
    _seed_legacy(persist_dir)
    plans = {p.name: p for p in plan_migration(_client(persist_dir), SemanticFakeEmbedder().identity)}
    assert plans["memory_entities"].action == "migrate"
    assert plans["memory_entities"].count == 2
    assert "legacy" in plans["memory_entities"].reason
    assert plans["memory_claims"].action == "skip_ok"  # 不存在


def test_migration_preserves_data_and_is_idempotent(tmp_path):
    persist_dir = str(tmp_path / "chroma")
    seeded = _seed_legacy(persist_dir)
    embedder = SemanticFakeEmbedder()

    report = run_migration(persist_dir, embedder, apply=True)
    result = next(r for r in report.results if r["collection"] == "memory_entities")
    assert result["action"] == "migrated" and result["verified"] is True

    # 数据保真 + marker 正确
    after = _client(persist_dir).get_collection("memory_entities")
    assert EmbeddingIdentity.from_markers(after.metadata) == embedder.identity
    got = after.get(include=["documents", "metadatas", "embeddings"])
    assert set(got["ids"]) == set(seeded["ids"])
    by_id = {i: (d, m, e) for i, d, m, e in zip(got["ids"], got["documents"], got["metadatas"], got["embeddings"])}
    for i, doc, meta in zip(seeded["ids"], seeded["documents"], seeded["metadatas"]):
        assert by_id[i][0] == doc
        assert by_id[i][1] == meta
        assert len(by_id[i][2]) == embedder.identity.dimension

    # 迁移后 store 可用且检索正确（语义 fake：查询与 D1 共享 heartbeat lease 词）
    store = StructuredMemoryStore(persist_dir=persist_dir, embedder=embedder)
    hits = store.search_entities("heartbeat lease", top_k=2)
    assert hits and hits[0].id == "old-1"

    # 幂等：第二次 plan 全部 skip
    plans = plan_migration(_client(persist_dir), embedder.identity)
    assert all(p.action == "skip_ok" for p in plans)


def test_run_migration_dry_run_does_not_touch_data(tmp_path):
    persist_dir = str(tmp_path / "chroma")
    _seed_legacy(persist_dir)
    embedder = SemanticFakeEmbedder()

    report = run_migration(persist_dir, embedder, apply=False)
    assert report.applied is False
    assert any(p.action == "migrate" for p in report.plans)
    # dry-run 不修改任何东西：collection 仍然无 marker、count 不变
    after = _client(persist_dir).get_collection("memory_entities")
    assert after.metadata is None and after.count() == 2


# ===== CLI 安全阀 =====


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "migrate_memory_schema_cli", REPO_ROOT / "scripts" / "migrate_memory_schema.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_refuses_fake_apply_without_allow_flag(tmp_path, capsys):
    persist_dir = str(tmp_path / "chroma")
    _seed_legacy(persist_dir)
    cli = _load_cli()  # conftest 已设 APP_ENV=test → fake embedding

    code = cli.main(["--apply", "--persist-dir", persist_dir])
    assert code == 3
    assert "fake embedding" in capsys.readouterr().err
    # 数据未被改动
    assert _client(persist_dir).get_collection("memory_entities").count() == 2


def test_cli_dry_run_exit_codes(tmp_path):
    persist_dir = str(tmp_path / "chroma")
    _seed_legacy(persist_dir)
    cli = _load_cli()

    assert cli.main(["--persist-dir", persist_dir]) == 2  # 有 collection 需要迁移
    # fake 模式下允许 dry-run 查看计划（--allow-fake 只影响 apply 安全阀）
    assert cli.main(["--persist-dir", persist_dir, "--allow-fake"]) == 2
