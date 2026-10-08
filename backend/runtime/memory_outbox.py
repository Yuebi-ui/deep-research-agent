"""Persistent at-least-once memory enrichment with lease fencing and checkpoints.

The final research report is the source of truth in tasks. Outbox rows contain
only a content fingerprint, small sanitized episode observations and progress
flags. Claiming a row is transactional; all completions are token-fenced.

No distributed exactly-once guarantee is claimed: a worker may crash after
writing Chroma but before recording progress. Stable memory IDs make replay
idempotent and the section extractor resumes completed windows.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import and_, or_, select, update, func
from sqlalchemy.orm import aliased

from backend.db.engine import session_scope
from backend.db.models import MemoryOutbox, ResearchTask
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)


def enabled() -> bool:
    return os.getenv("DR_MEMORY_OUTBOX_ENABLED", "on").strip().lower() not in ("0", "off", "false", "no")


class LeaseLost(RuntimeError):
    """A stale actor must never finalize another actor's claimed job."""


class IncompleteMemory(RuntimeError):
    """A Chroma section was not successfully extracted; retry later."""


class MemoryOutboxEnqueueError(RuntimeError):
    """Atomic task finalization failed; Redis job MUST remain unacked."""


@dataclass(frozen=True)
class ClaimedJob:
    job_id: str
    task_id: str
    report_sha256: str
    query: str
    episode_payload: list[dict]
    token: str
    attempts: int
    report_done: bool
    episodes_done: bool


class MemoryOutboxStore:
    """Atomic transitions over the application's existing SQLite task DB."""

    def claim(self, owner: str, *, now: float | None = None, lease_seconds: int = 600) -> ClaimedJob | None:
        current = time.time() if now is None else now
        lease_seconds = max(10, lease_seconds)
        # sqlite atomically evaluates an UPDATE's WHERE; competing SELECTs can
        # race, but only one worker can successfully CAS a particular row.
        with session_scope() as session:
            due = or_(
                and_(MemoryOutbox.status.in_(["pending", "retry"]),
                     MemoryOutbox.next_attempt_at <= current),
                and_(MemoryOutbox.status == "leased", MemoryOutbox.lease_until <= current),
            )
            ids = session.scalars(select(MemoryOutbox.job_id).where(due)
                                  .order_by(MemoryOutbox.created_at, MemoryOutbox.job_id)
                                  .limit(12)).all()
            for jid in ids:
                token = uuid.uuid4().hex
                # One active Chroma writer across ALL task workers. The
                # NOT EXISTS predicate executes inside the atomic UPDATE,
                # rather than relying on a racy prior SELECT.
                other = aliased(MemoryOutbox)
                another_active = select(other.job_id).where(
                    other.job_id != jid, other.status == "leased",
                    other.lease_until > current,
                ).exists()
                changed = session.execute(
                    update(MemoryOutbox).where(
                        MemoryOutbox.job_id == jid, due, ~another_active,
                    ).values(
                        status="leased", lease_token=token, lease_owner=owner[:200],
                        lease_until=current + lease_seconds,
                        updated_at=current, attempts=MemoryOutbox.attempts + 1,
                    )
                )
                if changed.rowcount != 1:
                    continue
                row = session.get(MemoryOutbox, jid, populate_existing=True)
                return ClaimedJob(
                    job_id=jid, task_id=row.task_id, report_sha256=row.report_sha256,
                    query=row.query, episode_payload=list(row.episode_payload or []),
                    token=token, attempts=row.attempts,
                    report_done=bool(row.report_done), episodes_done=bool(row.episodes_done),
                )
        return None

    @staticmethod
    def _fenced(session: Any, job: ClaimedJob, **values: Any) -> None:
        updated = session.execute(
            update(MemoryOutbox).where(
                MemoryOutbox.job_id == job.job_id,
                MemoryOutbox.status == "leased",
                MemoryOutbox.lease_token == job.token,
                MemoryOutbox.lease_until > time.time(),
            ).values(**values)
        )
        if updated.rowcount != 1:
            raise LeaseLost(f"outbox lease lost for {job.job_id}")

    def renew(self, job: ClaimedJob, *, now: float | None = None, lease_seconds: int = 600) -> None:
        current = time.time() if now is None else now
        with session_scope() as session:
            self._fenced(session, job, lease_until=current + max(10, lease_seconds), updated_at=current)

    def report_finished(self, job: ClaimedJob, doc_id: str | None) -> None:
        with session_scope() as session:
            self._fenced(session, job, report_done=True, doc_id=doc_id, updated_at=time.time())

    def episodes_finished(self, job: ClaimedJob) -> None:
        with session_scope() as session:
            self._fenced(session, job, episodes_done=True, updated_at=time.time())

    def complete(self, job: ClaimedJob) -> None:
        with session_scope() as session:
            row = session.get(MemoryOutbox, job.job_id)
            if row is None or not row.report_done or not row.episodes_done:
                raise RuntimeError("cannot complete a partially processed memory job")
            current = time.time()
            self._fenced(session, job, status="done", completed_at=current,
                         updated_at=current, lease_owner=None, lease_token=None,
                         lease_until=None, last_error=None)

    def fail(self, job: ClaimedJob, exc: Exception, *, max_attempts: int = 6,
             now: float | None = None) -> str:
        current = time.time() if now is None else now
        exhausted = job.attempts >= max(1, max_attempts)
        status = "dead" if exhausted else "retry"
        wait_seconds = min(3600, 10 * (2 ** min(job.attempts - 1, 9)))
        with session_scope() as session:
            self._fenced(session, job, status=status,
                         next_attempt_at=current + (wait_seconds if not exhausted else 0),
                         last_error=f"{type(exc).__name__}: {exc}"[:500],
                         lease_owner=None, lease_token=None, lease_until=None,
                         updated_at=current)
        return status

    def get_task_report(self, job: ClaimedJob) -> str:
        with session_scope() as session:
            task = session.get(ResearchTask, job.task_id)
            if not task or task.status != "completed":
                raise RuntimeError("outbox task missing or no longer completed")
            if hashlib.sha256(task.final_report.encode("utf-8")).hexdigest() != job.report_sha256:
                raise RuntimeError("completed report fingerprint mismatch; refuse to enrich unrelated content")
            return task.final_report

    def stats(self) -> dict[str, int]:
        with session_scope() as session:
            return dict(session.execute(select(MemoryOutbox.status, func.count())
                                        .group_by(MemoryOutbox.status)).all())

    def retry_dead(self, job_id: str) -> bool:
        """Explicit operator action; avoids endless automatic retries."""
        with session_scope() as session:
            row = session.get(MemoryOutbox, job_id)
            if row is None or row.status != "dead":
                return False
            row.status = "retry"
            row.attempts = 0
            row.next_attempt_at = 0
            row.last_error = None
            row.updated_at = time.time()
            return True


class MemoryOutboxProcessor:
    """One job at a time, with a heartbeat while the sync LLM/Chroma work runs."""

    def __init__(self, owner: str, *, store: MemoryOutboxStore | None = None,
                 report_writer: Callable[[str, str], str | None] | None = None,
                 episode_writer: Callable[[list[dict]], int] | None = None,
                 lease_seconds: int = 600) -> None:
        self.owner = owner
        self.store = store or MemoryOutboxStore()
        self.report_writer = report_writer or self._write_report
        self.episode_writer = episode_writer or self._write_episodes
        self.lease_seconds = max(30, lease_seconds)

    @staticmethod
    def _write_report(query: str, report: str) -> str | None:
        # Import only inside a processing thread; no model initialized on API startup.
        from deep_research.memory.runtime import get_memory_manager
        manager = get_memory_manager()
        doc_id = manager.store_from_report(query, report)
        if len(report) >= 100:
            if not doc_id or not manager.is_report_complete(doc_id):
                raise IncompleteMemory("section extraction or temporal linking is still pending")
        return doc_id

    @staticmethod
    def _write_episodes(payload: list[dict]) -> int:
        if not payload:
            return 0
        from deep_research.memory.episodes import ResearchEpisode
        from deep_research.memory.runtime import get_episode_store
        # Payload was sanitized at enqueue and capped. Re-validate on processing.
        return get_episode_store().upsert([ResearchEpisode(**record) for record in payload])

    async def _heartbeat(self, job: ClaimedJob) -> None:
        try:
            await asyncio.sleep(max(3, self.lease_seconds / 3))
            while True:
                try:
                    await asyncio.to_thread(self.store.renew, job, lease_seconds=self.lease_seconds)
                except LeaseLost:
                    logger.error("Memory outbox heartbeat fenced out: %s", job.job_id)
                    return
                except Exception as exc:
                    # A temporary DB lock must not permanently stop heartbeats
                    # while an LLM invocation may still be running.
                    logger.warning("Memory outbox heartbeat retry (%s): %s", job.job_id, exc)
                    await asyncio.sleep(5)
                    continue
                await asyncio.sleep(max(3, self.lease_seconds / 3))
        except asyncio.CancelledError:
            raise

    async def process_one(self) -> bool:
        if not enabled():
            return False  # applies to both embedded and dedicated workers
        job = await asyncio.to_thread(self.store.claim, self.owner, lease_seconds=self.lease_seconds)
        if job is None:
            return False
        heartbeat = asyncio.create_task(self._heartbeat(job), name=f"memory-lease:{job.job_id}")
        try:
            # Even on episode-only retries, ensure the parent task has not been
            # deleted or changed since the original outbox was committed.
            report = await asyncio.to_thread(self.store.get_task_report, job)
            if not job.report_done:
                doc_id = await asyncio.to_thread(self.report_writer, job.query, report)
                await asyncio.to_thread(self.store.report_finished, job, doc_id)
            if not job.episodes_done:
                await asyncio.to_thread(self.episode_writer, job.episode_payload)
                await asyncio.to_thread(self.store.episodes_finished, job)
            await asyncio.to_thread(self.store.complete, job)
            logger.info("Memory outbox completed %s", job.job_id)
        except LeaseLost as exc:
            logger.warning("Memory outbox fencing prevented stale completion: %s", exc)
        except Exception as exc:
            try:
                outcome = await asyncio.to_thread(self.store.fail, job, exc)
                logger.warning("Memory outbox %s: %s (%s)", outcome, job.job_id, type(exc).__name__)
            except LeaseLost:
                logger.warning("Memory outbox lease taken over: %s", job.job_id)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        return True
