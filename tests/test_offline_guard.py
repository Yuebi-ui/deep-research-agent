"""离线守卫与 Fake provider 的接入验证。

这些测试保证：默认测试环境下不可能产生付费外部调用。
"""

import socket

import pytest


def test_external_connection_is_blocked():
    """访问公网地址必须被守卫拦截。"""
    with pytest.raises(RuntimeError, match="External network access is disabled"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect(("93.184.216.34", 80))  # example.com
        finally:
            sock.close()


def test_unknown_hostname_is_blocked():
    """未解析的主机名也应拦截，避免任何 DNS/外联尝试。"""
    with pytest.raises(RuntimeError, match="External network access is disabled"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect(("api.openai.com", 443))
        finally:
            sock.close()


def test_loopback_is_allowed_by_guard():
    """回环地址不能触发守卫错误（连接失败是另一回事）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            sock.connect(("127.0.0.1", 1))  # 几乎必然拒绝连接
        except RuntimeError as exc:  # pragma: no cover - 守卫本不该触发
            pytest.fail(f"loopback 被守卫误拦: {exc}")
        except OSError:
            pass  # ConnectionRefusedError 等属于预期
    finally:
        sock.close()


# ===== Fake provider 接入 =====


def test_test_env_routes_llm_to_fake():
    from deep_research.llm import get_chat_model
    from deep_research.testing.fake_llm import FakeChatModel

    assert isinstance(get_chat_model("supervisor"), FakeChatModel)


def test_test_env_routes_search_to_fake():
    from deep_research.providers.fake import FakeSearchProvider
    from deep_research.tools.search_factory import get_search_provider

    assert isinstance(get_search_provider(), FakeSearchProvider)


def test_test_env_does_not_require_config_yml(tmp_path, monkeypatch):
    """离线模式下解析 provider 不应触及 config.yml。"""
    missing = tmp_path / "definitely-missing.yml"
    monkeypatch.setenv("CONFIG_PATH", str(missing))

    from deep_research.llm import get_chat_model
    from deep_research.settings import reset_engine_settings
    from deep_research.tools.search_factory import get_search_provider

    reset_engine_settings()
    get_chat_model("writer")  # 不抛 FileNotFoundError 即为通过
    get_search_provider()


def test_fake_search_results_use_unresolvable_domain():
    """Fake 结果应使用保留域名，避免被误当真。"""
    from deep_research.tools.tool import tavily_search_multiple

    docs = tavily_search_multiple(["q"], max_results=2)
    assert len(docs) == 1
    urls = [r["url"] for r in docs[0]["results"]]
    assert len(urls) == 2
    assert all(u.endswith(".invalid/doc") for u in urls)


def test_fake_embeddings_are_deterministic_and_sized():
    from deep_research.memory.vector_store import _EMBEDDING_DIMS, fake_embedding

    first = fake_embedding("hello")
    assert len(first) == _EMBEDDING_DIMS
    assert first == fake_embedding("hello")
    assert first != fake_embedding("world")
