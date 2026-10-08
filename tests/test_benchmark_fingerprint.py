"""Phase 3C P1：run fingerprint / config fingerprint / experiment identity / freeze。

对应验收清单（Section 8，1–4 + 8–9）：
1. run fingerprint deterministic
2. secrets 不进入 fingerprint artifact
3. config change → fingerprint changes
4. source revision change → revision changes
8. code changed during benchmark → run invalid
9. experiment_id / variant 被正确记入 run artifact
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from deep_research.benchmark.fingerprint import (
    ExperimentIdentity,
    build_config_snapshot,
    collect_code_revision,
    config_fingerprint,
    evaluate_run_validity,
    scrub_secrets,
    source_fingerprint,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

_BASE_STAGE_CFG = {
    "cognition": {
        "openai": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key": "EXAMPLE_API_KEY_ONE"},
        "openai_local": {"base_url": "http://127.0.0.1:8001/v1"},
    },
    "roles": {
        "supervisor": {"backend": "openai", "handle": "deepseek-v4-pro"},
        "researcher_main": {"backend": "openai_local", "handle": "qwen3-30b-a3b-local"},
    },
    "context_budget": {},
}

_FAKE_IDENTITY = {"provider": "dashscope", "model": "text-embedding-v4", "dimension": 1024, "schema_version": 1}


def _snapshot(cfg=_BASE_STAGE_CFG, env=None):
    return build_config_snapshot(cfg, env=env or {}, embedding=_FAKE_IDENTITY)


# ===== 1. deterministic =====


def test_config_fingerprint_is_deterministic():
    assert config_fingerprint(_snapshot()) == config_fingerprint(_snapshot())
    # 键序不同不影响
    reordered = dict(reversed(list(_BASE_STAGE_CFG.items())))
    assert config_fingerprint(_snapshot(reordered)) == config_fingerprint(_snapshot())


def test_source_fingerprint_is_deterministic_and_path_relative(tmp_path):
    def make_tree(base: Path) -> None:
        (base / "deep_research").mkdir(parents=True)
        (base / "deep_research" / "a.py").write_text("x = 1\n")
        (base / "backend").mkdir()
        (base / "backend" / "b.py").write_text("y = 2\n")

    tree_a, tree_b = tmp_path / "a", tmp_path / "b"
    make_tree(tree_a)
    make_tree(tree_b)
    fp_a, n_a = source_fingerprint(tree_a)
    fp_b, n_b = source_fingerprint(tree_b)
    assert fp_a == fp_b and n_a == n_b == 2, "相同内容、不同绝对路径 → 同一指纹"
    assert source_fingerprint(tree_a)[0] == fp_a, "重复计算必须一致"


# ===== 2. secrets =====


def test_secrets_never_enter_fingerprint_artifact():
    snapshot = _snapshot()
    serialized = json.dumps(snapshot)
    assert "EXAMPLE_API_KEY" not in serialized, "API key 不得出现在指纹快照里"
    # 命中敏感命名的键被替换
    assert scrub_secrets({"api_key": "x", "nested": {"TOKEN": "y"}}) == {
        "api_key": "<redacted>",
        "nested": {"TOKEN": "<redacted>"},
    }
    assert scrub_secrets({"note": "leaked <REDACTED> here"})["note"] == "leaked <redacted> here"


def test_scrubber_matches_credential_keys_without_misfiring_on_token_counts():
    """`token` 作为凭据 vs `tokens` 作为计数（max_tokens/reserve_output_tokens）。"""
    assert scrub_secrets({
        "api_key": "x", "access_token": "y", "TOKEN": "z", "db_password": "p", "credentials": "c",
    }) == {
        "api_key": "<redacted>", "access_token": "<redacted>", "TOKEN": "<redacted>",
        "db_password": "<redacted>", "credentials": "<redacted>",
    }
    kept = scrub_secrets({
        "max_tokens": 4096, "reserve_output_tokens": 1024, "safety_margin_tokens": 256,
    })
    assert kept == {"max_tokens": 4096, "reserve_output_tokens": 1024, "safety_margin_tokens": 256}


def test_role_max_tokens_is_part_of_fingerprint():
    """max_tokens 是真实影响 benchmark 的配置，不得被 scrub 丢掉。"""
    changed = json.loads(json.dumps(_BASE_STAGE_CFG))
    changed["roles"]["supervisor"] = {"backend": "openai", "handle": "deepseek-v4-pro", "max_tokens": 2048}
    assert config_fingerprint(_snapshot(changed)) != config_fingerprint(_snapshot())


def test_rotating_api_key_does_not_change_config_fingerprint():
    rotated = json.loads(json.dumps(_BASE_STAGE_CFG))
    rotated["cognition"]["openai"]["api_key"] = "EXAMPLE_API_KEY_TWO"
    assert config_fingerprint(_snapshot(rotated)) == config_fingerprint(_snapshot())


# ===== 3. config change =====


def test_config_change_changes_fingerprint():
    changed_role = json.loads(json.dumps(_BASE_STAGE_CFG))
    changed_role["roles"]["supervisor"]["handle"] = "deepseek-v4-flash"
    assert config_fingerprint(_snapshot(changed_role)) != config_fingerprint(_snapshot())

    changed_budget = json.loads(json.dumps(_BASE_STAGE_CFG))
    changed_budget["context_budget"] = {"reserve_output_tokens": 2048}
    assert config_fingerprint(_snapshot(changed_budget)) != config_fingerprint(_snapshot())

    changed_flag = _snapshot(env={"DR_SUPERVISOR_THINKING": "off"})
    assert config_fingerprint(changed_flag) != config_fingerprint(_snapshot())

    changed_embedding = build_config_snapshot(
        _BASE_STAGE_CFG, env={}, embedding={**_FAKE_IDENTITY, "model": "text-embedding-v3"}
    )
    assert config_fingerprint(changed_embedding) != config_fingerprint(_snapshot())


# ===== 4. source revision =====


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def tiny_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "deep_research").mkdir(parents=True)
    (repo / "deep_research" / "mod.py").write_text("VALUE = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return repo


def test_source_revision_changes_with_code(tiny_repo):
    first = collect_code_revision(tiny_repo)
    assert first["git_available"] and first["dirty"] is False

    (tiny_repo / "deep_research" / "mod.py").write_text("VALUE = 2\n")
    second = collect_code_revision(tiny_repo)
    assert second["dirty"] is True, "未提交修改必须被标记"
    assert second["source_fingerprint"] != first["source_fingerprint"]
    assert second["commit"] == first["commit"], "未提交修改不改变 commit"

    _git(tiny_repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(tiny_repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "change")
    third = collect_code_revision(tiny_repo)
    assert third["commit"] != first["commit"] and third["dirty"] is False


# ===== 8. freeze =====


def test_run_validity_detects_code_and_config_change():
    start = {"commit": "aaa", "source_fingerprint": "fp1"}
    same = {"commit": "aaa", "source_fingerprint": "fp1"}

    assert evaluate_run_validity(
        revision_start=start, revision_end=same,
        config_fingerprint_start="cfg1", config_fingerprint_end="cfg1",
    )["validity"] == "VALID"

    code_changed = evaluate_run_validity(
        revision_start=start, revision_end={"commit": "bbb", "source_fingerprint": "fp1"},
        config_fingerprint_start="cfg1", config_fingerprint_end="cfg1",
    )
    assert code_changed["validity"] == "INVALID_CODE_CHANGED"

    # dirty 工作树里编辑源码（commit 不变、指纹变）同样必须判 INVALID
    dirty_edit = evaluate_run_validity(
        revision_start=start, revision_end={"commit": "aaa", "source_fingerprint": "fp2"},
        config_fingerprint_start="cfg1", config_fingerprint_end="cfg1",
    )
    assert dirty_edit["validity"] == "INVALID_CODE_CHANGED"

    config_changed = evaluate_run_validity(
        revision_start=start, revision_end=same,
        config_fingerprint_start="cfg1", config_fingerprint_end="cfg2",
    )
    assert config_changed["validity"] == "INVALID_CONFIG"

    both = evaluate_run_validity(
        revision_start=start, revision_end={"commit": "bbb", "source_fingerprint": "fp2"},
        config_fingerprint_start="cfg1", config_fingerprint_end="cfg2",
    )
    assert both["validity"] == "INVALID_CODE_CHANGED+INVALID_CONFIG"


# ===== 9. experiment identity =====


def test_experiment_identity_validation():
    identity = ExperimentIdentity(experiment_id="writer-thinking-001", variant="thinking-on", kind="experiment")
    assert identity.to_dict() == {
        "experiment_id": "writer-thinking-001", "variant": "thinking-on", "kind": "experiment",
    }
    with pytest.raises(ValueError):
        ExperimentIdentity(experiment_id="", variant="x")
    with pytest.raises(ValueError):
        ExperimentIdentity(experiment_id="x", variant="x", kind="whatever")


class _StubProbes:
    """只实现 runner 指纹需要的接口：.worker_process() → 原始进程信息（含 env）。"""

    def __init__(self, worker):
        self._worker = worker

    def worker_process(self):
        return self._worker


def test_runner_config_snapshot_prefers_worker_process_env(tmp_path):
    """runner 的 config fingerprint 以 worker 进程环境覆盖同名 runner 变量。"""
    spec = importlib.util.spec_from_file_location("run_baseline_cli_env", REPO_ROOT / "scripts" / "run_baseline.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    with_worker = runner._build_config_snapshot(_StubProbes(
        {"pid": 1, "env": {"DR_STREAM_USAGE": "1", "CHECKPOINTER_BACKEND": "redis", "DR_WRITER_THINKING": "off"}}
    ))
    assert with_worker["feature_flags"]["DR_STREAM_USAGE"] == "1"
    assert with_worker["feature_flags_source"] == "worker_env+runner_env"
    assert with_worker["thinking"]["writer"] is False, (
        "worker 的 DR_WRITER_THINKING 必须进入指纹——否则 A/B 两个 variant 的 config_fp 相同"
    )

    without_worker = runner._build_config_snapshot(_StubProbes(None))
    assert without_worker["feature_flags_source"] == "runner_env"


def test_runner_snapshot_rejects_stripped_observation_dict():
    """回归：preflight 的 worker observation（已剥离 env）不得被当作指纹输入。

    历史 bug：runner 从 preflight observations 里取 worker 信息 → env 已被
    _process_summary 剥离 → 静默退化为 runner_env。
    """
    spec = importlib.util.spec_from_file_location("run_baseline_cli_strip", REPO_ROOT / "scripts" / "run_baseline.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    stripped_observation = {"pid": 1, "alive": True, "env_keys": ["DR_WRITER_THINKING"]}  # 无 "env" 值

    class _ObservationsProbes:
        def worker_process(self):
            return stripped_observation

    snapshot = runner._build_config_snapshot(_ObservationsProbes())
    assert snapshot["feature_flags_source"] == "runner_env", "剥离后的 observation 只能退化为 runner_env"

    # 而真正的 probe（含 env 值）必须被采用 —— 二者的区别即本测试守住的边界
    real = runner._build_config_snapshot(_StubProbes({"pid": 1, "env": {"DR_WRITER_THINKING": "off"}}))
    assert real["feature_flags_source"] == "worker_env+runner_env"
    assert real["thinking"]["writer"] is False


def test_experiment_identity_lands_in_run_artifact(tmp_path):
    """experiment_id / variant / kind 必须进入 run.json（含 integrity 判定）。"""
    spec = importlib.util.spec_from_file_location("run_baseline_cli", REPO_ROOT / "scripts" / "run_baseline.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    experiment = ExperimentIdentity(experiment_id="phase3c-smoke-001", variant="post-p0", kind="smoke")
    rows = {k: [] for k in ("llm_calls", "node_metrics", "search_metrics", "budget_events", "reliability_events")}
    out_dir = tmp_path / "run-out"

    runner.write_artifacts(
        out_dir, "run-xyz",
        {"thread_id": "t", "status": "completed", "attempt": 1},
        rows,
        {"gpu": "fake"}, {"case_id": "c", "query": "q", "variant": "post-p0"},
        [],
        extra={
            "experiment": experiment.to_dict(),
            "integrity": {"validity": "VALID", "reasons": []},
        },
    )
    run_json = json.loads((out_dir / "run.json").read_text())
    assert run_json["experiment"] == {
        "experiment_id": "phase3c-smoke-001", "variant": "post-p0", "kind": "smoke",
    }
    assert run_json["integrity"]["validity"] == "VALID"
