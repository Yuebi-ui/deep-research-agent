"""pytest 共享配置和 fixtures。

本文件承担三件事：

1. **环境前置** —— 必须在任何 ``deep_research`` 导入之前设置 ``APP_ENV=test``，
   因为多个 agent 模块在 **import 期** 就构造 LLM 客户端。
2. **状态重置** —— 项目里存在若干模块级缓存/单例，不重置会串测试。
3. **外部网络守卫** —— 最后一道防线，保证默认测试零付费 API 调用。
"""

import ipaddress
import os
import socket
import sys

import pytest

# ===== 1. 环境前置（必须早于 deep_research 的任何导入）=====
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ALLOW_LIVE_EXTERNAL_APIS", "false")
# Phase 5B 定版后 DR_SEED_LITE_SPECULATIVE **默认 on**（生产默认 = 投机拓扑）。
# 测试必须确定性：统一钉为 off（= 串行语义），需要投机语义的测试自行
# monkeypatch 成 on 或显式传 speculative=True —— 两条路径都有独立覆盖。
os.environ["DR_SEED_LITE_SPECULATIVE"] = "off"
# Legacy fixture expectations use the old best-effort path; new tests override.
os.environ.setdefault("DR_MEMORY_OUTBOX_ENABLED", "off")

# 确保项目根目录在 Python path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ===== 0. 真实数据库守卫 =====

def _real_task_count(db_path) -> int | None:
    """读取真实任务库的任务数；库不存在时返回 None。"""
    import sqlite3

    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None


@pytest.fixture(scope="session", autouse=True)
def _guard_real_database():
    """确保整个测试会话没有写入真实 data/tasks.db。

    起因：`test_task_service_transitions` 的隔离 fixture 最初只覆盖了
    "默认数据库路径"，但 config.yml 里显式的 `database.sqlite.path`
    优先级更高，导致 49 条测试任务被静默写进真实数据库。
    隔离 fixture 已修正，这个守卫负责让同类失误**当场暴露**而不是静默发生。
    """
    from deep_research.settings import get_engine_settings

    real_db = get_engine_settings().resolved_data_dir / "tasks.db"
    before = _real_task_count(real_db)

    yield

    after = _real_task_count(real_db)
    assert after == before, (
        f"测试改动了真实任务库 {real_db}（{before} -> {after} 条）。"
        "某个测试的隔离 fixture 失效了——请检查它是否覆盖了 config.yml 中"
        "显式配置的 database.sqlite.path，而不是只改默认值。"
    )


# ===== 3. 网络守卫 =====

_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain", "0.0.0.0", "::1", "::", ""}
# 容器编排里常见的服务名，测试环境可能需要
_INTRANET_HOSTNAMES = {"host.docker.internal", "redis", "db", "postgres", "mysql"}


def _is_local_address(address) -> bool:
    """判断目标地址是否允许访问（回环 / 内网 / 容器服务名）。"""
    if not isinstance(address, tuple):  # AF_UNIX 等
        return True
    host = address[0]
    if not isinstance(host, str):
        return False
    if host in _LOCAL_HOSTNAMES or host in _INTRANET_HOSTNAMES:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 未知主机名——不解析 DNS，直接拦截，避免任何外部尝试
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


@pytest.fixture(autouse=True)
def _block_external_network(request, monkeypatch):
    """默认禁止测试访问外部网络。

    通过 ``@pytest.mark.allow_network`` 或 ``ALLOW_LIVE_EXTERNAL_APIS=true`` 放行。
    """
    if os.environ.get("ALLOW_LIVE_EXTERNAL_APIS", "").lower() == "true":
        yield
        return
    if request.node.get_closest_marker("allow_network") or request.node.get_closest_marker("live"):
        yield
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _guard(original):
        def wrapper(self, address, *args, **kwargs):
            if not _is_local_address(address):
                raise RuntimeError(
                    "External network access is disabled during offline tests "
                    f"(attempted {address!r}). "
                    "Set ALLOW_LIVE_EXTERNAL_APIS=true or mark the test "
                    "with @pytest.mark.allow_network to allow it."
                )
            return original(self, address, *args, **kwargs)

        return wrapper

    monkeypatch.setattr(socket.socket, "connect", _guard(real_connect), raising=True)
    monkeypatch.setattr(socket.socket, "connect_ex", _guard(real_connect_ex), raising=True)
    yield


# ===== 2. 状态重置 =====

@pytest.fixture(autouse=True)
def _reset_all_state():
    """重置各模块的进程级缓存/单例，避免测试间相互污染。"""
    from deep_research import llm
    from deep_research.callbacks.cost_tracker import set_cost_callback
    from deep_research.settings import reset_engine_settings
    from deep_research.testing.fake_llm import reset_responders
    from deep_research.tools import search_factory
    from deep_research.tools import tool as dr_tools

    reset_engine_settings()
    llm._CONFIG_CACHE.clear()
    search_factory.clear_cache()
    registry_snapshot = dict(search_factory._PROVIDER_REGISTRY)
    dr_tools.reset_search_runtime()
    set_cost_callback(None)
    reset_responders()

    from deep_research import checkpoint as dr_checkpoint

    dr_checkpoint.discard_checkpointer()

    def _reset_backend_modules():
        try:
            import backend.main as main_module
            from backend.db import reset_repository
            from backend.runtime import redis as rt_redis
            from backend.services import agent_service, sse_adapter

            reset_repository()
            agent_service._agent_cache = None
            sse_adapter._agent_trace = None

            # Phase G：runtime 的 Redis 连接与 checkpointer 都是模块级缓存，
            # 不重置会跨用例串状态（尤其是测试各自指向不同 DB 时）。
            rt_redis.discard_redis()

            # 限流器持有模块级的按 IP 计数，不清空的话 API 测试跑到第 31 个
            # 请求就会开始返回 429，表现为"单独跑通过、连跑必失败"。
            main_module._rate_limit_store.clear()
        except ImportError:
            # 仅测试 deep_research 时 backend 可能不可导入
            pass

    _reset_backend_modules()

    yield

    reset_engine_settings()
    llm._CONFIG_CACHE.clear()
    search_factory.clear_cache()
    search_factory._PROVIDER_REGISTRY.clear()
    search_factory._PROVIDER_REGISTRY.update(registry_snapshot)
    dr_tools.reset_search_runtime()
    set_cost_callback(None)
    reset_responders()
    _reset_backend_modules()


# ===== 数据 fixtures =====

@pytest.fixture
def sample_search_results():
    """提供标准的 mock 搜索结果，用于测试去重和格式化。"""
    return [
        {
            "results": [
                {"url": "https://example.com/a", "title": "文章 A", "content": "内容 A", "raw_content": "长内容 A"},
                {"url": "https://example.com/b", "title": "文章 B", "content": "内容 B", "raw_content": "长内容 B"},
            ]
        }
    ]


@pytest.fixture
def duplicate_search_results():
    """包含重复 URL 的搜索结果，用于测试去重逻辑。"""
    return [
        {
            "results": [
                {"url": "https://example.com/a", "title": "文章 A", "content": "内容 A"},
                {"url": "https://example.com/a", "title": "文章 A 重复", "content": "内容 A 重复"},
            ]
        }
    ]
