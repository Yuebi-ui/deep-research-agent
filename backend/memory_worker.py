"""Dedicated durable memory consolidator worker.

    python -m backend.memory_worker
    python -m backend.memory_worker --once
    python -m backend.memory_worker --stats
    python -m backend.memory_worker --retry-dead JOB_ID

Requires `alembic upgrade head` and a writable task DB / Chroma / episode path.
Run ONE dedicated instance for a local single-writer Chroma setup and set
DR_MEMORY_OUTBOX_POLL_ON_WORKER=off for the normal worker processes.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import uuid

from backend.db.engine import get_engine
from backend.db.schema import assert_schema_up_to_date
from backend.runtime.memory_outbox import MemoryOutboxProcessor, MemoryOutboxStore
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)


async def run_loop() -> None:
    processor = MemoryOutboxProcessor(owner=f"memory-worker-{uuid.uuid4().hex[:12]}")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    while not stop.is_set():
        try:
            worked = await processor.process_one()
        except Exception as exc:
            logger.warning("memory worker poll failed: %s", exc)
            worked = False
        if not worked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Durable memory consolidator")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--once", action="store_true", help="process at most one due job")
    action.add_argument("--stats", action="store_true", help="print queue statuses")
    action.add_argument("--retry-dead", metavar="JOB_ID", help="reset one dead job")
    args = parser.parse_args()
    assert_schema_up_to_date(get_engine())
    store = MemoryOutboxStore()
    if args.stats:
        print(store.stats())
    elif args.retry_dead:
        print("requeued" if store.retry_dead(args.retry_dead) else "not found or not dead")
    elif args.once:
        asyncio.run(MemoryOutboxProcessor(owner=f"memory-once-{uuid.uuid4().hex[:8]}").process_one())
    else:
        asyncio.run(run_loop())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
