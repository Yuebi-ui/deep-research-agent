"""Generate coherent, fully AUDITABLE *synthetic* E2E reference examples.

This does NOT call the application, a model, Redis, Chroma or a search provider.
These records are hypothetical examples to demonstrate result schemas, rollups,
plots and interview metric definitions, not measured system performance.

Run from repo root:
    python benchmarks/fixtures/build_full_reference_results.py
    python benchmarks/fixtures/build_full_reference_results.py --verify
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.evaluate_faults import evaluate as evaluate_faults
from benchmarks.evaluate_runs import aggregate, paired_deltas
from benchmarks.run_offline import read_jsonl

SEED = 20261009
KIND = 'ILLUSTRATIVE_SYNTHETIC_EXAMPLE'
VARIANTS = ('memory_off', 'report_section_memory', 'stage_recall', 'stage_plus_episodic')
TASKS = ROOT / 'benchmarks/datasets/research_tasks.v1.jsonl'
FAULTS = ROOT / 'benchmarks/datasets/fault_scenarios.v1.jsonl'
RETRIEVAL = ROOT / 'results/offline/v1/retrieval_summary.json'
PRESETS = ROOT / 'benchmarks/configs/runtime_ablation.v1.json'
TARGET = ROOT / 'results/examples/reference_v1'
# Fictional blended rates for arithmetic demonstrations. Not current provider prices.
INPUT_RMB_PER_M = 28.0
OUTPUT_RMB_PER_M = 112.0
SEARCH_RMB_PER_CALL = 0.015

# Completion and hypothetical quality-review counts, measured against 60 prompts.
TARGET_COMPLETIONS = (53, 55, 56, 57)
TARGET_REVIEWED_SUCCESS = (46, 49, 51, 54)


def jbytes(payload: object) -> bytes:
    return (json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')


def lines_bytes(rows: list[dict]) -> bytes:
    return ''.join(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n' for row in rows).encode('utf-8')


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def csv_bytes(rows: list[dict], fields: list[str]) -> bytes:
    dest = io.StringIO(newline='')
    writer = csv.DictWriter(dest, fieldnames=fields, extrasaction='ignore', lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return dest.getvalue().encode('utf-8-sig')  # opens with readable Chinese/Excel headers too


def round6(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


def success_sets(tasks: list[dict]) -> tuple[list[set[str]], list[set[str]]]:
    """Correlated simulated outcomes, with modest task-level regressions permitted."""
    task_ids = [t['id'] for t in tasks]
    completion_sets: list[set[str]] = []
    review_sets: list[set[str]] = []
    for k, (n_completed, n_reviewed) in enumerate(zip(TARGET_COMPLETIONS, TARGET_REVIEWED_SUCCESS)):
        risk = {}
        review_risk = {}
        for tid in task_ids:
            # Common task-level difficulty plus variant perturbation; never mask failures.
            base = random.Random(f'completion-difficulty:{SEED}:{tid}').random()
            noise = random.Random(f'completion-noise:{SEED}:{tid}:{k}').uniform(-.13, .13)
            quality_base = random.Random(f'quality-difficulty:{SEED}:{tid}').random()
            quality_noise = random.Random(f'quality-noise:{SEED}:{tid}:{k}').uniform(-.18, .18)
            risk[tid] = base + noise
            review_risk[tid] = quality_base * .65 + base * .35 + quality_noise
        completed = set(sorted(task_ids, key=lambda t: (risk[t], t))[:n_completed])
        quality = set(sorted(completed, key=lambda t: (review_risk[t], t))[:n_reviewed])
        completion_sets.append(completed)
        review_sets.append(quality)
    return completion_sets, review_sets


def draws(rng: random.Random, n: int, p: float) -> int:
    return sum(rng.random() < p for _ in range(n))


def task_rows(tasks: list[dict]) -> list[dict]:
    completion, reviewed = success_sets(tasks)
    all_rows = []
    for task in tasks:
        task_id = task['id']
        base_rng = random.Random(f'task-baseline:{SEED}:{task_id}')
        base_search = base_rng.randint(9, 17)
        base_input = base_rng.randint(35500, 61500)
        base_output = base_rng.randint(5200, 9250)
        overhead = base_rng.randint(96, 178)
        risk = base_rng.random()
        for idx, variant in enumerate(VARIANTS):
            rng = random.Random(f'task-variant:{SEED}:{task_id}:{variant}')
            # Memory reduces redundant searching only in this hypothetical model.
            saved_calls = [0, 1, 2, 3][idx] + (int(idx > 1 and risk > .70))
            search = max(1, base_search - saved_calls + (1 if idx and rng.random() < .07 else 0))
            prompt_savings = [0, 2200, 4300, 5850][idx]
            # A few cases gain memory context, so candidate input tokens can rise.
            input_tokens = max(1500, base_input - prompt_savings + rng.randint(-1300, 1500))
            output_tokens = max(500, base_output + rng.randint(-380, 390))
            tool_calls = search + rng.randint(1, 5)
            tool_error_probability = [0.092, 0.078, 0.068, 0.055][idx]
            tool_failures = draws(rng, tool_calls, tool_error_probability)
            tool_successes = tool_calls - tool_failures
            elapsed = overhead + 4.0 * search + input_tokens / 1400 + output_tokens / 2300
            elapsed += rng.gauss(0, 7) + [0, 4, 6, 9][idx]  # retrieval CPU/IO overhead
            elapsed = round(max(25.0, elapsed), 2)
            input_cost = input_tokens * INPUT_RMB_PER_M / 1_000_000
            output_cost = output_tokens * OUTPUT_RMB_PER_M / 1_000_000
            search_cost = search * SEARCH_RMB_PER_CALL
            cost_rmb = round(input_cost + output_cost + search_cost, 6)
            ok = task_id in completion[idx]
            success = task_id in reviewed[idx]
            if success and not ok:
                raise AssertionError('simulated success requires completion')
            # Hypothetical independent manual review. Failed tasks have no report to audit.
            claims_audited = (max(8, round(output_tokens / 270)) + rng.randrange(0, 4)) if ok else None
            claim_bad_p = [.155, .125, .101, .076][idx] + (.095 if not success else 0)
            unsupported_claims = draws(rng, claims_audited, claim_bad_p) if ok else None
            citations_audited = (max(6, round(output_tokens / 380)) + rng.randrange(0, 4)) if ok else None
            citation_bad_p = [.175, .139, .104, .072][idx] + (.10 if not success else 0)
            supported_citations = (citations_audited - draws(rng, citations_audited, citation_bad_p)) if ok else None
            facts_reviewed = rng.randint(3, 8) if ok else None
            stale_fact_errors = draws(rng, facts_reviewed, [.10, .08, .065, .051][idx]) if ok else None
            row = {
                'task_id': task_id, 'variant': variant,
                'data_origin': 'synthetic_example', 'result_kind': KIND,
                'scenario': 'hypothetical_agent_e2e_run',
                'status': 'completed' if ok else ('timeout' if rng.random() < .30 else 'failed'),
                'reviewed_success': success,
                'latency_seconds': elapsed,
                'search_calls': search,
                'tool_calls': tool_calls,
                'tool_successes': tool_successes,
                'input_tokens': input_tokens,
                'output_tokens': output_tokens,
                'total_tokens': input_tokens + output_tokens,
                'input_cost_rmb': round(input_cost, 6),
                'output_cost_rmb': round(output_cost, 6),
                'search_cost_rmb': round(search_cost, 6),
                'cost_rmb': cost_rmb,
                'claims_audited': claims_audited,
                'unsupported_claims': unsupported_claims,
                'citations_audited': citations_audited,
                'supported_citations': supported_citations,
                'temporal_facts_audited': facts_reviewed,
                'stale_fact_errors': stale_fact_errors,
                'context_overflow_count': 0,
                'domain': task['domain'], 'language': task['language'],
                'note': 'FICTIONAL REFERENCE ONLY; NOT MODEL/PROVIDER/WORKER MEASUREMENTS',
            }
            all_rows.append(row)
    return sorted(all_rows, key=lambda r: (r['variant'],r['task_id']))


def fault_rows(scenarios: list[dict]) -> list[dict]:
    rows = []
    unrecovered_id = 'outbox_storage_failure-07'  # Simulated DLQ/manual intervention.
    for row in scenarios:
        ident = row['id']
        rng = random.Random(f'fault:{SEED}:{ident}')
        recovered = ident != unrecovered_id
        base = {
            'research_worker_termination': 12.0,
            'memory_worker_termination': 14.0,
            'lease_expiration': 19.0,
            'redis_unavailability': 24.0,
            'outbox_storage_failure': 29.0,
        }[row['fault_type']]
        duration = round(base + row['retry_attempt'] * .8 + rng.uniform(0.6, 8.0), 2) if recovered else None
        rows.append({
            'scenario_id': ident, 'fault_type': row['fault_type'],
            'data_origin': 'synthetic_example', 'result_kind': KIND,
            'injection_point': row['injection_point'],
            'status_after_observation': 'recovered' if recovered else 'dead_letter_manual_replay_required',
            'recovered': recovered,
            'lost_jobs': 0,
            'duplicate_writes': 0,
            'idempotency_violations': 0,
            'recovery_seconds': duration,
            'retry_attempt': row['retry_attempt'],
            'note': 'GENERATED SCENARIO; NOT AN EXECUTED FAULT INJECTION',
        })
    return rows


def _as_float(rows: list[dict], key: str) -> list[float]:
    return [float(r[key]) for r in rows if r.get(key) is not None]


def extra_stats(rows: list[dict]) -> dict[str, dict]:
    by = defaultdict(list)
    for r in rows:
        by[r['variant']].append(r)
    result = {}
    for variant, records in sorted(by.items()):
        facts = sum(r['temporal_facts_audited'] or 0 for r in records)
        errors = sum(r['stale_fact_errors'] or 0 for r in records)
        result[variant] = {
            'avg_total_tokens': round(sum(r['total_tokens'] for r in records)/len(records), 4),
            'avg_tool_calls': round(sum(r['tool_calls'] for r in records)/len(records),4),
            'temporal_facts_audited': facts,
            'stale_fact_errors': errors,
            'stale_fact_error_rate': round6(errors/facts if facts else None),
            'context_overflows': sum(r['context_overflow_count'] for r in records),
            'hypothetical_billing_formula_verified': all(abs(r['cost_rmb'] - (
                r['input_tokens'] * INPUT_RMB_PER_M / 1_000_000 +
                r['output_tokens'] * OUTPUT_RMB_PER_M / 1_000_000 +
                r['search_calls'] * SEARCH_RMB_PER_CALL)) < 0.000001 for r in records),
        }
    return result


def full_summary(rows: list[dict], faults: list[dict], fault_catalog: list[dict]) -> dict:
    e2e = aggregate(rows)
    extras = extra_stats(rows)
    for key,value in extras.items():
        e2e['variants'][key].update(value)
    e2e['cost_is_hypothetical'] = True
    e2e['quality_review_is_hypothetical'] = True
    e2e['result_kind'] = KIND
    e2e['price_assumptions'] = {
        'input_rmb_per_million_tokens': INPUT_RMB_PER_M,
        'output_rmb_per_million_tokens': OUTPUT_RMB_PER_M,
        'search_rmb_per_call': SEARCH_RMB_PER_CALL,
        'provider': 'fictional_blended_price_example_NOT_A_QUOTE',
    }
    e2e['faults'] = evaluate_faults(fault_catalog, faults)
    return e2e


def ablation_csv(summary: dict) -> tuple[bytes, list[dict]]:
    data = []
    for variant in VARIANTS:
        row = summary['variants'][variant]
        data.append({
            'variant': variant,
            'tasks': row['n_tasks'],
            'completed': row['completed_tasks'],
            'completion_pct': round(100*row['task_completion_rate'], 3),
            'reviewed_success_count': round(row['reviewed_success_rate'] * row['reviewed_task_count']),
            'reviewed_success_pct': round(100*row['reviewed_success_rate'], 3),
            'latency_p50_s': row['latency_p50_seconds'],
            'latency_p95_s': row['latency_p95_seconds'],
            'search_calls_per_task': row['avg_search_calls'],
            'input_tokens_per_task': row['avg_input_tokens'],
            'output_tokens_per_task': row['avg_output_tokens'],
            'total_tokens_per_task': row['avg_total_tokens'],
            'cost_rmb_per_task_hypothetical': row['avg_cost_rmb'],
            'tool_success_pct': round(100*row['tool_success_rate']['rate'],3),
            'unsupported_claim_pct': round(100*row['unsupported_claim_rate']['rate'],3),
            'supported_citation_pct': round(100*row['supported_citation_rate']['rate'],3),
            'stale_fact_error_pct': round(100*row['stale_fact_error_rate'],3),
            'context_overflows': row['context_overflows'],
            'data_origin': 'synthetic_example',
        })
    return csv_bytes(data,list(data[0])), data


def domain_csv(rows: list[dict]) -> bytes:
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row['variant'],row['domain'],row['language'])].append(row)
    result = []
    for (variant, domain, language), data in sorted(grouped.items()):
        result.append({
            'variant':variant, 'domain':domain, 'language':language,
            'tasks':len(data),
            'completed':sum(r['status']=='completed' for r in data),
            'reviewed_successes':sum(r['reviewed_success'] is True for r in data),
            'mean_search_calls':round(sum(r['search_calls'] for r in data)/len(data),4),
            'mean_cost_rmb_hypothetical':round(sum(r['cost_rmb'] for r in data)/len(data),4),
            'data_origin':'synthetic_example',
        })
    return csv_bytes(result,list(result[0]))


def manifest() -> dict:
    return {
        'result_kind': KIND,
        'data_origin': 'synthetic_example',
        'seed': SEED,
        'fixture_version': 'reference_v1',
        'date_label': 'synthetic_fixture_generated_from_repo_snapshot',
        'task_data_set': {'path':str(TASKS.relative_to(ROOT)), 'sha256':sha(TASKS), 'count':60},
        'fault_catalog': {'path':str(FAULTS.relative_to(ROOT)), 'sha256':sha(FAULTS), 'count':40},
        'ablation_config': {'path':str(PRESETS.relative_to(ROOT)), 'sha256':sha(PRESETS)},
        'measured_offline_fixture_snapshot': {'path':str(RETRIEVAL.relative_to(ROOT)), 'sha256':sha(RETRIEVAL),
                                             'note':'separate real offline fixture output, not synthetic E2E data'},
        'generator': {'path':'benchmarks/fixtures/build_full_reference_results.py',
                      'sha256':sha(Path(__file__))},
        'hypothetical_model_and_prices': {
            'provider': 'none',
            'model': 'NOT_EXECUTED_reference_model',
            'input_rmb_per_million_tokens': INPUT_RMB_PER_M,
            'output_rmb_per_million_tokens': OUTPUT_RMB_PER_M,
            'search_rmb_per_call': SEARCH_RMB_PER_CALL,
            'cost_kind': 'formula_generated_reference_not_actual_invoice',
        },
        'sample_sizes': {'tasks':60, 'runtime_variants':len(VARIANTS),
                         'task_variant_rows':60*len(VARIANTS), 'fault_scenarios':40},
        'limitations': [
            'No agents, model, vector DB, real search tool, Redis or Worker were executed.',
            'All task reviews and source-support audit counts were generated mathematically, not checked by humans.',
            'Cost uses fictional unit prices and is not a provider quote or measured invoice.',
            'Published empirical offline fixture results remain in results/offline/v1/ and are not altered.',
            'Successful task review is an illustrative label, not evidence of report correctness.',
        ],
    }


def overview_md(summary: dict, ablation_data: list[dict]) -> bytes:
    main = summary['faults']
    retrieval = json.loads(RETRIEVAL.read_text('utf-8'))
    header = [
        '# 合成参考结果（不是 Agent 实测）',
        '',
        '> **本页所有 E2E、质量、成本和故障数据均由固定种子生成，用于演示结果结构和数值关系；不能用于声称项目已达到这些指标。**',
        '',
        '本示例与仓库中真实执行的 [离线词法检索压力测试](../../offline/v1/README.md) 完全分开。',
        '',
        '## 研究任务消融（60 个任务 / 组，四组共 240 行合成任务记录）',
        '',
        '| 组别 | 成功任务 | 完成任务 | P50 / P95 (s) | Search/task | Token/task | 假设成本 (元/task) | 不支持 Claim | 来源支持引用 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for row in ablation_data:
        header.append('| {variant} | {success}/60 ({success_pct:.1f}%) | {completed}/60 | {p50:.1f} / {p95:.1f} | {calls:.1f} | {tokens:,.0f} | {cost:.2f} | {claim:.1f}% | {citation:.1f}% |'.format(
            variant=row['variant'], success=row['reviewed_success_count'],
            success_pct=row['reviewed_success_pct'],completed=row['completed'],
            p50=row['latency_p50_s'],p95=row['latency_p95_s'],calls=row['search_calls_per_task'],
            tokens=row['total_tokens_per_task'],cost=row['cost_rmb_per_task_hypothetical'],
            claim=row['unsupported_claim_pct'],citation=row['supported_citation_pct']))
    header += [
        '',
        '## 假设故障注入（不代表实际运行）',
        '',
        f"- 模拟场景：{main['observed_cases']} / {main['planned_cases']}；自动恢复：{main['recovered_count']} / {main['observed_cases']}（{100*main['recovery_rate']:.1f}%）。",
        f"- 恢复耗时：P50 {main['recovery_p50_seconds']:.2f}s / P95 {main['recovery_p95_seconds']:.2f}s（只统计已恢复场景）。",
        f"- 模拟记录中的任务丢失数：{main['lost_jobs']}；重复持久化写入数：{main['duplicate_writes']}。",
        '- 模拟的未恢复场景保留在 Dead Letter，需要人工处理；它不计入成功率或恢复耗时。',
        '',
        '## 另一个数据来源：真实执行过的离线词法检索测试',
        '',
        '> **下面这张表是代码真实计算得到的离线 fixture 分数，不是上述 Agent 合成示例、不是 Chroma Embedding 实测、也不是线上效果。**',
        '',
        '| 离线检索方案 | Recall@1 | Recall@3 | Recall@5 | MRR@10 | nDCG@10 |',
        '|---|---:|---:|---:|---:|---:|',
        *[
            f"| {name} | {100*metrics['recall_at_1']:.1f}% | {100*metrics['recall_at_3']:.1f}% | "
            f"{100*metrics['recall_at_5']:.1f}% | {metrics['reciprocal_rank_at_10']:.3f} | {metrics['ndcg_at_10']:.3f} |"
            for name,metrics in retrieval['metrics_by_variant'].items()
        ],
        '',
        f"公开 fixture：{retrieval['data_counts']['documents']} 条文档、{retrieval['data_counts']['queries']} 个标注问题；"
        '结果存在融合排序退化，应保留而不美化。详见 `results/offline/v1/`。',
        '',
        '## 口径与限制',
        '',
        '- `reviewed_success` 是模拟的审阅结论；HTTP completed 不直接等同于质量合格。',
        '- Unsupported Claim 为假设审阅的“不支持 Claim 数 / 审阅 Claim 总数”；引用指标也是假设逐引用核查结果，不是静态编号检查。',
        '- 失败且没有报告的任务，不伪造 Claim/Citation 审阅数；汇总仅对有可审阅报告的任务计算比例。',
        '- Token 为模拟用量；成本按固定**虚构单价**（见 `manifest.synthetic.json`）计算，绝非实际账单。',
        '- 故障场景的 `recovered=false` 无恢复耗时，这是有意义的缺失值，而不是遗漏数据。',
        '- 四组消融仅使用仓库里真实存在的 feature flag 组合，未将 Claim Verification/Outbox 冒充可切换实验。',
        '- 这些样例不能用于简历成果数字、真实项目效果宣称或与论文榜单比较。',
        '',
        '## 复算',
        '',
        '```bash',
        'python benchmarks/fixtures/build_full_reference_results.py --verify',
        'python -m unittest discover -s benchmarks/tests',
        'python benchmarks/publish_offline.py --verify',
        '```',
        '',
        '🔴 逐任务和逐故障观测原始记录不放入公开仓库；本地需要时可用生成器重建：',
        '',
        '```bash',
        'python benchmarks/fixtures/build_full_reference_results.py --raw-output artifacts/benchmarks/reference_v1_raw',
        '```',

        '',
    ]
    return ('\n'.join(header)).encode('utf-8')


def highlight_reference_readme(content: bytes) -> bytes:
    """Mark illustrative statistics with compact red warnings (not every cell)."""
    lines = content.decode('utf-8').splitlines()
    result = []
    for line in lines:
        if line.startswith('# 合成参考结果'):
            line = '# 🔴 ' + line[2:]
        elif line.startswith('> **本页所有'):
            line = line.replace('> **', '> 🔴 **', 1)
        elif line.startswith('## 研究任务消融') or line.startswith('## 假设故障注入') or line.startswith('## 口径与限制'):
            line = line.replace('## ', '## 🔴 ', 1)
        result.append(line)
    return ('\n'.join(result) + '\n').encode('utf-8')


# Aggregates belong to public results; raw per-task/per-fault observations
# can be regenerated privately if needed, but are not committed to GitHub.
PUBLIC_FILES = (
    'e2e_summary.synthetic.json',
    'ablation_summary.synthetic.csv',
    'paired_comparisons.synthetic.json',
    'domain_breakdown.synthetic.csv',
    'fault_summary.synthetic.json',
    'manifest.synthetic.json',
    'README.md',
)


def generate() -> dict[str, bytes]:
    tasks = read_jsonl(TASKS)
    faults_catalog = read_jsonl(FAULTS)
    if len(tasks)!=60 or len(faults_catalog)!=40 or len({t['id'] for t in tasks})!=len(tasks):
        raise ValueError('public benchmark dataset version changed; update manifest expectations')
    rows = task_rows(tasks)
    faults = fault_rows(faults_catalog)
    summary = full_summary(rows, faults, faults_catalog)
    table_data, table_rows = ablation_csv(summary)
    paired = {variant: paired_deltas(rows,VARIANTS[0],variant) for variant in VARIANTS[1:]}
    paired = {'result_kind': KIND, 'data_origin':'synthetic_example', 'pairs':paired}
    return {
        'task_runs.synthetic.jsonl': lines_bytes(rows),
        'e2e_summary.synthetic.json': jbytes(summary),
        'ablation_summary.synthetic.csv': table_data,
        'paired_comparisons.synthetic.json': jbytes(paired),
        'domain_breakdown.synthetic.csv': domain_csv(rows),
        'fault_observations.synthetic.jsonl': lines_bytes(faults),
        'fault_summary.synthetic.json': jbytes(summary['faults']),
        'manifest.synthetic.json': jbytes(manifest()),
        'README.md': highlight_reference_readme(overview_md(summary, table_rows)),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', action='store_true', help='verify committed reference files without writing them')
    parser.add_argument('--output-root', type=Path, default=TARGET, help='output directory; useful for testing')
    parser.add_argument('--raw-output', type=Path, help='optional local, ignored directory for detailed records')
    args = parser.parse_args(argv)
    generated = generate()
    expected = {k:generated[k] for k in PUBLIC_FILES}
    if args.verify:
        for name, payload in expected.items():
            path = args.output_root/name
            if not path.is_file() or path.read_bytes()!=payload:
                raise SystemExit(f'FAIL: synthetic example is stale or missing: {path}')
        print(f'OK: verified {len(expected)} synthetic example files (NO live metrics)')
        return 0
    args.output_root.mkdir(parents=True, exist_ok=True)
    for name, payload in expected.items():
        (args.output_root/name).write_bytes(payload)
    if args.raw_output is not None:
        safe_parent = (ROOT/'artifacts').resolve()
        target = args.raw_output.resolve()
        if safe_parent not in target.parents:
            raise SystemExit('raw-output must be inside ignored artifacts/')
        target.mkdir(parents=True,exist_ok=True)
        for name in ('task_runs.synthetic.jsonl','fault_observations.synthetic.jsonl'):
            (target/name).write_bytes(generated[name])
        print(f'Wrote private row-level data to {target}')
    print(f'Wrote {len(expected)} ILLUSTRATIVE aggregate files to {args.output_root}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
