"""Opt-in research API runner with repeatable trial IDs and local report evidence.

Does not apply worker feature flags: restart the deployment for each variant.
A supplied config file is operator-attested, NOT automatically verified against
remote worker environment. No factual correctness is inferred from completion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from benchmarks.evaluate_runs import observation_key, read_rows
from benchmarks.run_offline import read_jsonl
from deep_research.benchmark.quality import report_metrics


def request_json(url: str, payload: dict | None = None, timeout: float = 15) -> dict:
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    request = urllib.request.Request(
        url, data=data, headers={'Content-Type': 'application/json'},
        method='POST' if payload is not None else 'GET',
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def run_task(
    base: str, task: dict, variant: str, poll: float, timeout: float,
    provider_kind: str = 'fake', *, trial: str = '1', reports_dir: Path | None = None,
    config_sha256: str | None = None, tasks_sha256: str | None = None,
) -> dict:
    identifier = f'bm-{uuid.uuid4().hex[:16]}'
    observation = {
        'task_id': task['id'], 'variant': variant, 'trial_id': trial,
        'run_id': identifier,
        'data_origin': 'real_runtime' if provider_kind == 'live' else 'fake_runtime',
        'provider_kind': provider_kind,
        'status': 'failed', 'latency_seconds': None,
        'search_calls': None, 'input_tokens': None, 'output_tokens': None,
        'cost_rmb': None, 'tool_calls': None, 'tool_successes': None,
        'claims_audited': None, 'unsupported_claims': None,
        'citations_audited': None, 'supported_citations': None,
        'aspects_audited': None, 'aspects_covered': None, 'aspect_coverage': None,
        'independent_sources_verified': None, 'required_independent_sources': None,
        'source_adequacy_pass': None, 'reviewed_success': None,
        'structural_report_nonempty': None, 'report_metrics': None,
        'report_sha256': None, 'report_file': None, 'thread_id': None,
        'task_query_sha256': digest(task['query']),
        'tasks_file_sha256': tasks_sha256, 'config_sha256': config_sha256,
        'config_attestation': 'operator_supplied_not_remotely_verified' if config_sha256 else None,
    }
    started = time.monotonic()
    try:
        response = request_json(f'{base}/api/research/start', {'query': task['query'], 'run_id': identifier})
        thread_id = response['thread_id']
        observation['thread_id'] = thread_id
        deadline = started + timeout
        did_review = False
        while time.monotonic() < deadline:
            status = request_json(f'{base}/api/research/{thread_id}/status')
            state = status.get('status', '')
            if state == 'waiting_review' and not did_review:
                request_json(f'{base}/api/research/{thread_id}/resume', {'action': 'approve', 'feedback': ''})
                did_review = True
            elif state not in ('waiting_review', 'running'):
                did_review = False
            if state in ('completed', 'failed', 'cancelled', 'deleted'):
                observation['status'] = 'cancelled' if state in ('cancelled', 'deleted') else state
                break
            time.sleep(poll)
        else:
            observation['status'] = 'timeout'
        observation['latency_seconds'] = round(time.monotonic() - started, 3)
        if observation['status'] == 'completed':
            report = request_json(f'{base}/api/research/{thread_id}/report')
            content = report.get('final_report', '')
            if not isinstance(content, str):
                raise ValueError('final_report is not a string')
            observation['structural_report_nonempty'] = bool(content.strip())
            observation['report_metrics'] = report_metrics(content)
            observation['report_sha256'] = digest(content)
            if reports_dir is not None:
                reports_dir.mkdir(parents=True, exist_ok=True)
                destination = reports_dir / f'{identifier}.md'
                destination.write_text(content, encoding='utf-8')
                observation['report_file'] = str(destination)
    except (OSError, ValueError, KeyError, TypeError, urllib.error.HTTPError) as exc:
        observation['status'] = 'failed'
        observation['latency_seconds'] = round(time.monotonic() - started, 3)
        observation['error_type'] = type(exc).__name__
    return observation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm-live', action='store_true', help='Allow API tasks and potential provider charges')
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--variant', required=True)
    parser.add_argument('--provider-kind', choices=['fake', 'live'], default='fake')
    parser.add_argument('--tasks', type=Path, default=ROOT / 'benchmarks' / 'datasets' / 'research_tasks.v1.jsonl')
    parser.add_argument('--limit', type=int, default=1)
    parser.add_argument('--poll-seconds', type=float, default=3)
    parser.add_argument('--timeout-seconds', type=float, default=1800)
    parser.add_argument('--trials', type=int, default=1, help='Repeated runs per task')
    parser.add_argument('--trial-start', type=int, default=1, help='First trial number')
    parser.add_argument('--config-snapshot', type=Path,
                        help='File describing the ACTUAL deployed config; only operator-attested')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts' / 'benchmarks' / 'live_task_runs.jsonl')
    parser.add_argument('--reports-dir', type=Path, default=ROOT / 'artifacts' / 'benchmarks' / 'reports')
    args = parser.parse_args(argv)
    if not args.confirm_live:
        parser.error('No tasks submitted. Live calls require --confirm-live.')
    if min(args.limit, args.poll_seconds, args.timeout_seconds, args.trials, args.trial_start) <= 0:
        parser.error('limit/poll/timeout/trials/trial-start must be positive')
    tasks = read_jsonl(args.tasks)[:args.limit]
    ids = [item['id'] for item in tasks]
    if not tasks or len(ids) != len(set(ids)) or not all(isinstance(item.get('query'), str) and item['query'] for item in tasks):
        parser.error('tasks must have distinct IDs and nonempty queries')
    config_hash = hashlib.sha256(args.config_snapshot.read_bytes()).hexdigest() if args.config_snapshot else None
    tasks_hash = hashlib.sha256(args.tasks.read_bytes()).hexdigest()
    target_keys = {(ident, args.variant, str(t)) for t in range(args.trial_start, args.trial_start + args.trials) for ident in ids}
    if args.output.exists():
        existing = read_rows(args.output)
        collisions = target_keys.intersection(observation_key(row) for row in existing)
        if collisions:
            parser.error(f'output already contains {len(collisions)} planned run keys (no silent duplicate append)')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('a', encoding='utf-8') as output:
        for trial_number in range(args.trial_start, args.trial_start + args.trials):
            for task in tasks:
                row = run_task(
                    args.base_url.rstrip('/'), task, args.variant,
                    args.poll_seconds, args.timeout_seconds, args.provider_kind,
                    trial=str(trial_number), reports_dir=args.reports_dir,
                    config_sha256=config_hash, tasks_sha256=tasks_hash,
                )
                output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')
                output.flush()
                print(f"{row['task_id']} trial={trial_number} {row['status']} in {row['latency_seconds']}s")
    print(f'wrote {len(target_keys)} observations to {args.output}; no audited quality is inferred')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
