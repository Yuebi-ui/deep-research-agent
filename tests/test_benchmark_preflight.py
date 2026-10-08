"""Phase 3C P1：benchmark preflight self-check（Section 8 的 5/6/7 + 附加项）。

验收点：
- 全部一致 → passed
- 本地模型不符 → FAIL
- thinking 配置不符（读 worker 进程环境）→ FAIL
- embedding 配置不符 → FAIL
- worker 未运行 / 服务早于最新源码 → FAIL（后者可用 allow_stale_services 豁免）
- preflight 决议必须携带可审计的 observed/expected
"""

from __future__ import annotations

import os
import time

import pytest

from deep_research import llm
from deep_research.benchmark.fingerprint import thinking_snapshot
from deep_research.benchmark.preflight import Expectations, run_preflight

NOW = time.time()

_GOOD = {
    "api_health": {"status": "ok", "components": {"redis": "ok", "memory_schema": "ok"}},
    "redis_ping": True,
    "vllm_models": {"model": "qwen3-30b-a3b-local", "max_model_len": 8192, "base_url": "http://127.0.0.1:8001/v1"},
    "worker_process": {"pid": 111, "alive": True, "started_at": NOW, "env": {}},
    "api_process": {"pid": 222, "alive": True, "started_at": NOW, "env": {}},
    "embedding_identity": {"provider": "dashscope", "model": "text-embedding-v4", "dimension": 1024, "schema_version": 1},
    "memory_schema": [{"collection": "memory_entities", "status": "ok", "compatible": True}],
    "newest_source_mtime": NOW - 3600,
}


class FakeProbes:
    def __init__(self, **overrides):
        self._data = {**_GOOD, **overrides}

    def __getattr__(self, item):
        if item in self._data:
            value = self._data[item]
            return (lambda v=value: v) if not callable(value) else value
        raise AttributeError(item)


def _expectations(**kwargs) -> Expectations:
    base = dict(
        local_model="qwen3-30b-a3b-local",
        context_limit=8192,
        thinking={"claim_verify": False, "supervisor": True, "writer": False, "draft": False},
        embedding={"provider": "dashscope", "model": "text-embedding-v4", "dimension": 1024, "schema_version": 1},
    )
    base.update(kwargs)
    return Expectations(**base)


def test_preflight_passes_when_everything_matches():
    report = run_preflight(_expectations(), FakeProbes())
    assert report["passed"] is True, report["failed"]
    names = {c["name"] for c in report["checks"]}
    assert {"api_health", "redis", "vllm", "local_model", "context_limit",
            "thinking_policy", "embedding_identity", "memory_schema",
            "worker_process", "service_freshness"} <= names


def test_preflight_fails_on_wrong_local_model():
    report = run_preflight(_expectations(), FakeProbes(vllm_models={"model": "some-other-model", "max_model_len": 8192, "base_url": "x"}))
    assert report["passed"] is False
    assert "local_model" in report["failed"]


def test_preflight_fails_on_wrong_context_limit():
    report = run_preflight(_expectations(), FakeProbes(vllm_models={"model": "qwen3-30b-a3b-local", "max_model_len": 32768, "base_url": "x"}))
    assert report["passed"] is False and "context_limit" in report["failed"]


def test_preflight_fails_on_wrong_thinking_config():
    """worker 进程环境里 supervisor thinking=off（如实验后忘记恢复）→ FAIL。"""
    probes = FakeProbes(worker_process={"pid": 111, "alive": True, "started_at": NOW,
                                        "env": {"DR_SUPERVISOR_THINKING": "off"}})
    report = run_preflight(_expectations(), probes)
    assert report["passed"] is False and "thinking_policy" in report["failed"]
    check = next(c for c in report["checks"] if c["name"] == "thinking_policy")
    assert check["observed"]["supervisor"] is False


def test_preflight_fails_on_wrong_writer_thinking():
    """accepted 状态是 writer=off；worker 环境若残留 on（忘了重启）→ FAIL FAST。"""
    probes = FakeProbes(worker_process={"pid": 111, "alive": True, "started_at": NOW,
                                        "env": {"DR_WRITER_THINKING": "on"}})
    report = run_preflight(_expectations(), probes)  # 默认期望 writer=off
    assert report["passed"] is False and "thinking_policy" in report["failed"]
    check = next(c for c in report["checks"] if c["name"] == "thinking_policy")
    assert check["observed"]["writer"] is True and check["expected"]["writer"] is False


def test_worker_env_is_authoritative_over_runner_env(monkeypatch):
    """runner 环境正确但 worker 环境错误 → 仍必须 FAIL（历史事故：服务没重启）。"""
    monkeypatch.setenv("DR_SUPERVISOR_THINKING", "on")
    probes = FakeProbes(worker_process={"pid": 111, "alive": True, "started_at": NOW,
                                        "env": {"DR_SUPERVISOR_THINKING": "off"}})
    report = run_preflight(_expectations(), probes)
    assert report["passed"] is False and "thinking_policy" in report["failed"]


def test_preflight_fails_on_wrong_embedding_config():
    probes = FakeProbes(embedding_identity={"provider": "fake", "model": "dr-fake-embedding-v1", "dimension": 1024, "schema_version": 1})
    report = run_preflight(_expectations(), probes)
    assert report["passed"] is False and "embedding_identity" in report["failed"]


def test_preflight_fails_on_incompatible_memory_schema():
    probes = FakeProbes(memory_schema=[
        {"collection": "memory_entities", "status": "legacy_no_markers", "compatible": False},
        {"collection": "memory_claims", "status": "ok", "compatible": True},
    ])
    report = run_preflight(_expectations(), probes)
    assert report["passed"] is False and "memory_schema" in report["failed"]


def test_preflight_fails_on_dead_worker():
    probes = FakeProbes(worker_process={"pid": 111, "alive": False, "started_at": None, "env": None})
    report = run_preflight(_expectations(), probes)
    assert report["passed"] is False and "worker_process" in report["failed"]


def test_preflight_fails_on_stale_services_and_can_be_overridden():
    """worker 进程启动早于最新源码修改 → 默认 FAIL；显式豁免则通过。"""
    probes = FakeProbes(newest_source_mtime=NOW + 60)
    report = run_preflight(_expectations(), probes)
    assert report["passed"] is False and "service_freshness" in report["failed"]

    report = run_preflight(_expectations(allow_stale_services=True), probes)
    assert report["passed"] is True


def test_preflight_fails_when_api_unreachable():
    probes = FakeProbes(api_health={"status": "unreachable: connection refused", "components": {}})
    report = run_preflight(_expectations(), probes)
    assert report["passed"] is False and "api_health" in report["failed"]


def test_worker_feature_flags_are_captured():
    """feature flags 必须取 worker 进程环境（runner shell 常常没加载 .env.server）。"""
    probes = FakeProbes(worker_process={
        "pid": 111, "alive": True, "started_at": NOW,
        "env": {"DR_STREAM_USAGE": "1", "CHECKPOINTER_BACKEND": "redis"},
    })
    report = run_preflight(_expectations(), probes)
    flags = report["observations"]["worker_feature_flags"]
    assert flags["DR_STREAM_USAGE"] == "1" and flags["CHECKPOINTER_BACKEND"] == "redis"


def test_preflight_report_is_secret_free():
    import json

    report = run_preflight(_expectations(), FakeProbes())
    assert "sk-" not in json.dumps(report), "self-check 报告不得包含密钥"


def test_thinking_snapshot_matches_llm_module(monkeypatch):
    """fingerprint 的 thinking 解析必须与 deep_research.llm 的开关语义一致。"""
    for claim_value, sup_value, writer_value, draft_value in [
        ("on", "on", "on", "on"), ("off", "off", "off", "off"),
        ("0", "1", "1", "0"), ("false", "true", "no", "yes"),
    ]:
        monkeypatch.setenv("DR_CLAIM_VERIFY_THINKING", claim_value)
        monkeypatch.setenv("DR_SUPERVISOR_THINKING", sup_value)
        monkeypatch.setenv("DR_WRITER_THINKING", writer_value)
        monkeypatch.setenv("DR_DRAFT_THINKING", draft_value)
        assert thinking_snapshot(os.environ) == {
            "claim_verify": llm.claim_verify_thinking(),
            "supervisor": llm.supervisor_thinking(),
            "writer": llm.writer_thinking(),
            "draft": llm.draft_thinking(),
        }
    for var in ("DR_CLAIM_VERIFY_THINKING", "DR_SUPERVISOR_THINKING", "DR_WRITER_THINKING", "DR_DRAFT_THINKING"):
        monkeypatch.delenv(var, raising=False)
    assert thinking_snapshot(os.environ) == {
        "claim_verify": False, "supervisor": True, "writer": False, "draft": False,
    }
