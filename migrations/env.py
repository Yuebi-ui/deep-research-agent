"""Alembic 运行环境。

连接串从应用配置解析（`backend.db.engine.resolve_database_url`），
而不是写在 alembic.ini 里——避免数据库路径出现两个真相源。

如果 config.yml 显式配置了已移除的后端（如 mysql），
`resolve_database_url()` 会抛 `UnsupportedDatabaseBackend`，
迁移会 fail fast 而不是静默连到别处。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from backend.db.engine import resolve_database_url
from backend.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 未显式指定时才从应用配置解析
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", resolve_database_url())

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连接数据库。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite 需要 batch 模式才能做 ALTER（实际是重建表）
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite 不支持大部分 ALTER COLUMN，batch 模式会以「建新表 →
            # 拷数据 → 删旧表 → 改名」的方式实现，这是官方推荐做法。
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
