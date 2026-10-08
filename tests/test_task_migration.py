"""Alembic 迁移测试（执行包 §9 / §12）。

覆盖两条路径：

1. 空数据库 → `alembic upgrade head` 建立最新 schema
2. 已存在的旧 schema → 原地升级，且**数据不丢**

旧 schema 的 `verification` 是 `TEXT NOT NULL DEFAULT 'null'`，即用
「字符串 'null'」表示「无核查结果」。迁移必须把它规范化为真正的 SQL NULL。
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _head_revision() -> str:
    """代码中定义的最新迁移版本。

    不硬编码版本号——否则每加一个迁移都要改测试。
    """
    from alembic.config import Config as _Cfg
    from alembic.script import ScriptDirectory

    cfg = _Cfg(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return ScriptDirectory.from_config(cfg).get_current_head()


# 0001 建立的列 —— 后续迁移只允许**新增**，不得删除或改名
_BASELINE_COLUMNS = {
    "thread_id", "query", "status", "stage", "draft_report",
    "final_report", "verification", "error", "created_at", "updated_at",
}


def _alembic_config(db_path: pathlib.Path) -> AlembicConfig:
    """构造 Alembic 配置。

    直接设置 sqlalchemy.url，避免依赖 `resolve_database_url()`——
    migrations/env.py 只在未显式设置时才去解析应用配置。
    """
    cfg = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path}")
    return cfg


def _columns(db_path: pathlib.Path) -> dict[str, sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return {r["name"]: r for r in conn.execute("PRAGMA table_info(tasks)")}
    finally:
        conn.close()


def _create_legacy_schema(db_path: pathlib.Path) -> None:
    """建立改造前的旧 schema，并写入混合形态的 verification。"""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            thread_id TEXT PRIMARY KEY,
            query TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            stage TEXT NOT NULL DEFAULT '',
            draft_report TEXT NOT NULL DEFAULT '',
            final_report TEXT NOT NULL DEFAULT '',
            verification TEXT NOT NULL DEFAULT 'null',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    rows = [
        # verification 为哨兵字符串 'null'（历史形态，59/66 条都是这样）
        ("aaa1", "q1", "pending", "", "", "", "null", "", "2026-01-01T00:00:00.000000", "2026-01-01T00:00:00.000000"),
        # verification 为真实 JSON 对象
        ("bbb2", "q2", "completed", "final_report_generation", "draft", "report",
         '{"total_claims": 10}', "", "2026-01-02T00:00:00.000000", "2026-01-02T00:00:00.000000"),
        # 软删除
        ("ccc3", "q3", "deleted", "", "", "", "null", "", "2026-01-03T00:00:00.000000", "2026-01-03T00:00:00.000000"),
    ]
    conn.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


# ===== 路径 1：空库 =====


def test_empty_database_upgrade_head(tmp_path):
    db = tmp_path / "empty.db"
    command.upgrade(_alembic_config(db), "head")

    assert db.exists()
    cols = _columns(db)

    # 0001 建立的列必须在（后续迁移只允许新增）
    assert _BASELINE_COLUMNS <= set(cols), f"缺少基线列: {_BASELINE_COLUMNS - set(cols)}"
    # verification 必须是可空（旧的 NOT NULL 不能带过来）
    assert cols["verification"]["notnull"] == 0
    assert cols["verification"]["dflt_value"] is None

    # 0002 建立的运行时列也必须存在
    assert {"attempt", "claimed_by", "claimed_at", "heartbeat_at",
            "cancel_requested", "cancel_requested_at"} <= set(cols)
    assert cols["attempt"]["notnull"] == 1
    assert cols["cancel_requested"]["notnull"] == 1


def test_upgrade_head_is_idempotent(tmp_path):
    db = tmp_path / "empty.db"
    cfg = _alembic_config(db)
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # 再跑一次不应报错

    conn = sqlite3.connect(db)
    try:
        rev = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        conn.close()
    assert rev == _head_revision()


# ===== 路径 2：旧 schema 原地升级 =====


def test_existing_schema_migration(tmp_path):
    db = tmp_path / "legacy.db"
    _create_legacy_schema(db)

    before = _columns(db)
    assert before["verification"]["notnull"] == 1, "前置条件：旧 schema 应为 NOT NULL"

    command.upgrade(_alembic_config(db), "head")

    after = _columns(db)
    assert after["verification"]["notnull"] == 0
    assert after["verification"]["dflt_value"] is None


def test_migration_normalizes_null_sentinel(tmp_path):
    """字符串 'null' 必须变成真正的 SQL NULL。"""
    db = tmp_path / "legacy.db"
    _create_legacy_schema(db)
    command.upgrade(_alembic_config(db), "head")

    conn = sqlite3.connect(db)
    try:
        sentinel = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE verification = 'null'"
        ).fetchone()[0]
        sql_null = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE verification IS NULL"
        ).fetchone()[0]
        assert sentinel == 0, "仍有残留的字符串 'null'"
        assert sql_null == 2, "两条哨兵行应变为 SQL NULL"
    finally:
        conn.close()


def test_migration_preserves_every_field(tmp_path):
    """迁移不得丢失任何字段值。"""
    db = tmp_path / "legacy.db"
    _create_legacy_schema(db)
    command.upgrade(_alembic_config(db), "head")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rows = {r["thread_id"]: dict(r) for r in conn.execute("SELECT * FROM tasks")}
    finally:
        conn.close()

    assert set(rows) == {"aaa1", "bbb2", "ccc3"}
    assert rows["bbb2"]["query"] == "q2"
    assert rows["bbb2"]["status"] == "completed"
    assert rows["bbb2"]["stage"] == "final_report_generation"
    assert rows["bbb2"]["draft_report"] == "draft"
    assert rows["bbb2"]["final_report"] == "report"
    assert rows["bbb2"]["verification"] == '{"total_claims": 10}'
    assert rows["ccc3"]["status"] == "deleted"
    assert rows["aaa1"]["created_at"] == "2026-01-01T00:00:00.000000"


def test_migration_rejects_unexpected_schema(tmp_path):
    """schema 与预期不符时必须明确失败，不做猜测性修补。"""
    db = tmp_path / "weird.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (thread_id TEXT PRIMARY KEY, query TEXT)")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="缺少预期列"):
        command.upgrade(_alembic_config(db), "head")


def test_migration_gives_orm_readable_data(tmp_path, monkeypatch):
    """迁移后的库必须能被 ORM 正确读出（含 verification 的两种形态）。"""
    db = tmp_path / "legacy.db"
    _create_legacy_schema(db)
    command.upgrade(_alembic_config(db), "head")

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from backend.db.models import ResearchTask

    engine = create_engine(f"sqlite+pysqlite:///{db}")
    with Session(engine) as session:
        tasks = {t.thread_id: t for t in session.scalars(select(ResearchTask))}

    assert tasks["aaa1"].verification is None
    assert tasks["bbb2"].verification == {"total_claims": 10}
    assert tasks["ccc3"].verification is None
