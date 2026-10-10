"""Repeated, paired agent ablation statistics with task-cluster bootstrap CIs.

Each task is an independent resampling unit; repeated trials of the same task
are NOT counted as separate independent samples. Pair by (task_id, trial_id).
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from benchmarks.evaluate_runs import aggregate, observation_key, read_rows, validate
from benchmarks.metrics import percentile

METRICS = (
    'task_completion', 'reviewed_success', 'aspect_coverage',
    'source_adequacy_pass', 'unsupported_claim_fraction',
    'supported_citation_fraction', 'search_calls', 'input_tokens',
    'output_tokens', 'latency_seconds', 'cost_rmb',
)
PROPORTIONS = {'task_completion', 'reviewed_success', 'aspect_coverage',
               'source_adequacy_pass', 'unsupported_claim_fraction', 'supported_citation_fraction'}


def measure(row: dict, metric: str) -> float | None:
    if metric == 'task_completion':
        return float(row['status'] == 'completed')
    if metric in {'unsupported_claim_fraction', 'supported_citation_fraction'}:
        denominator, numerator = (
            ('claims_audited', 'unsupported_claims') if metric == 'unsupported_claim_fraction'
            else ('citations_audited', 'supported_citations')
        )
        count = row.get(denominator)
        return float(row[numerator]) / count if count and row.get(numerator) is not None else None
    value = row.get(metric)
    return float(value) if value is not None else None


def paired_analysis(
    rows: list[dict], baseline: str, candidate: str, *, seed: int = 20261010,
    bootstrap: int = 2000, require_config_snapshots: bool = False,
) -> dict:
    origin, rows = validate(rows)
    if baseline == candidate:
        raise ValueError('baseline and candidate must differ')
    if type(seed) is not int or type(bootstrap) is not int or bootstrap < 100:
        raise ValueError('seed must be int; bootstrap at least 100')
    relevant = [r for r in rows if r['variant'] in {baseline, candidate}]
    if not relevant or {r['variant'] for r in relevant} != {baseline, candidate}:
        raise ValueError('both baseline and candidate must have measured runs')
    if require_config_snapshots and any(not r.get('config_sha256') or not r.get('task_query_sha256')
                                        for r in relevant):
        raise ValueError('missing config_sha256 or task_query_sha256 in strict mode')
    pairs_by_key = defaultdict(dict)
    per_variant_task = defaultdict(lambda: defaultdict(list))
    for row in relevant:
        task_id, variant, _ = observation_key(row)
        pairs_by_key[(task_id, observation_key(row)[2])][variant] = row
        per_variant_task[variant][task_id].append(row)
    pairs = [pair for _, pair in sorted(pairs_by_key.items()) if baseline in pair and candidate in pair]
    tasks = sorted({pair[baseline]['task_id'] for pair in pairs})
    analysis = {}
    for index, metric in enumerate(METRICS):
        task_differences = defaultdict(list)
        matched_before = []
        matched_after = []
        for pair in pairs:
            a, b = measure(pair[baseline], metric), measure(pair[candidate], metric)
            if a is not None and b is not None:
                task_differences[pair[baseline]['task_id']].append(b - a)
                matched_before.append(a)
                matched_after.append(b)
        independent_tasks = sorted(task_differences)
        task_averages = [statistics.fmean(task_differences[t]) for t in independent_tasks]
        point = statistics.fmean(task_averages) if task_averages else None
        if len(task_averages) > 1:
            rng = random.Random(seed + index)
            n = len(task_averages)
            draws = [statistics.fmean(task_averages[rng.randrange(n)] for _ in range(n))
                     for _ in range(bootstrap)]
            ci = [percentile(draws, 0.025), percentile(draws, 0.975)]
        else:
            ci = None
        # Variability is computed within the same task across repeated trials.
        by_variant_sd = {}
        for variant in (baseline, candidate):
            deviations = []
            for rset in per_variant_task[variant].values():
                values = [measure(r, metric) for r in rset]
                values = [v for v in values if v is not None]
                if len(values) >= 2:
                    deviations.append(statistics.stdev(values))
            by_variant_sd[variant] = {
                'tasks_with_repeated_observations': len(deviations),
                'mean_within_task_sample_sd': round(statistics.fmean(deviations), 6) if deviations else None,
            }
        # pp units apply to proportions; the CI is absolute, not a relative change.
        scale = 100 if metric in PROPORTIONS else 1
        analysis[metric] = {
            'n_matched_run_pairs': len(matched_before),
            'n_independent_task_clusters': len(independent_tasks),
            'n_without_both_measurements': len(pairs) - len(matched_before),
            'mean_matched_baseline': round(statistics.fmean(matched_before), 6) if matched_before else None,
            'mean_matched_candidate': round(statistics.fmean(matched_after), 6) if matched_after else None,
            'task_equal_weight_candidate_minus_baseline': round(point * scale, 6) if point is not None else None,
            'bootstrap_95pct_ci': [round(x * scale, 6) for x in ci] if ci else None,
            'effect_unit': 'percentage_points' if metric in PROPORTIONS else 'original_units',
            'within_task_variability': by_variant_sd,
        }
    return {
        'analysis_kind': 'PAIRED_TASK_CLUSTER_BOOTSTRAP',
        'data_origin': origin,
        'baseline': baseline, 'candidate': candidate,
        'matched_unique_tasks': len(tasks), 'matched_run_pairs': len(pairs),
        'unmatched_baseline_runs': sum(baseline in pair and candidate not in pair for pair in pairs_by_key.values()),
        'unmatched_candidate_runs': sum(candidate in pair and baseline not in pair for pair in pairs_by_key.values()),
        'bootstrap_replicates': bootstrap, 'random_seed': seed,
        'ci_method': 'resample task IDs, average within-task matched trial differences; percentile 2.5/97.5',
        'config_hash_coverage': {
            v: {'with_hash': sum(r.get('config_sha256') is not None for r in relevant if r['variant'] == v),
                'n_runs': sum(r['variant'] == v for r in relevant)}
            for v in (baseline, candidate)
        },
        'interpretation': 'Not a causal claim without frozen worker configs, matched sources, independent review and leakage control.',
        'metrics': analysis,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--baseline', default='memory_off')
    parser.add_argument('--candidate', default='stage_plus_episodic')
    parser.add_argument('--seed', type=int, default=20261010)
    parser.add_argument('--bootstrap', type=int, default=2000)
    parser.add_argument('--require-config-snapshots', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts' / 'benchmarks' / 'paired_trials.json')
    args = parser.parse_args(argv)
    rows = read_rows(args.input)
    output = {'summary': aggregate(rows), 'paired_trials': paired_analysis(
        rows, args.baseline, args.candidate, seed=args.seed, bootstrap=args.bootstrap,
        require_config_snapshots=args.require_config_snapshots,
    )}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False, sort_keys=True) + '\n', 'utf-8')
    print(f"paired tasks={output['paired_trials']['matched_unique_tasks']}, "
          f"run pairs={output['paired_trials']['matched_run_pairs']} -> {args.output}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
