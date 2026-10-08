"""Checkpointer 工厂。

三档后端的用途划分（见执行包 §0.5.2）：

```text
memory  仅用于无需持久恢复的测试
sqlite  单机开发 / 持久化相关测试（无需外部服务）
redis   Phase F Worker Compatibility Spike 的目标后端，
        以及未来 Phase G / 服务器部署的目标 checkpointer
```

**不要用 memory 验证跨进程恢复**——它活不过进程退出。

后端解析优先级：

```text
1. 环境变量 CHECKPOINTER_BACKEND（显式指定，优先级最高）
2. config.yml 中 stages.<stage>.memory.checkpoint.backend
3. 默认 memory
```

刻意**不做**"Redis 连不上就静默降级到 memory"：那会让跨进程恢复的验证
变成假阳性——测试看似通过，实际用的是内存。配置与可达性不满足时直接抛错。

实现注意：`AsyncSqliteSaver` / `AsyncRedisSaver` 的 `from_conn_string()`
都是 `@asynccontextmanager`，必须保持上下文存活。因此本模块直接使用它们的
构造函数，并把底层资源交给 :class:`ManagedCheckpointer` 负责释放。
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from deep_research import logging as dr_logging
from deep_research.settings import get_engine_settings
from deep_research.utils import load_config

logger = dr_logging.get_logger(__name__)

CheckpointerBackend = Literal["memory", "sqlite", "redis"]

DEFAULT_BACKEND = "memory"
_SUPPORTED: tuple[str, ...] = ("memory", "sqlite", "redis")


class CheckpointerError(RuntimeError):
    """checkpointer 配置或初始化失败。"""


# ===== 后端解析 =====


def resolve_backend() -> str:
    """决定使用哪种 checkpointer。

    优先级：

    ```text
    1. CHECKPOINTER_BACKEND 环境变量（显式指定，最高优先级）
    2. APP_ENV=test 时固定 memory
    3. config.yml 的 memory.checkpoint.backend
    4. 默认 memory
    ```

    第 2 条很关键：config.yml 里配的是 sqlite，若不特判，测试会直接写进
    真实的 `data/checkpoints.db`——既是测试污染，也让"测试用内存"的约定失效。
    需要持久化的测试可显式设 CHECKPOINTER_BACKEND=sqlite 覆盖。

    **这是测试隔离的注入点**——测试应替换本函数。
    """
    import os

    explicit = (os.environ.get("CHECKPOINTER_BACKEND") or "").strip().lower()
    if explicit:
        backend = explicit
    elif get_engine_settings().is_test:
        backend = "memory"
    else:
        backend = str(_checkpoint_config().get("backend") or DEFAULT_BACKEND).strip().lower()

    if backend not in _SUPPORTED:
        raise CheckpointerError(
            f"未知的 checkpointer 后端 {backend!r}；可选: {', '.join(_SUPPORTED)}"
        )
    return backend


def _checkpoint_config() -> dict:
    """读取 config.yml 中的 memory.checkpoint 配置块。"""
    try:
        cfg = load_config(stage_name=get_engine_settings().stage)
        return (cfg.get("memory") or {}).get("checkpoint") or {}
    except Exception:
        return {}


def resolve_sqlite_path() -> str:
    """Checkpointer 用的 SQLite 路径。

    优先级：``CHECKPOINTER_SQLITE_PATH`` 环境变量 > config.yml > 默认。

    环境变量这一层是必要的：跨进程测试（如 scripts/worker_spike.py 用
    subprocess 启动 worker）无法通过 monkeypatch 注入路径，只能靠环境变量，
    否则子进程会写到真实的 data/checkpoints.db。

    配置里的相对路径按**项目根**解析（与 EngineSettings 的约定一致），
    而不是按进程 cwd——否则从不同目录启动会写到不同文件。
    """
    import os

    explicit = os.environ.get("CHECKPOINTER_SQLITE_PATH")
    if explicit:
        return str(Path(explicit).expanduser().absolute())

    configured = _checkpoint_config().get("db_path")
    if configured:
        path = Path(str(configured))
        return str(path if path.is_absolute() else get_engine_settings().project_root / path)
    return str(get_engine_settings().resolved_data_dir / "checkpoints.db")


def resolve_redis_url() -> str:
    """Redis 连接串。未启用或未配置时抛错——不静默降级。"""
    try:
        cfg = load_config(stage_name=get_engine_settings().stage)
        redis_cfg = cfg.get("redis") or {}
    except Exception as exc:
        raise CheckpointerError(f"无法读取 redis 配置: {exc}") from exc

    if not redis_cfg.get("enabled"):
        raise CheckpointerError(
            "checkpointer 后端为 redis，但 config.yml 中 "
            "stages.<stage>.redis.enabled 为 false。"
            "请启用 Redis，或改用 CHECKPOINTER_BACKEND=sqlite。"
        )
    url = redis_cfg.get("url")
    if not url:
        raise CheckpointerError("checkpointer 后端为 redis，但未配置 redis.url")
    return str(url)


# ===== 受管 checkpointer =====


@dataclass
class ManagedCheckpointer:
    """checkpointer 及其底层资源。

    提供 :meth:`aclose` 以便在 lifespan / worker 退出时释放连接。
    """

    backend: str
    saver: Any
    _closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    async def aclose(self) -> None:
        for closer in self._closers:
            try:
                await closer()
            except Exception as exc:  # 释放失败不应掩盖调用方关心的异常
                logger.warning("释放 checkpointer 资源失败: %s", exc)
        self._closers.clear()


async def create_checkpointer(backend: str | None = None) -> ManagedCheckpointer:
    """按后端创建受管的 checkpointer。"""
    backend = (backend or resolve_backend()).lower()

    if backend == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        logger.info("checkpointer = InMemorySaver（进程内，**不能**跨进程恢复）")
        return ManagedCheckpointer("memory", InMemorySaver())

    if backend == "sqlite":
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        path = resolve_sqlite_path()
        conn = await aiosqlite.connect(path)
        saver = AsyncSqliteSaver(conn)
        logger.info("checkpointer = AsyncSqliteSaver（%s）", path)
        return ManagedCheckpointer("sqlite", saver, _closers=[conn.close])

    if backend == "redis":
        from langgraph.checkpoint.redis.aio import AsyncRedisSaver

        url = resolve_redis_url()
        saver = AsyncRedisSaver(redis_url=url)
        # AsyncRedisSaver 自身是异步上下文管理器，由它负责连接生命周期
        await saver.__aenter__()
        await saver.asetup()
        logger.info("checkpointer = AsyncRedisSaver（%s）", url)

        async def _close() -> None:
            await saver.__aexit__(None, None, None)

        return ManagedCheckpointer("redis", saver, _closers=[_close])

    raise CheckpointerError(f"未知的 checkpointer 后端: {backend!r}")


# ===== 进程内单例 =====

_managed: ManagedCheckpointer | None = None
_lock = threading.Lock()


async def init_checkpointer() -> ManagedCheckpointer:
    """初始化并缓存 checkpointer（应在 lifespan / worker 启动时调用）。"""
    global _managed
    if _managed is None:
        created = await create_checkpointer()
        with _lock:
            if _managed is None:
                _managed = created
    return _managed


def get_checkpointer() -> Any:
    """返回已初始化的 saver。

    未初始化时抛错而不是回退到 memory——静默回退会让"跨进程恢复"的验证
    变成假阳性。
    """
    if _managed is None:
        raise CheckpointerError(
            "checkpointer 尚未初始化。请先 await init_checkpointer()"
            "（正常路径由 FastAPI lifespan 或 worker 启动流程负责）。"
        )
    return _managed.saver


def get_backend() -> str:
    """当前实际使用的后端；未初始化时返回解析结果。"""
    return _managed.backend if _managed is not None else resolve_backend()


async def reset_checkpointer() -> None:
    """优雅释放并丢弃缓存的 checkpointer（用于 lifespan / worker 退出）。"""
    global _managed
    with _lock:
        current, _managed = _managed, None
    if current is not None:
        await current.aclose()


def discard_checkpointer() -> None:
    """仅丢弃缓存的引用，不做异步释放。

    供**同步的**测试隔离 fixture 使用——那里的 autouse fixture 无法 await。
    内存后端无需释放；连接的回收交给进程退出或 GC，不会跨测试串状态。
    """
    global _managed
    with _lock:
        _managed = None


@asynccontextmanager
async def checkpointer_scope(backend: str | None = None) -> AsyncIterator[ManagedCheckpointer]:
    """按需创建并在退出时释放的 checkpointer 作用域。

    适合 worker / 一次性脚本：不依赖全局单例，退出即释放。
    """
    managed = await create_checkpointer(backend)
    try:
        yield managed
    finally:
        await managed.aclose()
