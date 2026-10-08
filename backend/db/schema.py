"""数据库 schema 版本检查。

背景：Phase C 把 `verification` 从 `TEXT NOT NULL DEFAULT 'null'` 改为
真正的 nullable JSON，模型写入 `verification=None` 时落 SQL NULL。
**未迁移的旧库连创建任务都会失败**：

    sqlalchemy.exc.IntegrityError: NOT NULL constraint failed: tasks.verification

裸的 IntegrityError 出现在第一次写任务时才爆，排查成本高。这里在启动阶段
就检查版本，给出可直接照做的修复命令。
"""

from __future__ import annotations

from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from backend.core.errors import InfrastructureError
from deep_research import logging as dr_logging
from deep_research.settings import get_engine_settings

logger = dr_logging.get_logger(__name__)

_UPGRADE_HINT = (
    "请在项目根目录执行：\n"
    "    alembic upgrade head\n"
    "该命令会把现有数据库升级到最新 schema，不会删除已有数据。"
)


def _alembic_config() -> AlembicConfig:
    """构造 Alembic 配置（指向项目根下的 alembic.ini）。"""
    root = get_engine_settings().project_root
    cfg = AlembicConfig(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    return cfg


def head_revision() -> str | None:
    """代码中定义的最新迁移版本。"""
    return ScriptDirectory.from_config(_alembic_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    """数据库当前的迁移版本；未迁移或未初始化时为 None。"""
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def schema_status(engine: Engine) -> tuple[str | None, str | None]:
    """返回 (当前版本, 最新版本)。"""
    return current_revision(engine), head_revision()


def assert_schema_up_to_date(engine: Engine) -> None:
    """校验数据库 schema 已是最新，否则抛出带修复指引的错误。

    未初始化（全新克隆）与未迁移（升级后未跑迁移）两种情况都会命中——
    两者的修复方式相同。
    """
    current, head = schema_status(engine)

    if head is None:
        # 找不到迁移脚本，说明部署包不完整
        raise InfrastructureError(
            "未找到任何 Alembic 迁移脚本，无法校验数据库 schema。"
            "请确认 migrations/ 目录与 alembic.ini 已随代码一起部署。"
        )

    if current == head:
        logger.debug("数据库 schema 已是最新（%s）", head)
        return

    if current is None:
        detail = "数据库尚未初始化或未纳入迁移管理（alembic_version 表不存在）"
    else:
        detail = f"数据库 schema 落后：当前 {current}，最新 {head}"

    raise InfrastructureError(
        f"{detail}。\n"
        f"当前代码要求 schema 版本 {head}；在旧 schema 上写入会触发\n"
        f"IntegrityError: NOT NULL constraint failed: tasks.verification。\n"
        f"{_UPGRADE_HINT}"
    )
