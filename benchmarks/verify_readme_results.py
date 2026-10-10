"""Check README-to-results transcription, NOT historical experiment reproducibility.

Usage: python benchmarks/verify_readme_results.py
"""
from __future__ import annotations
import csv
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / 'results/live/v1'
PAIRS = ('memory_off', 'report_section_memory', 'stage_recall', 'stage_plus_episodic')


def load(filename):
    return json.loads((LIVE / filename).read_text('utf-8'))


def almost(actual, expected, label):
    if actual is None or abs(actual - expected) > 1e-9:
        raise AssertionError(f'{label}: expected {expected}, got {actual}')


def readme_rows(readme):
    result = {}
    for line in readme.splitlines():
        if not line.startswith('|'):
            continue
        cells = [x.strip() for x in line.strip().strip('|').split('|')]
        if len(cells) == 4:
            result[cells[0]] = cells[1:]
    return result


def value_percent(value):
    return float(value.strip().rstrip('%').replace(',', '')) / 100


def confidence_interval(text):
    match = re.search(r'\[\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*\]', text)
    if not match:
        raise AssertionError('missing CI: ' + text)
    return [float(match[1]), float(match[2])]


def verify():
    root_readme_bytes = (ROOT / 'README.md').read_bytes()
    readme = root_readme_bytes.decode('utf-8')
    rows = readme_rows(readme)
    summary = load('e2e_summary.json')
    fault = load('fault_summary.json')
    pairs = load('paired_comparisons.json')
    retrieval = load('retrieval_summary.json')
    manifest = load('manifest.json')
    legacy = json.loads((ROOT / 'results/live/summary.json').read_text('utf-8'))
    assert manifest['root_readme_sha256'] == hashlib.sha256(root_readme_bytes).hexdigest()
    assert all(d['result_kind'] == 'README_REPORTED_REAL_RUNTIME' for d in [summary, fault, pairs, retrieval, manifest, legacy])
    assert summary == {k:v for k,v in legacy.items() if k != 'source_file'}
    assert summary['faults'] == fault
    assert summary['total_records'] is None
    assert set(summary['variants']) == set(PAIRS)
    assert manifest['historical_raw_runs_in_archive'] is False

    b = summary['variants']['memory_off']
    a = summary['variants']['stage_plus_episodic']
    specs = [
        ('Task Completion Rate', 'task_completion_rate'),
        ('Reviewed Task Success', 'reviewed_success_rate'),
        ('Mean Aspect Coverage', 'avg_aspect_coverage'),
        ('Source Adequacy Pass Rate', 'source_adequacy_pass_rate'),
        ('Unsupported Claim Rate', 'unsupported_claim_rate'),
        ('Supported Citation Rate', 'supported_citation_rate'),
        ('Tool Success Rate', 'tool_success_rate'),
    ]
    for name, key in specs:
        left, right, _ = rows[name]
        for item, value, side in [(b, left, 'baseline'), (a, right, 'candidate')]:
            stored = item[key]['rate'] if isinstance(item[key], dict) else item[key]
            almost(stored, value_percent(value), name + ' ' + side)
    for k, key in [('Search Calls / Task', 'avg_search_calls'), ('Total Tokens / Task','avg_total_tokens')]:
        left, right, _ = rows[k]
        for item, val in [(b, left), (a, right)]:
            almost(item[key], float(val.replace(',','')), k)
    for item, metric in [(b, rows['Latency P50 / P95'][0]), (a, rows['Latency P50 / P95'][1])]:
        p50,p95 = [float(s.strip().removesuffix('s')) for s in metric.split('/')]
        almost(item['latency_p50_seconds'], p50, 'P50')
        almost(item['latency_p95_seconds'], p95, 'P95')
        assert item['context_overflows'] == int(rows['Context Overflow Count'][0 if item is b else 1])
        assert item['completed_tasks'] is None and item['n_runs'] is None
        assert item['review_coverage_rate'] is None
        for k in ('unsupported_claim_rate', 'supported_citation_rate', 'tool_success_rate'):
            assert item[k]['numerator'] is None and item[k]['denominator'] is None
    match = re.search(r'(\d+)\s*/\s*(\d+)（(\d+(?:\.\d+)?)%）', rows['Fault Recovery Rate'][1])
    if not match:
        raise AssertionError('fault row format changed')
    recovered, total, rate = int(match[1]), int(match[2]), float(match[3])/100
    assert fault['observed_cases'] == total and fault['recovered_count'] == recovered
    almost(fault['recovery_rate'], rate, 'fault recovery rate')
    assert fault['recovery_p50_seconds'] is None and fault['observed_by_type'] is None
    assert fault['recovered_by_type'] is None

    chroma_base, chroma_after, chroma_change = rows['Chroma Recall@5']
    ndcg_base, ndcg_after, ndcg_change = rows['nDCG@10']
    almost(retrieval['chroma']['baseline']['recall_at_5'],value_percent(chroma_base),'chroma baseline')
    almost(retrieval['chroma']['optimized']['recall_at_5'],value_percent(chroma_after),'chroma optimized')
    almost(retrieval['chroma']['baseline']['ndcg_at_10'],float(ndcg_base),'ndcg baseline')
    almost(retrieval['chroma']['optimized']['ndcg_at_10'],float(ndcg_after),'ndcg optimized')
    # The original README has TWO reported numbers: the displayed rounded
    # endpoints give +4.1pp, while a separate historical note says +4.2pp.
    # Keep both with their exact labels; do not silently rewrite raw evidence.
    almost(retrieval['chroma']['delta_from_displayed_rounded_endpoints_pp'],
           float(chroma_change.split()[0].lstrip('+')),'displayed recall delta')
    extra_recall_match = re.search(r'Chroma Recall@5 的 `([+-]\d+(?:\.\d+)?) pp`', readme)
    assert extra_recall_match, 'missing historical recall-delta note'
    almost(retrieval['chroma']['reported_recall_at_5_delta_pp'],
           float(extra_recall_match[1]), 'separately reported recall delta')
    almost(retrieval['chroma']['reported_ndcg_at_10_delta'],float(ndcg_change),'ndcg delta')

    for line in readme.splitlines():
        if not line.startswith('| `'):
            continue
        cols = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cols) != 5:
            continue
        variant = cols[0].strip('`')
        if variant not in PAIRS:
            continue
        rec = summary['variants'][variant]
        almost(rec['task_completion_rate'],value_percent(cols[1]),variant+' completion')
        almost(rec['reviewed_success_rate'],value_percent(cols[2]),variant+' reviewed')
        if variant != 'memory_off':
            delta = float(cols[3].split()[0].lstrip('+'))
            almost(pairs['pairs'][variant]['reviewed_success_delta_pp'],delta,variant+' delta')
            assert pairs['pairs'][variant]['paired_ci_95_pp'] == confidence_interval(cols[4])
            assert pairs['pairs'][variant]['matched_run_pairs'] is None
    ci_line = next(line for line in readme.splitlines() if 'Paired Reviewed Success Improvement' in line)
    assert pairs['additional_reported_ci']['main_result_paragraph_95_pp'] == confidence_interval(ci_line)
    assert pairs['pairs']['stage_plus_episodic']['paired_ci_95_pp'] != pairs['additional_reported_ci']['main_result_paragraph_95_pp']
    with (LIVE / 'ablation_summary.csv').open('r', newline='', encoding='utf-8') as f:
        ablations = list(csv.DictReader(f))
    assert len(ablations) == 4
    for row in ablations:
        rec = summary['variants'][row['variant']]
        almost(float(row['task_completion_pct'])/100,rec['task_completion_rate'],'ablation csv completion')
        almost(float(row['reviewed_success_pct'])/100,rec['reviewed_success_rate'],'ablation csv review')
    with (LIVE / 'domain_breakdown.csv').open('r', newline='', encoding='utf-8') as f:
        assert len(list(csv.DictReader(f))) == 0, 'domain rows require historical data'
    print('PASS: README-reported aggregate values are consistent across live files.')
    print('NOTE: This checks transcription only; original runs, audits and bootstrap samples are unavailable.')
    return True

if __name__ == '__main__':
    verify()
