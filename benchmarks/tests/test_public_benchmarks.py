"""No-network, no-model tests for public benchmark contracts."""
from __future__ import annotations
import copy
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from benchmarks import metrics
from benchmarks.evaluate_runs import aggregate, paired_deltas, read_rows
from benchmarks.evaluate_faults import evaluate as eval_faults
from benchmarks.import_runtime_metrics import import_metrics
from benchmarks.merge_runs import merge
from benchmarks.evaluate_citations import evaluate as eval_citations
from benchmarks.publish_offline import build_publication, canonical
from benchmarks.run_offline import read_jsonl
from benchmarks.retrievers import OfflineIndex, bm25_terms, VARIANTS

DATA=ROOT/'benchmarks'/'datasets'


class PublicBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from benchmarks.fixtures.build_public_datasets import build
        build()  # deterministic local fixtures, ignored by Git
        cls.corpus=read_jsonl(DATA/'memory_corpus.v1.jsonl')
        cls.queries=read_jsonl(DATA/'memory_queries.v1.jsonl')
        cls.tasks=read_jsonl(DATA/'research_tasks.v1.jsonl')
        cls.faults=read_jsonl(DATA/'fault_scenarios.v1.jsonl')
        cls.citations=read_jsonl(DATA/'citation_contract.v1.jsonl')

    def test_fixed_dataset_sizes(self):
        self.assertEqual((len(self.corpus),len(self.queries),len(self.tasks),len(self.faults),len(self.citations)),
                         (360,120,60,40,32))

    def test_dataset_ids_unique_and_labelled(self):
        for collection in (self.corpus,self.queries,self.tasks,self.faults,self.citations):
            self.assertEqual(len(collection),len({row['id'] for row in collection}))
        ids={row['id'] for row in self.corpus}
        for query in self.queries:
            self.assertTrue(set(query['relevant_ids']).issubset(ids))
            self.assertTrue(set(query['hard_negative_ids']).issubset(ids))
            self.assertTrue(set(query['hard_negative_ids']).isdisjoint(query['relevant_ids']))

    def test_no_real_world_sources_claimed(self):
        self.assertTrue(all(row['synthetic'] and '.example/' in row['source_url'] for row in self.corpus))
        self.assertTrue(all(row['status']=='prompt_only_no_run' for row in self.tasks))
        self.assertTrue(all(row['run_status']=='planned_not_executed' for row in self.faults))

    def test_bilingual_cases_exist(self):
        languages={row['language'] for row in self.queries}
        self.assertEqual(languages,{'en','zh'})
        self.assertTrue(bm25_terms('RRF \u8bb0\u5fc6\u68c0\u7d22'))

    def test_retrieval_definitions(self):
        result=metrics.retrieval_metrics(['a','b'],['c','a','b'])
        self.assertEqual(result['recall_at_1'],0)
        self.assertEqual(result['recall_at_3'],1)
        self.assertAlmostEqual(result['reciprocal_rank_at_10'],0.5)
        self.assertGreater(result['ndcg_at_10'],0)
        self.assertLessEqual(result['ndcg_at_10'],1)

    def test_invalid_retrieval_labels(self):
        with self.assertRaises(ValueError):
            metrics.retrieval_metrics([],[])
        with self.assertRaises(ValueError):
            metrics.retrieval_metrics(['x'],['a','a'])

    def test_percentile_definition(self):
        self.assertEqual(metrics.percentile([0,10],0.95),9.5)
        self.assertIsNone(metrics.percentile([],0.5))
        with self.assertRaises(ValueError):
            metrics.percentile([1],1.1)

    def test_offline_rank_reproducible(self):
        index=OfflineIndex(self.corpus)
        query=self.queries[0]['query']
        for variant in VARIANTS:
            self.assertEqual(index.rank(query,variant),index.rank(query,variant))
            self.assertEqual(len(set(index.rank(query,variant))),len(index.rank(query,variant)))

    def test_citation_contract_reality(self):
        summary,rows=eval_citations(self.citations)
        self.assertEqual(summary['sample_count'],32)
        self.assertEqual(summary['matches_expected'],32)
        self.assertEqual(summary['semantic_unsupported_cases_passing_static'],4)
        self.assertIsNone(summary['factual_grounding_rate'])

    def test_no_faults_no_recovery_rate(self):
        result=eval_faults(self.faults,[])
        self.assertEqual(result['result_kind'],'NOT_EXECUTED')
        self.assertIsNone(result['recovery_rate'])
        self.assertIsNone(result['lost_jobs'])

    def test_observed_fault_counts(self):
        r=eval_faults(self.faults,[{'scenario_id':self.faults[0]['id'],
                         'recovered':False,'lost_jobs':1,'duplicate_writes':0,'recovery_seconds':8}])
        self.assertEqual(r['observed_cases'],1)
        self.assertEqual(r['recovery_rate'],0)
        self.assertEqual(r['lost_jobs'],1)

    def test_bad_fault_rejected(self):
        with self.assertRaises(ValueError):
            eval_faults(self.faults,[{'scenario_id':self.faults[0]['id'],
                         'recovered':True,'lost_jobs':-1,'duplicate_writes':0}])

    def test_synthetic_trace_result_is_explicit(self):
        path=ROOT/'results'/'examples'/'synthetic_task_runs.example.jsonl'
        rows=read_rows(path)
        summary=aggregate(rows)
        self.assertEqual(summary['result_kind'],'ILLUSTRATIVE_SYNTHETIC_EXAMPLE')
        self.assertIsNone(summary['variants']['memory_off']['reviewed_success_rate'])
        self.assertIsNone(summary['variants']['memory_off']['unsupported_claim_rate']['rate'])

    def test_fake_runtime_not_labelled_live_provider(self):
        rows=read_rows(ROOT/'results'/'examples'/'synthetic_task_runs.example.jsonl')
        modified=[{**row,'data_origin':'fake_runtime'} for row in rows]
        self.assertEqual(aggregate(modified)['result_kind'],'FAKE_PROVIDER_RUNTIME')

    def test_origin_mixing_rejected(self):
        rows=read_rows(ROOT/'results'/'examples'/'synthetic_task_runs.example.jsonl')
        other=copy.deepcopy(rows)
        other[0]['data_origin']='real_runtime'
        with self.assertRaises(ValueError):
            aggregate(other)

    def test_duplicate_task_variant_rejected(self):
        rows=read_rows(ROOT/'results'/'examples'/'synthetic_task_runs.example.jsonl')
        with self.assertRaises(ValueError):
            aggregate(rows+[rows[0]])

    def test_invalid_metric_values_rejected(self):
        rows=read_rows(ROOT/'results'/'examples'/'synthetic_task_runs.example.jsonl')
        other=copy.deepcopy(rows)
        other[0]['claims_audited']=4
        with self.assertRaises(ValueError):
            aggregate(other)
        other=copy.deepcopy(rows)
        other[0]['search_calls']=1.2
        with self.assertRaises(ValueError):
            aggregate(other)

    def test_runtime_metric_import_requires_valid_identity(self):
        row={'task_id':'q001','variant':'base','data_origin':'real_runtime',
             'status':'completed','thread_id':'task-1','run_id':'run-1'}
        run={'run_id':'run-1','task_id':'task-1','status':'completed',
             'llm':{'calls':2,'input_tokens':400,'output_tokens':80,'success_without_usage':1},
             'search':{'calls':5},'cloud':{'estimated_cost_rmb':0.7}}
        imported,stats=import_metrics([row],[run])
        self.assertEqual(stats['matched_runs'],1)
        self.assertIsNone(imported[0].get('input_tokens'))
        self.assertEqual(imported[0]['search_calls'],5)
        self.assertIsNone(imported[0]['cost_rmb'])
        run['llm']['success_without_usage']=0
        imported,_=import_metrics([row],[run])
        self.assertEqual(imported[0]['input_tokens'],400)
        run['task_id']='someone-else'
        with self.assertRaises(ValueError):
            import_metrics([row],[run])

    def test_merge_rejects_duplicate_files(self):
        fixture=ROOT/'results'/'examples'/'synthetic_task_runs.example.jsonl'
        with self.assertRaises(ValueError):
            merge([fixture,fixture])

    def test_paired_deltas_are_matched(self):
        rows=read_rows(ROOT/'results'/'examples'/'synthetic_task_runs.example.jsonl')
        pair=paired_deltas(rows,'memory_off','stage_plus_episodic')
        self.assertEqual(pair['matched_task_count'],6)
        self.assertEqual(pair['paired_field_changes']['latency_seconds']['n_paired'],6)
        self.assertIsNone(pair['paired_field_changes']['cost_rmb']['relative_change_pct'])

    def test_committed_offline_results_match_computation(self):
        for name,content in build_publication().items():
            self.assertEqual((ROOT/'results'/'offline'/'v1'/name).read_text('utf-8'),canonical(content))

    def test_live_requires_opt_in_before_http(self):
        proc=subprocess.run([sys.executable,str(ROOT/'benchmarks'/'run_live.py'),
                            '--variant','test'],capture_output=True,text=True,timeout=10)
        self.assertNotEqual(proc.returncode,0)
        self.assertIn('--confirm-live',proc.stderr)

if __name__=='__main__':
    unittest.main()
