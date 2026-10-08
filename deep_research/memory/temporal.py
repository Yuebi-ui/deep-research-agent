"""Evidence-aware temporal *candidates* and auditable human review.

Extraction timestamps are not validity times. Differing numbers are only a
lead, never proof of a conflict or an overwrite instruction. The SQLite ledger
is the source of truth for human decisions, independent of Chroma retries.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import or_, select

from backend.db.engine import session_scope
from backend.db.models import MemoryTemporalAudit, MemoryTemporalDecision
from deep_research.memory.schemas import Contradiction, MemoryClaim

_ALLOWED = {"confirmed_change", "confirmed_conflict", "dismissed"}


from deep_research.memory.retrieval import _valid_date as _date, valid_interval, interval_relation


@dataclass(frozen=True)
class ReviewedRelation:
    relation_id: str
    claim_a_id: str
    claim_b_id: str
    decision: str
    older_claim_id: str | None
    newer_claim_id: str | None
    reviewer: str
    rationale: str
    reviewed_at: float


class TemporalReviewLedger:
    """Atomic latest decision + append-only audit, with review preconditions."""

    @staticmethod
    def _result(row: MemoryTemporalDecision) -> ReviewedRelation:
        return ReviewedRelation(
            relation_id=row.relation_id, claim_a_id=row.claim_a_id,
            claim_b_id=row.claim_b_id, decision=row.decision,
            older_claim_id=row.older_claim_id, newer_claim_id=row.newer_claim_id,
            reviewer=row.reviewer, rationale=row.rationale, reviewed_at=row.reviewed_at,
        )

    def get(self, relation_id: str) -> ReviewedRelation | None:
        with session_scope() as session:
            row = session.get(MemoryTemporalDecision, relation_id)
            return self._result(row) if row else None

    def for_claims(self, claim_ids: set[str], limit: int = 100) -> list[ReviewedRelation]:
        if not claim_ids:
            return []
        # Large Chroma collections may reference more IDs than SQLite's bind
        # parameter limit. Page IDs deterministically, then globally sort.
        refs = sorted(claim_ids)
        combined: dict[str, ReviewedRelation] = {}
        with session_scope() as session:
            for start in range(0, len(refs), 250):
                page = refs[start:start + 250]
                records = session.scalars(select(MemoryTemporalDecision).where(or_(
                    MemoryTemporalDecision.claim_a_id.in_(page),
                    MemoryTemporalDecision.claim_b_id.in_(page),
                )).order_by(MemoryTemporalDecision.reviewed_at.desc())
                    .limit(max(1, min(500, limit)))).all()
                for row in records:
                    combined[row.relation_id] = self._result(row)
        return sorted(combined.values(),
                      key=lambda item: (-item.reviewed_at, item.relation_id))[:max(1, min(500, limit))]

    def audit(self, relation_id: str) -> list[dict]:
        with session_scope() as session:
            rows = session.scalars(select(MemoryTemporalAudit).where(
                MemoryTemporalAudit.relation_id == relation_id,
            ).order_by(MemoryTemporalAudit.id)).all()
            return [dict(prior_decision=row.prior_decision, decision=row.decision,
                         reviewer=row.reviewer, rationale=row.rationale,
                         reviewed_at=row.reviewed_at) for row in rows]

    def review(self, relation: Contradiction, claim_a: MemoryClaim, claim_b: MemoryClaim,
               *, decision: str, reviewer: str, rationale: str) -> ReviewedRelation:
        """Explicit human action only. Does not mutate Claim truth status.

        'confirmed_change' needs known ordered *effective* dates and original
        source URLs for BOTH versions; observation/published timestamps do not
        imply supersession. Reversals are retained in an append-only audit.
        """
        if decision not in _ALLOWED:
            raise ValueError("decision must be confirmed_change, confirmed_conflict or dismissed")
        if not reviewer.strip() or len(reviewer.strip()) > 150 or len(rationale.strip()) < 12:
            raise ValueError("human reviewer and explanatory rationale (>=12 chars) required")
        if not relation.id or not relation.claim_a_id or not relation.claim_b_id:
            raise ValueError("review requires a bound two-Claim relation")
        if {claim_a.id, claim_b.id} != {relation.claim_a_id, relation.claim_b_id}:
            raise ValueError("provided Claims are not the referenced relation")
        if claim_a.id == claim_b.id or not claim_a.report_id or not claim_b.report_id:
            raise ValueError("claims must have distinct provenance")
        if not valid_interval(claim_a) or not valid_interval(claim_b):
            raise ValueError("cannot review invalid effective-date intervals")
        if decision != "dismissed":
            if claim_a.report_id == claim_b.report_id:
                raise ValueError("confirmed cross-report review requires different reports")
            if not set(claim_a.entities).intersection(claim_b.entities):
                raise ValueError("confirmed review requires a shared exact Entity ID")
            from urllib.parse import urlsplit
            if not all(url and urlsplit(url).scheme in {"http", "https"} and
                       urlsplit(url).hostname for url in (claim_a.source_url, claim_b.source_url)):
                raise ValueError("confirmed review requires http(s) source URLs for both versions")
        older = newer = None
        if decision == "confirmed_change":
            a0, b0 = _date(claim_a.valid_from), _date(claim_b.valid_from)
            if not a0 or not b0 or a0 == b0:
                raise ValueError("change needs two distinct explicit valid_from dates")
            previous, later = ((claim_a, claim_b) if a0 < b0 else (claim_b, claim_a))
            if not _date(previous.valid_to) or not _date(previous.valid_to) < _date(later.valid_from):
                raise ValueError("supersession requires explicit non-overlapping validity windows")
            older, newer = previous.id, later.id
        elif decision == "confirmed_conflict":
            if interval_relation(claim_a, claim_b) != "overlap":
                raise ValueError("conflict needs an explicit overlapping date interval")
        now = time.time()
        actor = reviewer.strip()
        explanation = rationale.strip()[:4000]
        with session_scope() as session:
            prior = session.get(MemoryTemporalDecision, relation.id)
            if prior and (prior.claim_a_id != relation.claim_a_id or
                          prior.claim_b_id != relation.claim_b_id):
                raise ValueError("relation ID has conflicting claim references")
            if (prior and prior.decision == decision and prior.older_claim_id == older and
                    prior.newer_claim_id == newer and prior.reviewer == actor and
                    prior.rationale == explanation):
                return self._result(prior)  # idempotent review replay
            session.add(MemoryTemporalAudit(
                relation_id=relation.id, prior_decision=prior.decision if prior else None,
                decision=decision, reviewer=actor, rationale=explanation, reviewed_at=now,
            ))
            if prior is None:
                prior = MemoryTemporalDecision(
                    relation_id=relation.id, claim_a_id=relation.claim_a_id,
                    claim_b_id=relation.claim_b_id, decision=decision,
                    older_claim_id=older, newer_claim_id=newer, reviewer=actor,
                    rationale=explanation, reviewed_at=now,
                )
                session.add(prior)
            else:
                prior.decision = decision
                prior.older_claim_id = older
                prior.newer_claim_id = newer
                prior.reviewer = actor
                prior.rationale = explanation
                prior.reviewed_at = now
            session.flush()
            return self._result(prior)
