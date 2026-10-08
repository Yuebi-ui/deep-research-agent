"""Checkpointer 工厂（执行包 §0.5.2）。

决策要点：

```text
memory  仅用于无需持久恢复的测试
sqlite  单机开发 / 持久化测试
redis   Phase F Spike 与 Phase G 的目标后端

不得用 memory 验证跨进程恢复。
不得在配置要求持久化后端时静默降级到 memory。
```
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from deep_research import checkpoint as C


@pytest.fixture(autouse=True)
def _clean():
    C.discard_checkpointer()
    yield
    C.discard_checkpointer()


# ===== 后端解析 =====


def test_test_env_defaults_to_memory(monkeypatch):
    """测试环境必须默认 memory。

    config.yml 里配的是 sqlite，若不特判，测试会直接写进真实的
    data/checkpoints.db——既是测试污染，也让"测试用内存"的约定失效。
    """
    monkeypatch.delenv("CHECKPOINTER_BACKEND", raising=False)
    assert C.resolve_backend() == "memory"


def test_explicit_env_overrides_everything(monkeypatch):
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "sqlite")
    assert C.resolve_backend() == "sqlite"

    monkeypatch.setenv("CHECKPOINTER_BACKEND", "redis")
    assert C.resolve_backend() == "redis"


def test_unknown_backend_is_rejected(monkeypatch):
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
    with pytest.raises(C.CheckpointerError, match="未知的 checkpointer 后端"):
        C.resolve_backend()


def test_sqlite_path_is_absolute_and_project_rooted():
    """配置里的相对路径必须按项目根解析，而不是按进程 cwd。"""
    from deep_research.settings import get_engine_settings

    path = pathlib.Path(C.resolve_sqlite_path())
    assert path.is_absolute()
    assert str(path).startswith(str(get_engine_settings().project_root))


# ===== 创建与释放 =====


def test_create_memory_checkpointer():
    from langgraph.checkpoint.memory import InMemorySaver

    async def run():
        async with C.checkpointer_scope("memory") as managed:
            assert managed.backend == "memory"
            assert isinstance(managed.saver, InMemorySaver)

    asyncio.run(run())


def test_create_sqlite_checkpointer(tmp_path):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async def run():
        async with C.checkpointer_scope("sqlite") as managed:
            assert managed.backend == "sqlite"
            assert isinstance(managed.saver, AsyncSqliteSaver)

    # 指向临时文件，避免碰到真实 data/checkpoints.db
    original = C.resolve_sqlite_path
    C.resolve_sqlite_path = lambda: str(tmp_path / "ckpt.db")
    try:
        asyncio.run(run())
    finally:
        C.resolve_sqlite_path = original

    assert (tmp_path / "ckpt.db").exists()


# ===== 不允许静默降级 =====


def test_redis_backend_fails_loudly_when_not_enabled(monkeypatch):
    """配置要求 redis 但未启用时必须报错，不能回退到 memory。

    静默回退会让"跨进程恢复"的验证变成假阳性——测试看似通过，
    实际用的是进程内内存。

    注意：不能依赖真实 config.yml 的当前取值（Phase F 跑通验证后
    redis.enabled 已被改为 true），必须显式构造"未启用"的配置。
    """
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "redis")
    monkeypatch.setattr(C, "load_config", lambda **_kw: {"redis": {"enabled": False}})

    async def run():
        with pytest.raises(C.CheckpointerError, match="redis.enabled"):
            await C.create_checkpointer("redis")

    asyncio.run(run())


def test_redis_backend_reports_missing_url(monkeypatch):
    """启用了 redis 但没配 url —— 同样必须明确报错。"""
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "redis")
    monkeypatch.setattr(C, "load_config", lambda **_kw: {"redis": {"enabled": True}})

    async def run():
        with pytest.raises(C.CheckpointerError, match="redis.url"):
            await C.create_checkpointer("redis")

    asyncio.run(run())


def test_get_checkpointer_raises_before_init():
    """未初始化时抛错，而不是悄悄给一个 memory saver。"""
    C.discard_checkpointer()
    with pytest.raises(C.CheckpointerError, match="尚未初始化"):
        C.get_checkpointer()


def test_init_and_get_roundtrip():
    async def run():
        managed = await C.init_checkpointer()
        assert managed.backend == "memory"
        assert C.get_checkpointer() is managed.saver
        assert C.get_backend() == "memory"

    asyncio.run(run())


def test_memory_checkpointer_does_not_survive_discard():
    """memory 后端的语义：丢弃即失去状态——这正是不能用于跨进程验证的原因。"""
    from langgraph.checkpoint.memory import InMemorySaver

    async def run():
        await C.init_checkpointer()
        first = C.get_checkpointer()
        C.discard_checkpointer()

        await C.init_checkpointer()
        second = C.get_checkpointer()
        assert first is not second, "discard 后应得到新的实例"
        assert isinstance(second, InMemorySaver)

    asyncio.run(run())
