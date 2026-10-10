#!/usr/bin/env python3
"""Reindex existing completed SQLite task reports into Memory 3.0 (manual only).

Default mode is read-only DRY-RUN. The --apply option can invoke the configured
embedding provider and LLM many times; back up Chroma before running it.

Examples:
  python scripts/backfill_memory_v3.py --db data/tasks.db
  python scripts/backfill_memory_v3.py --db data/tasks.db --limit 5 --apply
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from deep_research.memory.sections import split_report_sections  # noqa: E402


def iter_tasks(db_path: Path, limit: int):
    """Read tasks.db without modifying it; do not print potentially sensitive text."""
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        stmt = (
            "SELECT thread_id, query, final_report FROM tasks "
            "WHERE status = 'completed' AND length(final_report) >= 100 "
            "ORDER BY created_at ASC"
        )
        if limit:
            stmt += " LIMIT ?"
            cursor = conn.execute(stmt, (limit,))
        else:
            cursor = conn.execute(stmt)
        yield from cursor
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, required=True, help="Completed-task SQLite database")
    parser.add_argument("--chroma", type=Path, help="Chroma directory; defaults to engine data dir/chroma")
    parser.add_argument("--limit", type=int, default=20, help="Max tasks, default 20; 0 means all")
    parser.add_argument("--apply", action="store_true", help="Write memory and call models (default read-only)")
    args = parser.parse_args(argv)
    if args.limit < 0 or not args.db.is_file():
        parser.error("--limit must be >= 0 and --db must point to an existing file")
    import os
    if args.apply and os.getenv("DR_MEMORY_V3_ENABLED", "on").lower() in {"off", "0", "false"}:
        parser.error("Memory 3.0 is disabled; remove DR_MEMORY_V3_ENABLED=off before --apply")

    manager = None
    if args.apply:
        from deep_research.memory.manager import MemoryManager
        if args.chroma is None:
            from deep_research.settings import get_engine_settings
            args.chroma = get_engine_settings().resolved_memory_data_dir / "chroma"
        manager = MemoryManager(persist_dir=str(args.chroma))
    processed = 0
    incomplete = 0
    planned_windows = 0
    try:
        for task_id, query, report in iter_tasks(args.db, args.limit):
            windows = len(split_report_sections(report))
            planned_windows += windows
            digest = hashlib.sha256(report.encode("utf-8")).hexdigest()[:10]
            if manager is None:
                print(f"[DRY-RUN] task={task_id} chars={len(report)} windows={windows} sha256-prefix={digest}")
            else:
                report_id = manager.store_from_report(query or "", report)
                saved = manager._store.get_memory(report_id) if report_id else None
                ok = bool(saved and saved["metadata"].get("structured_status") == "complete" and
                          saved["metadata"].get("structured_version", 0) >= 3)
                if not ok:
                    incomplete += 1
                print(f"[{'OK' if ok else 'PENDING'}] task={task_id} windows={windows}")
            processed += 1
    except sqlite3.DatabaseError as exc:
        print(f"[ERROR] read-only tasks.db query failed: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"[ERROR] interrupted by {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4
    print(
        f"Tasks={processed}; estimated extraction windows={planned_windows}; "
        f"pending={incomplete}; mode={'APPLY' if args.apply else 'DRY-RUN'}"
    )
    if not args.apply:
        print("Dry run performed zero embedding/LLM calls. Use --apply only after Chroma backup.")
    return 2 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
