"""Local Provider（本地 vLLM）接入的离线验证。

覆盖两层：

1. ``config.hybrid.example.yml`` 的结构正确性
   （角色 → backend/handle、base_url、模板不含真实密钥）；
2. ``deep_research/llm.py`` 中 ``openai_local`` backend 的构建行为，
   以及 ``APP_ENV=test`` 下 Fake 短路不被破坏（本轮不得访问
   localhost:8001 —— 当前机器没有 GPU / vLLM 服务）。

全部测试不发起任何网络连接。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
HYBRID_CONFIG = REPO_ROOT / "config.hybrid.example.yml"

# 代码中实际通过 get_chat_model* 调用的角色（grep 所得）+ context_pruner（清单保留）
CODE_ROLES = {
    "supervisor",
    "writer",
    "evaluator",
    "red_team",
    "draft",
    "researcher_main",
    "researcher_compressor",
    "researcher_summarizer",
}
LOCAL_ROLES = {
    "researcher_main",
    "researcher_compressor",
    "researcher_summarizer",
    "context_pruner",
}
CLOUD_ROLES = {"supervisor", "writer", "evaluator", "red_team", "draft"}


@pytest.fixture(scope="module")
def hybrid_cfg() -> dict:
    data = yaml.safe_load(HYBRID_CONFIG.read_text(encoding="utf8"))
    return data["stages"]["prod"]


# ===== 1. Hybrid 配置结构 =====


def test_hybrid_config_covers_all_code_roles(hybrid_cfg):
    missing = CODE_ROLES - set(hybrid_cfg["roles"])
    assert not missing, f"config 缺少代码使用的角色: {sorted(missing)}"


def test_every_role_resolves_to_known_backend(hybrid_cfg):
    cognition = hybrid_cfg["cognition"]
    for role, cfg in hybrid_cfg["roles"].items():
        assert cfg.get("backend") in cognition, f"角色 {role} 指向未知 backend"
        assert cfg.get("handle"), f"角色 {role} 缺少 handle"


def test_local_roles_use_openai_local(hybrid_cfg):
    roles = hybrid_cfg["roles"]
    for role in LOCAL_ROLES:
        assert roles[role] == {
            "backend": "openai_local",
            "handle": "qwen3-30b-a3b-local",
        }, f"角色 {role} 的本地映射不符"
    assert (
        hybrid_cfg["cognition"]["openai_local"]["base_url"]
        == "http://127.0.0.1:8001/v1"
    )


def test_cloud_roles_use_dashscope(hybrid_cfg):
    roles = hybrid_cfg["roles"]
    for role in CLOUD_ROLES:
        assert roles[role]["backend"] == "openai", f"角色 {role} 应保持云端"
    assert (
        hybrid_cfg["cognition"]["openai"]["base_url"]
        == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )


def test_local_api_key_is_placeholder_not_secret(hybrid_cfg):
    key = hybrid_cfg["cognition"]["openai_local"]["api_key"]
    assert key == "EMPTY"
    assert not key.startswith("sk-")


def test_example_config_contains_no_real_secrets(hybrid_cfg):
    """模板必须可安全提交：两个真实 provider 的 key 均应为空。"""
    assert hybrid_cfg["cognition"]["openai"]["api_key"] == ""
    assert hybrid_cfg["search"]["tavily"]["api_key"] == ""


# ===== 2. llm.py 构建行为 =====


def test_build_kwargs_routes_openai_local():
    from deep_research.llm import _build_kwargs

    api_cfg = {
        "base_url": "http://127.0.0.1:8001/v1",
        "api_key": "EMPTY",
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    kwargs = _build_kwargs(
        backend="openai_local",
        handle="qwen3-30b-a3b-local",
        api_cfg=api_cfg,
        role_cfg={},
        max_tokens=2048,
        timeout_seconds=30,
    )
    assert kwargs["model"] == "qwen3-30b-a3b-local"
    assert kwargs["model_provider"] == "openai"
    assert kwargs["base_url"] == "http://127.0.0.1:8001/v1"
    assert kwargs["api_key"] == "EMPTY"
    assert kwargs["max_tokens"] == 2048
    assert kwargs["timeout"] == 30
    assert kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_build_kwargs_qwen32b_thinking_still_disabled():
    """既有 qwen3-32b 的特殊处理必须保持。"""
    from deep_research.llm import _build_kwargs

    kwargs = _build_kwargs(
        backend="openai",
        handle="qwen3-32b",
        api_cfg={},
        role_cfg={},
        max_tokens=None,
        timeout_seconds=None,
    )
    assert kwargs["extra_body"] == {"enable_thinking": False}


def test_build_kwargs_config_extra_body_merges():
    from deep_research.llm import _build_kwargs

    kwargs = _build_kwargs(
        backend="openai",
        handle="qwen3-32b",
        api_cfg={"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
        role_cfg={},
        max_tokens=None,
        timeout_seconds=None,
    )
    assert kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False},
        "enable_thinking": False,
    }


def test_build_kwargs_rejects_unknown_backend():
    from deep_research.llm import LLMConfigError, _build_kwargs

    with pytest.raises(LLMConfigError):
        _build_kwargs(
            backend="not_a_backend",
            handle="x",
            api_cfg={},
            role_cfg={},
            max_tokens=None,
            timeout_seconds=None,
        )


def test_fake_short_circuit_covers_local_roles():
    """APP_ENV=test 下本地角色仍然短路到 Fake —— 不构造真实 client。"""
    from deep_research.llm import get_chat_model
    from deep_research.testing.fake_llm import FakeChatModel

    for role in ("researcher_main", "researcher_compressor", "researcher_summarizer"):
        assert isinstance(get_chat_model(role), FakeChatModel)


# ===== E1a：thinking 参数端到端接线（防回归）=====


def test_get_chat_model_threads_thinking_into_request_kwargs(monkeypatch):
    """get_chat_model(thinking=False) 必须最终进入 extra_body.enable_thinking。

    回归背景：Phase 3B 首版把 `thinking` 收进了函数签名却忘了传给
    `_build_kwargs`，导致开关静默失效（微实验两次调用实际同参）。
    """
    from types import SimpleNamespace

    import deep_research.llm as llm_mod

    cfg = {
        "roles": {"writer": {"backend": "openai", "handle": "deepseek-v4-pro"}},
        "cognition": {"openai": {"base_url": "http://x", "api_key": "k"}},
    }
    monkeypatch.setattr(llm_mod, "_load_stage_config", lambda *a, **k: cfg)

    # 兼容 conftest teardown：reset_engine_settings() 会调用本函数的
    # cache_clear（autouse fixture 的 teardown 可能早于 monkeypatch 撤销）
    def _fake_settings():
        return SimpleNamespace(use_fake_llm=False, resolved_config_path="cfg.yml")

    _fake_settings.cache_clear = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr("deep_research.settings.get_engine_settings", _fake_settings)

    captured: dict = {}

    class _Dummy:
        def with_config(self, config):
            return self

    def fake_init(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return _Dummy()

    monkeypatch.setattr(llm_mod, "init_chat_model", fake_init)

    llm_mod.get_chat_model("writer", thinking=False)
    assert captured["extra_body"]["enable_thinking"] is False, "thinking=False 必须落到请求参数"

    llm_mod.get_chat_model("writer", thinking=True)
    assert "enable_thinking" not in (captured.get("extra_body") or {}), "thinking=True 不得注入关闭参数"

    llm_mod.get_chat_model("writer")
    assert "enable_thinking" not in (captured.get("extra_body") or {}), "默认不得干预"


def test_claim_verify_thinking_env_semantics(monkeypatch):
    from deep_research.llm import claim_verify_thinking

    # E1a KEEP（2026-10-06）：默认翻转为 off；显式 on 可恢复旧行为
    monkeypatch.delenv("DR_CLAIM_VERIFY_THINKING", raising=False)
    assert claim_verify_thinking() is False
    for value in ("off", "0", "false", "no"):
        monkeypatch.setenv("DR_CLAIM_VERIFY_THINKING", value)
        assert claim_verify_thinking() is False
    monkeypatch.setenv("DR_CLAIM_VERIFY_THINKING", "on")
    assert claim_verify_thinking() is True


def test_supervisor_thinking_env_semantics(monkeypatch):
    from deep_research.llm import supervisor_thinking

    monkeypatch.delenv("DR_SUPERVISOR_THINKING", raising=False)
    assert supervisor_thinking() is True  # E1b 实验前默认 on = 历史行为
    for value in ("off", "0", "false", "no"):
        monkeypatch.setenv("DR_SUPERVISOR_THINKING", value)
        assert supervisor_thinking() is False
