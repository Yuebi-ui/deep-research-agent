"""Phase 3C-3 / E9：draft thinking 开关的端到端接线测试。

必须证明（与 E8 同一标准）：

1. 开关值进入 **真实请求构造参数**（extra_body.enable_thinking）；
2. `draft_agent` 的 import-time 构造按开关执行；
3. 单变量不变量：开关**只影响 logical role = draft 的唯一调用点**，不得
   泄漏到 write_research_brief 的 auto 路由、refine_draft_report、final writer；
4. benchmark 侧：config fingerprint 记录、preflight 可验证。
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace


import deep_research.llm as llm_mod
from deep_research.llm import draft_thinking


# ===== 1. 开关语义 =====


def test_draft_thinking_env_semantics(monkeypatch):
    monkeypatch.delenv("DR_DRAFT_THINKING", raising=False)
    assert draft_thinking() is False, "E9 KEEP（Phase 3C-3）后默认翻转为 off"

    for value in ("off", "0", "false", "no", "OFF"):
        monkeypatch.setenv("DR_DRAFT_THINKING", value)
        assert draft_thinking() is False

    for value in ("on", "1", "true", "yes"):
        monkeypatch.setenv("DR_DRAFT_THINKING", value)
        assert draft_thinking() is True, "显式 on 必须可恢复旧行为（开关不得被删除）"


# ===== 2. 真实请求参数 =====


def _capture_kwargs(monkeypatch) -> dict:
    cfg = {
        "roles": {
            "draft": {"backend": "openai", "handle": "deepseek-v4-pro"},
            "writer": {"backend": "openai", "handle": "deepseek-v4-pro"},
            "evaluator": {"backend": "openai", "handle": "deepseek-v4-flash"},
        },
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


def test_draft_thinking_off_lands_in_request_kwargs(monkeypatch):
    captured = _capture_kwargs(monkeypatch)
    monkeypatch.setenv("DR_DRAFT_THINKING", "off")

    llm_mod.get_chat_model("draft", thinking=draft_thinking())
    assert captured["extra_body"]["enable_thinking"] is False, (
        "DR_DRAFT_THINKING=off 必须落到 draft 请求的 extra_body"
    )
    assert captured["model"] == "deepseek-v4-pro", "唯一变量只能是 thinking"


def test_default_draft_request_receives_off(monkeypatch):
    """默认（off）必须落到真实请求参数——防止开关静默失效。"""
    captured = _capture_kwargs(monkeypatch)
    monkeypatch.delenv("DR_DRAFT_THINKING", raising=False)

    llm_mod.get_chat_model("draft", thinking=draft_thinking())
    assert captured["extra_body"]["enable_thinking"] is False


def test_draft_thinking_explicit_on_leaves_request_untouched(monkeypatch):
    captured = _capture_kwargs(monkeypatch)
    monkeypatch.setenv("DR_DRAFT_THINKING", "on")

    llm_mod.get_chat_model("draft", thinking=draft_thinking())
    assert "enable_thinking" not in (captured.get("extra_body") or {}), (
        "显式 on 必须保持 provider 默认（override 后请求体与历史行为一致）"
    )


# ===== 3. import-time 接线 =====


def _spy(calls: list[tuple]):
    class _Dummy:
        def __getattr__(self, item):
            raise AttributeError(item)

    def spy(role, **kwargs):
        calls.append((role, kwargs))
        return _Dummy()

    return spy


def test_draft_agent_import_time_wiring(monkeypatch):
    import deep_research.agents.draft_agent as draft_agent

    calls: list[tuple] = []
    with monkeypatch.context() as m:
        m.setenv("DR_DRAFT_THINKING", "off")
        m.setattr(llm_mod, "get_chat_model", _spy(calls))
        importlib.reload(draft_agent)
    role, kwargs = calls[-1]
    assert role == "draft" and kwargs.get("thinking") is False, f"off 必须以 thinking=False 构造，得到 {kwargs}"

    importlib.reload(draft_agent)  # 恢复

    with monkeypatch.context() as m:
        m.delenv("DR_DRAFT_THINKING", raising=False)
        calls.clear()
        m.setattr(llm_mod, "get_chat_model", _spy(calls))
        importlib.reload(draft_agent)
    role, kwargs = calls[-1]
    assert role == "draft" and kwargs.get("thinking") is False, "E9 KEEP 后默认必须 thinking=False"

    importlib.reload(draft_agent)  # 恢复

    with monkeypatch.context() as m:
        m.setenv("DR_DRAFT_THINKING", "on")
        calls.clear()
        m.setattr(llm_mod, "get_chat_model", _spy(calls))
        importlib.reload(draft_agent)
    role, kwargs = calls[-1]
    assert role == "draft" and kwargs.get("thinking") is True, "显式 override 必须可恢复 ON"

    importlib.reload(draft_agent)  # 恢复


# ===== 4. 单变量不变量 =====


def test_draft_switch_does_not_leak_to_writer(monkeypatch):
    """DR_DRAFT_THINKING=off 不得改变 final writer 的构造（反之亦然）。"""
    import deep_research.agent_builder as agent_builder

    calls: list[tuple] = []
    with monkeypatch.context() as m:
        m.setenv("DR_DRAFT_THINKING", "off")
        m.setenv("DR_WRITER_THINKING", "on")  # writer 显式 on
        m.setattr(llm_mod, "get_chat_model", _spy(calls))
        importlib.reload(agent_builder)
    role, kwargs = calls[-1]
    assert role == "writer" and kwargs.get("thinking") is True, (
        "draft 开关泄漏到了 writer！"
    )
    importlib.reload(agent_builder)  # 恢复


def test_draft_switch_does_not_leak_to_auto_routing_or_refine(monkeypatch):
    import deep_research.tools.tool as tool_mod

    monkeypatch.setenv("DR_DRAFT_THINKING", "off")
    captured: list[tuple] = []

    def spy(role, **kwargs):
        captured.append((role, kwargs))

        class _Dummy:
            pass

        return _Dummy()

    with monkeypatch.context() as m:
        m.setattr(llm_mod, "get_chat_model", spy)
        # auto 路由（长 query → writer role）不带 thinking
        llm_mod.get_chat_model_auto("draft", query_text="x" * 200)
    assert captured[-1][0] == "writer" and "thinking" not in captured[-1][1], (
        "research_brief 的 auto 路由不得受 DR_DRAFT_THINKING 影响"
    )

    calls: list[tuple] = []
    with monkeypatch.context() as m:
        m.setattr(llm_mod, "get_chat_model", _spy(calls))
        importlib.reload(tool_mod)
    assert all("thinking" not in kw for role, kw in calls if role == "writer"), (
        "refine_draft_report 不受 DR_DRAFT_THINKING 影响"
    )
    importlib.reload(tool_mod)  # 恢复


def test_red_team_call_site_has_no_thinking_switch(monkeypatch):
    """red_team 保持 provider 默认（ON）：调用点不得出现 thinking 参数。"""
    import deep_research.agents.red_team_agent as red_mod

    calls: list[tuple] = []
    with monkeypatch.context() as m:
        m.setenv("DR_DRAFT_THINKING", "off")
        m.setattr(llm_mod, "get_chat_model", _spy(calls))
        importlib.reload(red_mod)
    role, kwargs = calls[-1]
    assert role == "red_team" and "thinking" not in kwargs, f"red_team 调用点不应有开关，得到 {kwargs}"
    importlib.reload(red_mod)  # 恢复


def test_accepted_thinking_policy_after_e9(monkeypatch):
    """accepted 状态（E9 KEEP 后）：extractor/judge off、supervisor on、writer off、draft off。

    red_team / research-local roles 无开关（provider 默认 ON），其调用点不得出现 thinking 参数。
    """
    for var in ("DR_CLAIM_VERIFY_THINKING", "DR_SUPERVISOR_THINKING", "DR_WRITER_THINKING", "DR_DRAFT_THINKING"):
        monkeypatch.delenv(var, raising=False)
    assert llm_mod.claim_verify_thinking() is False
    assert llm_mod.supervisor_thinking() is True
    assert llm_mod.writer_thinking() is False
    assert llm_mod.draft_thinking() is False


# ===== 5. benchmark 侧 =====


def test_draft_thinking_changes_config_fingerprint(monkeypatch):
    from deep_research.benchmark.fingerprint import build_config_snapshot, config_fingerprint, thinking_snapshot

    on_env = {"DR_DRAFT_THINKING": "on"}
    off_env = {"DR_DRAFT_THINKING": "off"}
    assert thinking_snapshot(off_env)["draft"] is False
    assert thinking_snapshot(on_env)["draft"] is True

    cfg = {"roles": {"draft": {"backend": "openai", "handle": "deepseek-v4-pro"}}}
    assert config_fingerprint(build_config_snapshot(cfg, env=on_env)) != config_fingerprint(
        build_config_snapshot(cfg, env=off_env)
    ), "draft thinking 变化必须改变 config fingerprint（A/B 可区分）"


def test_preflight_fails_on_wrong_draft_thinking():
    from tests.test_benchmark_preflight import FakeProbes, _expectations
    from deep_research.benchmark.preflight import run_preflight

    probes = FakeProbes(worker_process={"pid": 1, "alive": True, "started_at": 1e12,
                                        "env": {"DR_DRAFT_THINKING": "on"}})
    report = run_preflight(_expectations(thinking={"draft": False}), probes)
    assert report["passed"] is False and "thinking_policy" in report["failed"]
    check = next(c for c in report["checks"] if c["name"] == "thinking_policy")
    assert check["observed"]["draft"] is True and check["expected"]["draft"] is False
