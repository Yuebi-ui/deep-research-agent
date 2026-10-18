"""Manual-only timeline review; DOES NOT fetch or validate external URLs.

    python -m scripts.review_temporal_relation --list
    python -m scripts.review_temporal_relation --relation RELATION_ID \
        --decision confirmed_change --reviewer investigator --rationale '...'

Inspect original dated source documents BEFORE recording decisions. A review
makes a human decision auditable; it does not certify a source is current.
"""
from __future__ import annotations
import argparse


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="print candidate IDs (read-only)")
    group.add_argument("--relation", metavar="RELATION_ID")
    group.add_argument("--audit", metavar="RELATION_ID", help="display immutable review history")
    parser.add_argument("--decision", choices=["confirmed_change", "confirmed_conflict", "dismissed"])
    parser.add_argument("--reviewer")
    parser.add_argument("--rationale")
    parser.add_argument("--user", help="Configured account ID; required for multi-user review")
    args = parser.parse_args()
    from deep_research.memory.tenant import resolve_cli_account, memory_user_scope
    try:
        owner = resolve_cli_account(args.user)
    except ValueError as exc:
        parser.error(str(exc))
    with memory_user_scope(owner):
        return _run_review(args, parser)


def _run_review(args, parser) -> int:
    from deep_research.agent_builder import _get_memory_manager
    manager = _get_memory_manager()
    if args.list:
        for item in manager.get_contradictions():
            if item.relation_type == "possible_change" and item.review_status == "unverified":
                print(f"{item.id}  {item.claim_a_id} <-> {item.claim_b_id} [{item.interval_relation}]")
        return 0
    if args.audit:
        from deep_research.memory.temporal import TemporalReviewLedger
        for row in TemporalReviewLedger().audit(args.audit):
            print(row)
        return 0
    if not (args.decision and args.reviewer and args.rationale):
        parser.error("--relation requires --decision, --reviewer and --rationale")
    outcome = manager.review_temporal_relation(
        args.relation, decision=args.decision, reviewer=args.reviewer, rationale=args.rationale,
    )
    print(outcome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
