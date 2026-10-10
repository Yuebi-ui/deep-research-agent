"""Join independently annotated report reviews to measured task observations.

This module NEVER asks the generating Agent to grade itself. Reviewer labels and
source verification are external inputs; computations are deterministic. The
review file must be kept alongside local evidence (not committed as raw logs).
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from benchmarks.evaluate_runs import observation_key, read_rows, validate
from benchmarks.run_offline import read_jsonl

ASPECT_MIN = 0.80
CITATION_MIN = 0.90
UNSUPPORTED_MAX = 0.10


def _nonnegative_int(value, label: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError(f'{label} must be {"positive" if positive else "nonnegative"} integer')
    return value


def task_map(tasks: list[dict]) -> dict[str, dict]:
    result = {}
    for task in tasks:
        identifier = task.get('id')
        aspects = task.get('expected_aspects')
        minimum = task.get('min_independent_sources')
        if not isinstance(identifier, str) or not identifier or identifier in result:
            raise ValueError('missing/duplicated task id')
        if not isinstance(aspects, list) or not aspects or any(not isinstance(s, str) or not s for s in aspects):
            raise ValueError(f'missing expected_aspects for task {identifier}')
        if len(aspects) != len(set(aspects)):
            raise ValueError(f'duplicate expected_aspects for task {identifier}')
        _nonnegative_int(minimum, 'min_independent_sources', positive=True)
        result[identifier] = task
    return result


def make_templates(rows: list[dict], tasks: list[dict]) -> list[dict]:
    validate(rows)
    tasks_by_id = task_map(tasks)
    output = []
    for row in rows:
        if row['status'] != 'completed':
            continue
        task = tasks_by_id.get(row['task_id'])
        if task is None:
            raise ValueError(f"unknown task_id {row['task_id']}")
        if not row.get('report_sha256') or not row.get('run_id'):
            raise ValueError('completed task requires report_sha256 and run_id for audit')
        output.append({
            'task_id': row['task_id'], 'variant': row['variant'],
            'trial_id': observation_key(row)[2], 'run_id': row['run_id'],
            'report_sha256': row['report_sha256'],
            'reviewer_id': None, 'review_method': None, 'reviewed_at': None,
            'aspect_reviews': [{'aspect': aspect, 'covered': None, 'evidence_excerpt': ''}
                               for aspect in task['expected_aspects']],
            'source_reviews': [],
            'claims_audited': None, 'unsupported_claims': None,
            'citations_audited': None, 'supported_citations': None,
            'critical_factual_error': None,
            'template_only_not_reviewed': True,
        })
    return output


def _report_content(row: dict) -> str:
    file_name = row.get('report_file')
    if not isinstance(file_name, str) or not file_name:
        raise ValueError('quality review requires a locally saved report_file')
    report = Path(file_name).read_text('utf-8')
    observed_hash = hashlib.sha256(report.encode('utf-8')).hexdigest()
    if observed_hash != row['report_sha256']:
        raise ValueError(f"report_file hash does not match {row['task_id']} trial {observation_key(row)[2]}")
    return report


def score_review(row: dict, task: dict, review: dict) -> dict:
    """Compute a strict success decision from operator-attested external review."""
    key = observation_key(row)
    if row['status'] != 'completed':
        raise ValueError(f'cannot audit a run with no completed report: {key}')
    if any(review.get(name) != row.get(name) for name in ('task_id', 'variant', 'run_id', 'report_sha256')):
        raise ValueError(f'wrong task/variant/run_id/report hash in review for {key}')
    if observation_key(review) != key:
        raise ValueError(f'trial mismatch in review for {key}')
    if review.get('template_only_not_reviewed') is True:
        raise ValueError('audit template is not an actual review; complete and remove template flag')
    method = review.get('review_method')
    if method not in {'human', 'external_judge'}:
        raise ValueError('review_method must be human or external_judge')
    if not isinstance(review.get('reviewer_id'), str) or not review['reviewer_id'].strip():
        raise ValueError('reviewer_id required for independent review')
    if not isinstance(review.get('reviewed_at'), str) or not review['reviewed_at'].strip():
        raise ValueError('reviewed_at is required for traceability')
    try:
        timestamp = datetime.fromisoformat(review['reviewed_at'].replace('Z', '+00:00'))
    except (TypeError, ValueError) as exc:
        raise ValueError('reviewed_at must be an ISO-8601 timestamp with timezone') from exc
    if timestamp.tzinfo is None:
        raise ValueError('reviewed_at must include a timezone')
    report = _report_content(row)
    expected = task['expected_aspects']
    reviewed = review.get('aspect_reviews')
    if not isinstance(reviewed, list) or len(reviewed) != len(expected):
        raise ValueError(f'annotate all expected aspects for {key}')
    actual = [a.get('aspect') for a in reviewed if isinstance(a, dict)]
    if len(actual) != len(expected) or sorted(actual) != sorted(expected):
        raise ValueError(f'expected_aspects mismatch for {key}')
    covered = 0
    for entry in reviewed:
        if type(entry.get('covered')) is not bool:
            raise ValueError('every expected aspect must have a reviewed bool label')
        if entry['covered']:
            excerpt = entry.get('evidence_excerpt')
            if not isinstance(excerpt, str) or len(excerpt.strip()) < 10 or excerpt not in report:
                raise ValueError('covered aspect requires an exact excerpt in the saved report')
            covered += 1
    source_reviews = review.get('source_reviews')
    if not isinstance(source_reviews, list):
        raise ValueError('source_reviews must be a complete reviewed list (may be empty)')
    verified_groups = set()
    for source in source_reviews:
        if not isinstance(source, dict) or type(source.get('verified')) is not bool:
            raise ValueError('source requires a verified bool decision')
        url = source.get('url')
        group = source.get('independent_group')
        if not isinstance(url, str) or urlsplit(url).scheme not in {'http', 'https'} or not urlsplit(url).netloc:
            raise ValueError('source URL must be absolute HTTP(S)')
        if not isinstance(group, str) or not group.strip():
            raise ValueError('source independent_group is required')
        if source['verified']:
            note = source.get('verification_note')
            if not isinstance(note, str) or not note.strip():
                raise ValueError('verified source needs external evidence verification_note')
            verified_groups.add(group)
    claim_count = _nonnegative_int(review.get('claims_audited'), 'claims_audited', positive=True)
    unsupported = _nonnegative_int(review.get('unsupported_claims'), 'unsupported_claims')
    citation_count = _nonnegative_int(review.get('citations_audited'), 'citations_audited', positive=True)
    supported = _nonnegative_int(review.get('supported_citations'), 'supported_citations')
    if unsupported > claim_count or supported > citation_count:
        raise ValueError('review numerators exceed denominators')
    critical = review.get('critical_factual_error')
    if type(critical) is not bool:
        raise ValueError('critical_factual_error requires explicit bool decision')
    ratio = covered / len(expected)
    source_minimum = task['min_independent_sources']
    sources_pass = len(verified_groups) >= source_minimum
    success = (bool(report.strip()) and ratio >= ASPECT_MIN and sources_pass
               and unsupported / claim_count <= UNSUPPORTED_MAX
               and supported / citation_count >= CITATION_MIN and not critical)
    audit_hash = hashlib.sha256(json.dumps(review, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    return {
        'aspects_audited': len(expected), 'aspects_covered': covered,
        'aspect_coverage': round(ratio, 6),
        'independent_sources_verified': len(verified_groups),
        'required_independent_sources': source_minimum,
        'source_adequacy_pass': sources_pass,
        'claims_audited': claim_count, 'unsupported_claims': unsupported,
        'citations_audited': citation_count, 'supported_citations': supported,
        'critical_factual_error': critical, 'reviewed_success': success,
        'reviewer_id': review['reviewer_id'], 'review_method': method,
        'reviewed_at': review['reviewed_at'],
        'quality_provenance': ('independent_human_review' if method == 'human'
                               else 'documented_external_judge'),
        'review_sha256': audit_hash,
    }


def apply_reviews(rows: list[dict], tasks: list[dict], audits: list[dict]) -> tuple[list[dict], dict]:
    validate(rows)
    task_by_id = task_map(tasks)
    by_key = {}
    for audit in audits:
        if not isinstance(audit, dict):
            raise ValueError('audit record must be object')
        key = observation_key(audit)
        if key in by_key:
            raise ValueError(f'duplicate review for {key}')
        by_key[key] = audit
    seen = set()
    enriched = []
    for row in rows:
        key = observation_key(row)
        task = task_by_id.get(row['task_id'])
        if task is None:
            raise ValueError(f'unknown task_id {row["task_id"]}')
        new = dict(row)
        if key in by_key:
            new.update(score_review(row, task, by_key[key]))
            seen.add(key)
        enriched.append(new)
    if seen != set(by_key):
        raise ValueError(f'unknown review measurement keys: {sorted(set(by_key) - seen)}')
    validate(enriched)
    return enriched, {
        'total_runs': len(rows), 'completed_runs': sum(r['status'] == 'completed' for r in rows),
        'reviewed_runs': len(seen), 'unreviewed_runs': len(rows) - len(seen),
        'provenance': 'manual_or_external_review_attested_by_operator',
        'status': 'COMPLETE' if len(seen) == sum(r['status'] == 'completed' for r in rows) else 'PARTIAL',
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n' for row in rows), 'utf-8')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=Path, required=True)
    parser.add_argument('--tasks', type=Path, default=ROOT / 'benchmarks' / 'datasets' / 'research_tasks.v1.jsonl')
    parser.add_argument('--prepare', action='store_true', help='Create UNREVIEWED annotation templates')
    parser.add_argument('--audits', type=Path, help='Completed external review records (required unless --prepare)')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts' / 'benchmarks' / 'reviewed_runs.jsonl')
    args = parser.parse_args(argv)
    rows = read_rows(args.runs)
    tasks = read_jsonl(args.tasks)
    if args.prepare:
        if args.audits:
            parser.error('--prepare does not accept --audits')
        output = make_templates(rows, tasks)
        write_jsonl(args.output, output)
        print(f'created {len(output)} unreviewed templates at {args.output}; no quality score has been assigned')
        return 0
    if not args.audits:
        parser.error('--audits is required when not using --prepare')
    reviewed, stats = apply_reviews(rows, tasks, read_rows(args.audits))
    write_jsonl(args.output, reviewed)
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
