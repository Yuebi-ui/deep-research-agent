"""数据库 engine / session 生命周期。

事务边界：以 `session_scope()` 为单位，进入时开启，正常退出提交，
异常退出回滚。

**测试隔离的注入点是 :func:`resolve_database_url`** —— 详见
`tests/conftest.py` 的真实数据库守卫。注意只改 `DR_DATA_DIR` 或默认路径
是**不够**的，因为 `config.yml` 中的 `database.sqlite.path` 优先级更高。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from backend.core.errors import UnsupportedDatabaseBackend
from deep_research import logging as dr_logging
from deep_research.settings import get_engine_settings
from deep_research.utils import load_config

logger = dr_logging.get_logger(__name__)

# 当前唯一受支持的后端
SUPPORTED_BACKEND = "sqlite"

# 已移除的后端：显式配置时 fail fast，不静默降级
_REMOVED_BACKENDS = {"mysql", "mariadb"}

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
# 必须是可重入锁：get_session_factory() 在持锁期间会调用 get_engine()，
# 若用普通 Lock 会造成自我死锁（持锁线程再次取同一把锁 → 永久阻塞）。
_lock = threading.RLock()


def _load_db_config() -> dict:
    """读取当前 stage 的 database 配置。失败时返回空 dict（沿用零依赖启动）。"""
    try:
        cfg = load_config(stage_name=get_engine_settings().stage)
        return cfg.get("database", {}) or {}
    except Exception:
        return {}


def _default_db_path() -> str:
    """默认 SQLite 路径——基于项目根目录，而非进程 cwd。"""
    return str(get_engine_settings().resolved_data_dir / "tasks.db")


def resolve_database_path() -> Path:
    """Return the canonical task DB file path, independent of the process cwd.

    The database.sqlite.path config takes priority over DR_DATA_DIR. API,
    Alembic, AutoDL and health checks must use this one resolution rule.
    """
    cfg = _load_db_config()
    backend = str(cfg.get("backend") or SUPPORTED_BACKEND).strip().lower()

    if backend in _REMOVED_BACKENDS:
        raise UnsupportedDatabaseBackend(
            f"数据库后端 {backend!r} 已被移除，当前唯一受支持的后端是 "
            f"{SUPPORTED_BACKEND!r}。请将 config.yml 中 "
            f"`stages.<stage>.database.backend` 改为 'sqlite'。"
        )

    if backend != SUPPORTED_BACKEND:
        raise UnsupportedDatabaseBackend(
            f"未知的数据库后端 {backend!r}；当前仅支持 {SUPPORTED_BACKEND!r}。"
        )

    value = (cfg.get("sqlite") or {}).get("path") or _default_db_path()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = get_engine_settings().project_root / path
    return path.resolve()


def resolve_database_url() -> str:
    """SQLAlchemy URI for :func:`resolve_database_path` (test injection point)."""
    return f"sqlite+pysqlite:///{resolve_database_path()}"


def _create_engine(url: str) -> Engine:
    engine = create_engine(url, future=True)

    if url.startswith("sqlite"):
        # SQLite 默认不允许跨线程复用连接，而 FastAPI 会把同步路由
        # 放到线程池执行；同时开启 WAL 以降低读写争用。
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record):  # pragma: no cover
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        engine = engine.execution_options(
            # 让 ORM 与连接都能跨线程（配合 session_scope 的短生命周期）
            connect_args={"check_same_thread": False},
        )

    return engine


def get_engine() -> Engine:
    """获取进程内缓存的 Engine。"""
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                url = resolve_database_url()
                _engine = _create_engine(url)
                logger.info("数据库 engine 已建立（backend=%s）", SUPPORTED_BACKEND)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        # 刻意在取锁**之前**解析 engine：get_engine() 自身也要取锁，
        # 放在 with _lock 内部会造成同锁重入。这里靠 _lock 是 RLock 兜底，
        # 但显式地先解析更清晰、也不依赖锁的类型。
        engine = get_engine()
        with _lock:
            if _session_factory is None:
                _session_factory = sessionmaker(
                    bind=engine, expire_on_commit=False, future=True
                )
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务边界：正常退出提交，异常退出回滚。"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Session:
    """获取一个裸 session。

    仅用于只读查询等不需要显式事务的场景；写操作请使用
    :func:`session_scope`，以确保提交/回滚语义明确。
    """
    return get_session_factory()()


def reset_engine() -> None:
    """释放并丢弃已缓存的 engine 与 session factory。

    必须先 dispose 再置空：只丢弃引用的话，SQLite 连接要等 GC 才释放，
    期间残留的写锁会让下一条连接报 `database is locked`。
    """
    global _engine, _session_factory
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _session_factory = None
