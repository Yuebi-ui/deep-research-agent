"""Phase 3C-2 / E8：final writer thinking 开关的端到端接线测试。

必须证明的不是 "config helper 返回 False"，而是：

1. 开关值确实进入 **请求构造参数**（extra_body.enable_thinking）——
   防 Phase 3B "thinking 参数未接入 _build_kwargs" 类回归；
2. `agent_builder` 的 import-time 构造确实把开关传给了 writer model；
3. writer role 的**另外两个消费者不受影响**（单变量实验的核心前提）：
   tools/tool.py 的 refine_draft_report、get_chat_model_auto 的 draft 路由。
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

import deep_research.agent_builder as agent_builder
import deep_research.llm as llm_mod


# ===== 1. 开关语义 =====


def test_writer_thinking_env_semantics(monkeypatch):
    from deep_research.llm import writer_thinking

    monkeypatch.delenv("DR_WRITER_THINKING", raising=False)
    assert writer_thinking() is False, "E8 KEEP（Phase 3C-2）后默认翻转为 off"

    for value in ("off", "0", "false", "no", "OFF"):
        monkeypatch.setenv("DR_WRITER_THINKING", value)
        assert writer_thinking() is False

    for value in ("on", "1", "true", "yes"):
        monkeypatch.setenv("DR_WRITER_THINKING", value)
        assert writer_thinking() is True, "显式 on 必须可恢复旧行为（开关不得被删除）"


def test_accepted_thinking_policy_is_consistent(monkeypatch):
    """accepted state：extractor OFF / judge OFF / supervisor ON / writer OFF。

    red_team 无开关（provider 默认 = ON，由调用点断言覆盖）；draft 见 tests/test_draft_thinking.py。
    """
    from deep_research import llm

    for var in ("DR_CLAIM_VERIFY_THINKING", "DR_SUPERVISOR_THINKING", "DR_WRITER_THINKING"):
        monkeypatch.delenv(var, raising=False)
    assert llm.claim_verify_thinking() is False
    assert llm.supervisor_thinking() is True
    assert llm.writer_thinking() is False


# ===== 2. 真实请求参数（capture 模式，复用 test_local_provider 的做法）=====


def _capture_writer_request_kwargs(monkeypatch) -> dict:
    cfg = {
        "roles": {"writer": {"backend": "openai", "handle": "deepseek-v4-pro"}},
        "cognition": {"openai": {"base_url": "http://x", "api_key": "k"}},
    }
    monkeypatch.setattr(llm_mod, "_load_stage_config", lambda *a, **k: cfg)

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
    return captured


def test_writer_thinking_off_lands_in_request_kwargs(monkeypatch):
    captured = _capture_writer_request_kwargs(monkeypatch)
    monkeypatch.delenv("DR_WRITER_THINKING", raising=False)  # 默认即 off

    llm_mod.get_chat_model("writer", thinking=llm_mod.writer_thinking())
    assert captured["extra_body"]["enable_thinking"] is False, (
        "默认（off）必须落到 writer 请求的 extra_body"
    )
    assert captured["model"] == "deepseek-v4-pro", "变量只能是 thinking，不得换模型"


def test_writer_thinking_explicit_override_on_reaches_request(monkeypatch):
    """显式 override 仍可恢复 thinking ON（开关能力不得被删除）。

    thinking=on 不注入任何字段 → 请求体与 E8 A 组/历史基线逐字节一致。
    """
    captured = _capture_writer_request_kwargs(monkeypatch)
    monkeypatch.setenv("DR_WRITER_THINKING", "on")

    llm_mod.get_chat_model("writer", thinking=llm_mod.writer_thinking())
    assert "enable_thinking" not in (captured.get("extra_body") or {})


# ===== 3. import-time 构造接线 =====


def _spy_get_chat_model(calls: list[tuple]) -> object:
    class _Dummy:
        def __getattr__(self, item):  # 模型实例只需可赋值给模块变量
            raise AttributeError(item)

    def spy(role, **kwargs):
        calls.append((role, kwargs))
        return _Dummy()

    return spy


def test_agent_builder_import_time_wiring(monkeypatch):
    """agent_builder 模块级 writer_model 必须按开关构造（import 期）。

    回归背景：模型在 import 期构造，开关若只在运行时读，永远不会生效；
    Phase 3B 也发生过「参数进了签名但没进调用链」的事故。
    """
    calls: list[tuple] = []

    with monkeypatch.context() as m:
        m.setenv("DR_WRITER_THINKING", "off")
        m.setattr(llm_mod, "get_chat_model", _spy_get_chat_model(calls))
        importlib.reload(agent_builder)

    assert calls, "reload 必须触发 writer model 构造"
    role, kwargs = calls[-1]
    assert role == "writer"
    assert kwargs.get("thinking") is False, f"DR_WRITER_THINKING=off 必须以 thinking=False 构造，得到 {kwargs}"

    importlib.reload(agent_builder)  # 恢复（用真实 llm 函数重建 writer_model）

    with monkeypatch.context() as m:
        m.delenv("DR_WRITER_THINKING", raising=False)
        calls.clear()
        m.setattr(llm_mod, "get_chat_model", _spy_get_chat_model(calls))
        importlib.reload(agent_builder)

    role, kwargs = calls[-1]
    assert role == "writer" and kwargs.get("thinking") is False, (
        "E8 KEEP 后默认必须 thinking=False"
    )

    with monkeypatch.context() as m:
        m.setenv("DR_WRITER_THINKING", "on")
        calls.clear()
        m.setattr(llm_mod, "get_chat_model", _spy_get_chat_model(calls))
        importlib.reload(agent_builder)

    role, kwargs = calls[-1]
    assert role == "writer" and kwargs.get("thinking") is True, "显式 override 必须可恢复 ON"

    importlib.reload(agent_builder)


def test_other_writer_role_consumers_are_not_switched(monkeypatch):
    """单变量前提：refine_draft_report（tools/tool.py）不接 writer 开关。"""
    import deep_research.tools.tool as tool_mod

    calls: list[tuple] = []
    with monkeypatch.context() as m:
        m.setenv("DR_WRITER_THINKING", "off")
        m.setattr(llm_mod, "get_chat_model", _spy_get_chat_model(calls))
        importlib.reload(tool_mod)

    writer_calls = [kw for role, kw in calls if role == "writer"]
    assert writer_calls, "tools/tool.py 应构造 writer role（refine_draft_report）"
    assert all("thinking" not in kw for kw in writer_calls), (
        "refine_draft_report 的 writer 调用点不得受 DR_WRITER_THINKING 影响"
    )

    importlib.reload(tool_mod)  # 恢复


def test_get_chat_model_auto_writer_routing_is_not_switched(monkeypatch):
    """单变量前提：长 query 的 draft 路由（会自动选 writer role）不带 thinking 参数。"""
    captured: list[tuple] = []

    def spy(role, **kwargs):
        captured.append((role, kwargs))

        class _Dummy:
            pass

        return _Dummy()

    monkeypatch.setattr(llm_mod, "get_chat_model", spy)
    monkeypatch.setenv("DR_WRITER_THINKING", "off")
    llm_mod.get_chat_model_auto("draft", query_text="x" * 200)

    assert captured and captured[-1][0] == "writer", "长 query 应路由到 writer role"
    assert "thinking" not in captured[-1][1], "auto 路由不得被 writer 开关污染"


# ===== 4. benchmark 侧联动（指纹 + preflight）=====


def test_writer_thinking_changes_config_fingerprint(monkeypatch):
    from deep_research.benchmark.fingerprint import (
        build_config_snapshot,
        config_fingerprint,
        thinking_snapshot,
    )

    base_env = {"DR_CLAIM_VERIFY_THINKING": "off", "DR_SUPERVISOR_THINKING": "on"}
    on_env = {**base_env, "DR_WRITER_THINKING": "on"}
    off_env = {**base_env, "DR_WRITER_THINKING": "off"}

    assert thinking_snapshot(on_env)["writer"] is True
    assert thinking_snapshot(off_env)["writer"] is False
    assert thinking_snapshot(base_env)["writer"] is False, "缺省 = off（E8 KEEP）"

    cfg = {"roles": {"writer": {"backend": "openai", "handle": "deepseek-v4-pro"}}}
    fp_on = config_fingerprint(build_config_snapshot(cfg, env=on_env))
    fp_off = config_fingerprint(build_config_snapshot(cfg, env=off_env))
    assert fp_on != fp_off, "writer thinking 变化必须改变 config fingerprint（A/B 可区分）"


@pytest.mark.parametrize("bad", ["writer=turbo", "unknown=on", "writer"])
def test_expect_thinking_flag_validation(tmp_path, bad):
    import importlib.util
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("run_baseline_cli_t", repo_root / "scripts" / "run_baseline.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    import argparse

    args = argparse.Namespace(
        expect_thinking=[bad], expect_claim_verify_thinking=None, expect_supervisor_thinking=None
    )
    with pytest.raises(ValueError):
        runner._parse_thinking_expectations(args)


def test_expect_thinking_flag_merges_with_defaults():
    import argparse
    import importlib.util
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("run_baseline_cli_m", repo_root / "scripts" / "run_baseline.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    args = argparse.Namespace(
        expect_thinking=["writer=off"], expect_claim_verify_thinking=None, expect_supervisor_thinking=None
    )
    assert runner._parse_thinking_expectations(args) == {
        "claim_verify": False, "supervisor": True, "writer": False, "draft": False,
    }
