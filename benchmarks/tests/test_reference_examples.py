"""Validate internally coherent, explicitly synthetic E2E/reference result fixtures."""
from __future__ import annotations
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.evaluate_faults import evaluate as evaluate_faults
from benchmarks.evaluate_runs import paired_deltas, read_rows
from benchmarks.fixtures.build_full_reference_results import generate, full_summary, VARIANTS, PUBLIC_FILES
from benchmarks.run_offline import read_jsonl

D = ROOT/'results/examples/reference_v1'
SOURCE = 'ILLUSTRATIVE_SYNTHETIC_EXAMPLE'

class ReferenceExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        generated=generate()
        cls.runs=[json.loads(line) for line in generated['task_runs.synthetic.jsonl'].decode('utf-8').splitlines()]
        cls.faults=[json.loads(line) for line in generated['fault_observations.synthetic.jsonl'].decode('utf-8').splitlines()]
        cls.scenarios=read_jsonl(ROOT/'benchmarks/datasets/fault_scenarios.v1.jsonl')
        cls.summary=json.loads((D/'e2e_summary.synthetic.json').read_text('utf-8'))

    def test_four_ablation_variants_cover_same_sixty_tasks(self):
        self.assertEqual(len(self.runs),240)
        ids=set(r['task_id'] for r in self.runs)
        self.assertEqual(len(ids),60)
        self.assertEqual({r['variant'] for r in self.runs},set(VARIANTS))
        self.assertEqual(Counter(r['variant'] for r in self.runs),dict.fromkeys(VARIANTS,60))
        self.assertEqual(len({(r['variant'],r['task_id']) for r in self.runs}),240)

    def test_run_origins_are_explicitly_synthetic(self):
        self.assertTrue(all(r['data_origin']=='synthetic_example' and
                            r['result_kind']==SOURCE and 'FICTIONAL' in r['note'] for r in self.runs))
        self.assertEqual(self.summary['result_kind'],SOURCE)
        self.assertTrue(self.summary['quality_review_is_hypothetical'])
        self.assertTrue(self.summary['cost_is_hypothetical'])

    def test_aggregate_recomputes_from_raw_rows(self):
        rerun=full_summary(self.runs,self.faults,self.scenarios)
        self.assertEqual(rerun,self.summary)
        for v in self.summary['variants'].values():
            self.assertEqual(v['reviewed_task_count'],60)
            self.assertEqual(v['n_tasks'],60)
            self.assertIsNotNone(v['unsupported_claim_rate']['rate'])
            self.assertIsNotNone(v['supported_citation_rate']['rate'])
            self.assertIsNotNone(v['avg_cost_rmb'])
            self.assertIsNotNone(v['stale_fact_error_rate'])

    def test_failures_not_reported_as_hypothetical_success(self):
        for row in self.runs:
            if row['reviewed_success']:
                self.assertEqual(row['status'],'completed')
            if row['status']!='completed':
                self.assertIsNone(row['claims_audited'])
                self.assertIsNone(row['citations_audited'])
                self.assertIsNone(row['temporal_facts_audited'])
                self.assertIsNone(row['stale_fact_errors'])
            else:
                self.assertLessEqual(row['unsupported_claims'],row['claims_audited'])
                self.assertLessEqual(row['supported_citations'],row['citations_audited'])
                self.assertLessEqual(row['stale_fact_errors'],row['temporal_facts_audited'])

    def test_accounting_identity_for_tokens_prices_and_tools(self):
        p=self.summary['price_assumptions']
        for row in self.runs:
            self.assertEqual(row['total_tokens'],row['input_tokens']+row['output_tokens'])
            expected=(row['input_tokens']*p['input_rmb_per_million_tokens']/1_000_000+
                      row['output_tokens']*p['output_rmb_per_million_tokens']/1_000_000+
                      row['search_calls']*p['search_rmb_per_call'])
            self.assertAlmostEqual(row['cost_rmb'],expected,places=5)
            self.assertTrue(0<=row['tool_successes']<=row['tool_calls'])
            self.assertEqual(row['context_overflow_count'],0)

    def test_fault_summary_correctly_synthetic_and_failure_retained(self):
        s=evaluate_faults(self.scenarios,self.faults)
        self.assertEqual(s['result_kind'],SOURCE)
        self.assertEqual(s,json.loads((D/'fault_summary.synthetic.json').read_text('utf-8')))
        self.assertEqual((s['planned_cases'],s['observed_cases'],s['recovered_count']),(40,40,39))
        self.assertEqual((s['lost_jobs'],s['duplicate_writes'],s['idempotency_violations']),(0,0,0))
        self.assertEqual(s['manual_intervention_count'],1)
        self.assertEqual(len([f for f in self.faults if f['recovery_seconds'] is None]),1)

    def test_no_mixing_synthetic_and_real_fault_observations(self):
        data=[dict(self.faults[0]),dict(self.faults[1])]
        data[0]['data_origin']='real_runtime'
        with self.assertRaises(ValueError):
            evaluate_faults(self.scenarios,data)

    def test_paired_deltas_recomputed(self):
        stored=json.loads((D/'paired_comparisons.synthetic.json').read_text('utf-8'))
        self.assertEqual(stored['result_kind'],SOURCE)
        for candidate in VARIANTS[1:]:
            recalculated=paired_deltas(self.runs,'memory_off',candidate)
            self.assertEqual(stored['pairs'][candidate],recalculated)
            self.assertEqual(recalculated['matched_task_count'],60)

    def test_csv_rounded_values_match_machine_summary(self):
        with (D/'ablation_summary.synthetic.csv').open('r',encoding='utf-8-sig',newline='') as f:
            table=list(csv.DictReader(f))
        self.assertEqual(len(table),4)
        for row in table:
            v=self.summary['variants'][row['variant']]
            self.assertEqual(int(row['tasks']),v['n_tasks'])
            self.assertEqual(int(row['completed']),v['completed_tasks'])
            self.assertAlmostEqual(float(row['search_calls_per_task']),v['avg_search_calls'],places=3)
            self.assertAlmostEqual(float(row['cost_rmb_per_task_hypothetical']),v['avg_cost_rmb'],places=3)
            self.assertEqual(row['data_origin'],'synthetic_example')

    def test_reproducible_generator_and_checksum_manifest(self):
        files=generate()
        self.assertEqual(set(PUBLIC_FILES),{p.name for p in D.iterdir() if p.is_file()})
        for name in PUBLIC_FILES:
            content=files[name]
            self.assertEqual((D/name).read_bytes(),content,name)

    def test_large_details_are_not_published(self):
        self.assertFalse((D/'task_runs.synthetic.jsonl').exists())
        self.assertFalse((D/'fault_observations.synthetic.jsonl').exists())
        self.assertIn('task_runs.synthetic.jsonl',generate())

    def test_generator_verify_exits_cleanly(self):
        result=subprocess.run([sys.executable,str(ROOT/'benchmarks/fixtures/build_full_reference_results.py'),
            '--verify'], cwd=ROOT, capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_generator_rejects_tampered_csv(self):
        with tempfile.TemporaryDirectory() as tempdir:
            directory=Path(tempdir)
            for name,body in generate().items():
                if name not in PUBLIC_FILES:
                    continue
                (directory/name).write_bytes(body)
            (directory/'ablation_summary.synthetic.csv').write_text('bad\n','utf-8')
            result=subprocess.run([sys.executable,str(ROOT/'benchmarks/fixtures/build_full_reference_results.py'),
                '--verify','--output-root',str(directory)],cwd=ROOT,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)

if __name__=='__main__':
    unittest.main()
