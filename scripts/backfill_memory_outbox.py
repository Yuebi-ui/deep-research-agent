"""Explicit legacy-completed task reconciliation. DRY RUN by default.

    python -m scripts.backfill_memory_outbox
    python -m scripts.backfill_memory_outbox --apply

Only current completed reports are queued; research traces from old tasks
cannot be reconstructed and will not be invented.
"""
from __future__ import annotations
import argparse

from backend.db import get_repository
from backend.db.engine import get_engine
from backend.db.schema import assert_schema_up_to_date


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="persist missing outbox jobs")
    args = parser.parse_args()
    assert_schema_up_to_date(get_engine())
    repo = get_repository()
    tasks = [x for x in repo.list_tasks() if x.status == "completed" and x.final_report]
    if not args.apply:
        print(f"DRY RUN: {len(tasks)} completed tasks eligible for idempotent backfill; no writes performed")
        return 0
    created = sum(1 for task in tasks if repo.enqueue_existing_completed(task.thread_id))
    print(f"outbox jobs created={created}; completed tasks examined={len(tasks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
