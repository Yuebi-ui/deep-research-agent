"""Validate and summarize task observations without inventing missing measurements.

One observation is identified by (task_id, variant, trial_id). Legacy rows
without trial_id are treated as trial '1' for backward compatibility.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from benchmarks.metrics import percentile

VALID_SOURCES = {'real_runtime', 'fake_runtime', 'synthetic_example'}
NUMERIC_FIELDS = (
    'latency_seconds', 'search_calls', 'input_tokens', 'output_tokens',
    'cost_rmb', 'tool_calls', 'tool_successes',
)
AUDIT_FIELDS = ('claims_audited', 'unsupported_claims', 'citations_audited', 'supported_citations')
QUALITY_FIELDS = ('aspects_audited', 'aspects_covered', 'independent_sources_verified', 'required_independent_sources')
RATE_FIELDS = ('aspect_coverage',)
BOOLEAN_FIELDS = ('reviewed_success', 'source_adequacy_pass', 'critical_factual_error')


def read_rows(path: Path) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(path)
    result = []
    for n, line in enumerate(path.read_text('utf-8').splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f'invalid JSON line {n}') from exc
        if not isinstance(item, dict):
            raise ValueError(f'JSON line {n} is not an object')
        result.append(item)
    return result


def trial_id(row: dict) -> str:
    value = row.get('trial_id', '1')
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError('trial_id must be a nonempty string or positive integer')
    return value.strip()


def observation_key(row: dict) -> tuple[str, str, str]:
    return row.get('task_id'), row.get('variant'), trial_id(row)


def _number(value, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f'{name} requires a nonnegative finite number or null')
    return float(value)


def validate(rows: list[dict]) -> tuple[str, list[dict]]:
    if not rows:
        raise ValueError('empty experiment: no data to aggregate')
    sources = {row.get('data_origin') for row in rows}
    if len(sources) != 1 or not sources.issubset(VALID_SOURCES):
        raise ValueError('mixed/unknown origins are forbidden')
    seen = set()
    seen_run_ids = set()
    frozen: dict[str, str] = {}
    queries: dict[str, str] = {}
    for row in rows:
        key = observation_key(row)
        if not all(isinstance(v, str) and v for v in key):
            raise ValueError('each record needs nonempty task_id, variant and trial_id')
        if key in seen:
            raise ValueError(f'duplicate measurement for {key}')
        seen.add(key)
        run_id = row.get('run_id')
        if run_id is not None:
            if not isinstance(run_id, str) or not run_id.strip() or run_id in seen_run_ids:
                raise ValueError(f'invalid or duplicated run_id at {key}')
            seen_run_ids.add(run_id)
        if row.get('status') not in ('completed', 'failed', 'cancelled', 'timeout'):
            raise ValueError(f'invalid run status for {key}')
        for field in BOOLEAN_FIELDS:
            if row.get(field) is not None and type(row[field]) is not bool:
                raise ValueError(f'{field} must be bool or null')
        for field in NUMERIC_FIELDS + AUDIT_FIELDS + QUALITY_FIELDS + RATE_FIELDS:
            _number(row.get(field), field)
        for field in ('search_calls', 'input_tokens', 'output_tokens', 'tool_calls', 'tool_successes', *AUDIT_FIELDS, *QUALITY_FIELDS):
            value = row.get(field)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                raise ValueError(f'{field} must be a count (integer)')
        for total, part in (
            ('claims_audited', 'unsupported_claims'),
            ('citations_audited', 'supported_citations'),
            ('aspects_audited', 'aspects_covered'),
        ):
            a, b = row.get(total), row.get(part)
            if (a is None) != (b is None):
                raise ValueError(f'incomplete audit pair {total},{part} for {key}')
            if a is not None and b > a:
                raise ValueError(f'{part} exceeds {total}')
        if (row.get('tool_calls') is None) != (row.get('tool_successes') is None):
            raise ValueError('tool audit pair incomplete')
        if row.get('tool_calls') is not None and row['tool_successes'] > row['tool_calls']:
            raise ValueError('tool_successes exceeds tool_calls')
        if row.get('aspect_coverage') is not None:
            if row['aspect_coverage'] > 1:
                raise ValueError('aspect_coverage must be <=1')
            if row.get('aspects_audited') in (None, 0):
                raise ValueError('aspect_coverage requires a nonzero aspect audit')
            expected = row['aspects_covered'] / row['aspects_audited']
            if abs(row['aspect_coverage'] - expected) > 1e-5:
                raise ValueError('aspect_coverage does not match counts')
        if (row.get('independent_sources_verified') is None) != (row.get('required_independent_sources') is None):
            raise ValueError('incomplete source audit pair')
        if row.get('source_adequacy_pass') is not None:
            count = row.get('independent_sources_verified')
            threshold = row.get('required_independent_sources')
            if count is None or threshold is None or row['source_adequacy_pass'] != (count >= threshold):
                raise ValueError('source_adequacy_pass does not match counts')
        if row.get('reviewed_success') is True and row['status'] != 'completed':
            raise ValueError('failed/cancelled/timed-out run cannot be reviewed_success=True')
        # A human-written annotation is required before a measured runtime result
        # may contain a quality success decision. Synthetic examples are not evidence.
        if row['data_origin'] == 'real_runtime' and row.get('reviewed_success') is not None:
            if row.get('quality_provenance') not in {'independent_human_review', 'documented_external_judge'}:
                raise ValueError('real reviewed_success requires independent review provenance')
            if not row.get('reviewer_id') or not row.get('report_sha256'):
                raise ValueError('real reviewed_success requires reviewer_id and report_sha256')
        snapshot = row.get('config_sha256')
        if snapshot is not None:
            if not isinstance(snapshot, str) or not snapshot:
                raise ValueError('config_sha256 must be a nonempty string')
            variant = row['variant']
            if variant in frozen and frozen[variant] != snapshot:
                raise ValueError(f'config drift across trials of {variant}')
            frozen[variant] = snapshot
        query_hash = row.get('task_query_sha256')
        if query_hash is not None:
            if not isinstance(query_hash, str) or not query_hash:
                raise ValueError('task_query_sha256 must be a nonempty string')
            task = row['task_id']
            if task in queries and queries[task] != query_hash:
                raise ValueError(f'task query drift for {task}')
            queries[task] = query_hash
    return next(iter(sources)), rows


def _mean(rows: list[dict], field: str) -> float | None:
    nums = [float(r[field]) for r in rows if r.get(field) is not None]
    return round(statistics.fmean(nums), 4) if nums else None


def _rate(rows: list[dict], total: str, part: str) -> dict:
    audited = [row for row in rows if row.get(total) is not None]
    count = sum(int(row[total]) for row in audited)
    part_count = sum(int(row[part]) for row in audited)
    return {'rate': round(part_count / count, 6) if count else None,
            'numerator': part_count, 'denominator': count,
            'tasks_with_annotation': len(audited)}


def aggregate(rows: list[dict]) -> dict:
    source, rows = validate(rows)
    by_variant = defaultdict(list)
    for row in rows:
        by_variant[row['variant']].append(row)
    summary = {}
    for variant, data in sorted(by_variant.items()):
        n = len(data)
        completed = sum(row['status'] == 'completed' for row in data)
        reviewed = [row['reviewed_success'] for row in data if row.get('reviewed_success') is not None]
        durations = [row['latency_seconds'] for row in data if row.get('latency_seconds') is not None]
        sources = [row['source_adequacy_pass'] for row in data if row.get('source_adequacy_pass') is not None]
        trial_counts = defaultdict(int)
        for row in data:
            trial_counts[trial_id(row)] += 1
        summary[variant] = {
            'n_tasks': n,  # Legacy name; counts run observations. Prefer n_runs below.
            'n_runs': n, 'n_unique_tasks': len({row['task_id'] for row in data}),
            'n_distinct_trials': len(trial_counts),
            'runs_per_trial': dict(sorted(trial_counts.items())),
            'completed_tasks': completed,
            'task_completion_rate': round(completed / n, 6),
            'reviewed_success_rate': round(sum(reviewed) / len(reviewed), 6) if reviewed else None,
            'reviewed_task_count': len(reviewed),
            'review_coverage_rate': round(len(reviewed) / n, 6),
            'latency_p50_seconds': round(percentile(durations, 0.5), 3) if durations else None,
            'latency_p95_seconds': round(percentile(durations, 0.95), 3) if durations else None,
            'latency_measured_count': len(durations),
            'avg_search_calls': _mean(data, 'search_calls'),
            'avg_input_tokens': _mean(data, 'input_tokens'),
            'avg_output_tokens': _mean(data, 'output_tokens'),
            'avg_cost_rmb': _mean(data, 'cost_rmb'),
            'avg_aspect_coverage': _mean(data, 'aspect_coverage'),
            'aspect_coverage_counts': _rate(data, 'aspects_audited', 'aspects_covered'),
            'source_adequacy_pass_rate': round(sum(sources) / len(sources), 6) if sources else None,
            'source_audited_run_count': len(sources),
            'unsupported_claim_rate': _rate(data, 'claims_audited', 'unsupported_claims'),
            'supported_citation_rate': _rate(data, 'citations_audited', 'supported_citations'),
            'tool_success_rate': _rate(data, 'tool_calls', 'tool_successes'),
            'coverage_by_field': {name: sum(row.get(name) is not None for row in data)
                                  for name in (*NUMERIC_FIELDS, 'reviewed_success', 'claims_audited',
                                               'citations_audited', *QUALITY_FIELDS, *RATE_FIELDS,
                                               'source_adequacy_pass')},
        }
    return {
        'result_kind': {'synthetic_example': 'ILLUSTRATIVE_SYNTHETIC_EXAMPLE',
                        'fake_runtime': 'FAKE_PROVIDER_RUNTIME',
                        'real_runtime': 'LIVE_PROVIDER_RUNTIME_OBSERVATION'}[source],
        'data_origin': source,
        'interpretation': 'Completion is not quality success; absent audits are null; n_runs counts repeated trials.',
        'total_records': len(rows),
        'variants': summary,
    }


def paired_deltas(rows: list[dict], baseline: str, candidate: str) -> dict:
    """Backward-compatible paired means, now matched by (task_id, trial_id)."""
    _, rows = validate(rows)
    groups = defaultdict(dict)
    for row in rows:
        groups[(row['task_id'], trial_id(row))][row['variant']] = row
    matched = []
    for _, pair in sorted(groups.items()):
        if baseline in pair and candidate in pair:
            matched.append((pair[baseline], pair[candidate]))
    fields = ['latency_seconds', 'search_calls', 'input_tokens', 'output_tokens', 'cost_rmb']
    changes = {}
    for field in fields:
        pairs = [(a[field], b[field]) for a, b in matched
                 if a.get(field) is not None and b.get(field) is not None]
        before = statistics.fmean(p[0] for p in pairs) if pairs else None
        after = statistics.fmean(p[1] for p in pairs) if pairs else None
        changes[field] = {
            'n_paired': len(pairs), 'mean_before': round(before, 4) if before is not None else None,
            'mean_after': round(after, 4) if after is not None else None,
            'relative_change_pct': round(100 * (after / before - 1), 4) if before else None,
        }
    return {
        'baseline': baseline, 'candidate': candidate,
        'matched_task_count': len(matched),  # Legacy key; matched observations, not independent tasks.
        'matched_run_pairs': len(matched),
        'matched_unique_tasks': len({a['task_id'] for a, _ in matched}),
        'unmatched_baseline_runs': sum(baseline in pair and candidate not in pair for pair in groups.values()),
        'unmatched_candidate_runs': sum(candidate in pair and baseline not in pair for pair in groups.values()),
        'completion_transition': {
            'baseline_completed': sum(a['status'] == 'completed' for a, _ in matched),
            'candidate_completed': sum(b['status'] == 'completed' for _, b in matched),
        },
        'paired_field_changes': changes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts' / 'benchmarks' / 'run_summary.json')
    parser.add_argument('--baseline')
    parser.add_argument('--candidate')
    args = parser.parse_args(argv)
    rows = read_rows(args.input)
    result = aggregate(rows)
    if bool(args.baseline) != bool(args.candidate):
        parser.error('baseline and candidate must be passed together')
    if args.baseline:
        result['paired_comparison'] = paired_deltas(rows, args.baseline, args.candidate)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + '\n', 'utf-8')
    print(f"evaluated {result['total_records']} rows, origin={result['data_origin']} -> {args.output}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
