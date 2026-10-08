#!/usr/bin/env python
"""任务库迁移快照与比对工具（执行包 §11）。

用途：在数据库迁移前后各取一次快照，比较是否发生数据丢失或语义变化。

设计约束（执行包 §11）：

* **默认只读** —— 只以只读模式打开数据库，不写任何数据。
* **必须显式传 --db** —— 不提供指向生产库的默认值，避免误操作。
* **不打印业务正文** —— 只输出计数与哈希，不含 query / report 内容。

用法：

    # 迁移前取快照
    python scripts/verify_task_migration.py snapshot --db data/tasks.db --out before.json

    # 迁移后取快照并比对
    python scripts/verify_task_migration.py snapshot --db data/tasks.db --out after.json
    python scripts/verify_task_migration.py compare --before before.json --after after.json

`compare` 在不一致时以退出码 1 结束，便于接入 CI 或脚本。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sqlite3
import sys
from typing import Any

# 迁移前后 verification 的形态会变（字符串 'null' → SQL NULL），
# 比对时统一归一化，避免把"等价"误判为"不一致"。
_LEGACY_NULL_SENTINEL = "null"


def _connect_readonly(db_path: pathlib.Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(f"数据库不存在: {db_path}")
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _normalize_verification(raw: Any) -> bool:
    """是否属于「有核查结果」。只关心有没有，不关心内容。"""
    if raw is None:
        return False
    if isinstance(raw, str):
        return raw.strip() not in ("", _LEGACY_NULL_SENTINEL)
    return True


def snapshot(db_path: pathlib.Path) -> dict[str, Any]:
    """采集只读快照。"""
    conn = _connect_readonly(db_path)
    try:
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        if not table_exists:
            raise SystemExit(f"{db_path} 中不存在 tasks 表")

        rows = list(conn.execute("SELECT * FROM tasks"))
        revision = None
        has_alembic = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        if has_alembic:
            revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]

        status_dist: dict[str, int] = {}
        for row in rows:
            status_dist[row["status"]] = status_dist.get(row["status"], 0) + 1

        thread_ids = sorted(row["thread_id"] for row in rows)
        ids_hash = hashlib.sha256("".join(thread_ids).encode("utf-8")).hexdigest()

        def non_empty(column: str) -> int:
            return sum(1 for row in rows if str(row[column] or "").strip())

        return {
            "db_path": str(db_path),
            "alembic_revision": revision,
            "task_count": len(rows),
            "status_distribution": dict(sorted(status_dist.items())),
            "thread_ids_hash": ids_hash,
            # 只输出计数，不含任何正文内容
            "draft_report_non_empty": non_empty("draft_report"),
            "final_report_non_empty": non_empty("final_report"),
            "stage_non_empty": non_empty("stage"),
            "error_non_empty": non_empty("error"),
            "verification_with_value": sum(
                1 for row in rows if _normalize_verification(row["verification"])
            ),
            "created_at_min": min((r["created_at"] for r in rows), default=None),
            "created_at_max": max((r["created_at"] for r in rows), default=None),
        }
    finally:
        conn.close()


# 必须逐项一致的字段（alembic_revision 与 verification 形态允许变化）
_STRICT_FIELDS = [
    "task_count",
    "status_distribution",
    "thread_ids_hash",
    "draft_report_non_empty",
    "final_report_non_empty",
    "stage_non_empty",
    "error_non_empty",
    "verification_with_value",
]


def compare(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """返回不一致项的描述列表；空列表表示通过。"""
    problems: list[str] = []
    for field in _STRICT_FIELDS:
        old, new = before.get(field), after.get(field)
        if old != new:
            problems.append(f"  {field}: {old!r} -> {new!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务库迁移快照与比对（只读）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_snap = sub.add_parser("snapshot", help="采集只读快照")
    p_snap.add_argument("--db", required=True, type=pathlib.Path,
                        help="数据库路径（必填，无默认值）")
    p_snap.add_argument("--out", type=pathlib.Path, default=None,
                        help="写入 JSON 文件；省略则打印到 stdout")

    p_cmp = sub.add_parser("compare", help="比对两个快照")
    p_cmp.add_argument("--before", required=True, type=pathlib.Path)
    p_cmp.add_argument("--after", required=True, type=pathlib.Path)

    args = parser.parse_args(argv)

    if args.command == "snapshot":
        data = snapshot(args.db)
        text = json.dumps(data, ensure_ascii=False, indent=2)
        if args.out:
            args.out.write_text(text, encoding="utf-8")
            print(f"快照已写入 {args.out}")
            print(text)
        else:
            print(text)
        return 0

    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    problems = compare(before, after)

    print("=== 迁移一致性比对 ===")
    for field in _STRICT_FIELDS:
        mark = "OK " if before.get(field) == after.get(field) else "DIFF"
        print(f"  [{mark}] {field}: {after.get(field)!r}")

    if problems:
        print("\n发现不一致：")
        print("\n".join(problems))
        return 1

    print("\n全部一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
