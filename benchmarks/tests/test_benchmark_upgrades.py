"""Offline contract tests; no paid providers or Chroma installation required."""
from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.analyze_trials import paired_analysis
from benchmarks.evaluate_report_quality import apply_reviews, make_templates
from benchmarks.evaluate_runs import aggregate, paired_deltas, validate
from benchmarks.merge_runs import merge
from benchmarks.run_chroma_retrieval import labels_origin, load_fixture, score_queries
from benchmarks.run_live import run_task


class BenchmarkUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.report = 'Aspect A is documented. Aspect B is documented. Aspect C is documented. Aspect D is documented. Aspect E is documented. Source one: https://a.example/test. Source two: https://b.example/doc.'
        self.report_path = Path(self.tmp.name) / 'report.md'
        self.report_path.write_text(self.report, 'utf-8')
        self.hash = hashlib.sha256(self.report.encode('utf-8')).hexdigest()
        self.task = {'id': 'research-1', 'query': 'Test', 'expected_aspects': ['A', 'B', 'C', 'D', 'E'],
                     'min_independent_sources': 2}
        self.row = {'task_id': 'research-1', 'variant': 'memory_off', 'trial_id': '1',
                    'run_id': 'run-1', 'data_origin': 'real_runtime', 'status': 'completed',
                    'report_file': str(self.report_path), 'report_sha256': self.hash,
                    'latency_seconds': 5.0, 'task_query_sha256': 'query123',
                    'config_sha256': 'config-base'}
        self.audit = {
            'task_id': 'research-1', 'variant': 'memory_off', 'trial_id': '1',
            'run_id': 'run-1', 'report_sha256': self.hash,
            'reviewer_id': 'reviewer-1', 'review_method': 'human', 'reviewed_at': '2026-10-10T12:00:00Z',
            'aspect_reviews': [{'aspect': ch, 'covered': ch != 'E', 'evidence_excerpt': f'Aspect {ch} is documented.' if ch != 'E' else ''}
                               for ch in ['A', 'B', 'C', 'D', 'E']],
            'source_reviews': [
                {'url': 'https://a.example/test', 'independent_group': 'org-a',
                 'verified': True, 'verification_note': 'Checked source and claim'},
                {'url': 'https://b.example/doc', 'independent_group': 'org-b',
                 'verified': True, 'verification_note': 'Checked independent primary source'},
            ],
            'claims_audited': 10, 'unsupported_claims': 1,
            'citations_audited': 10, 'supported_citations': 9,
            'critical_factual_error': False,
        }

    def test_quality_pass_and_aggregate(self):
        rows, stats = apply_reviews([self.row], [self.task], [self.audit])
        self.assertEqual(stats['reviewed_runs'], 1)
        self.assertTrue(rows[0]['reviewed_success'])
        self.assertEqual(rows[0]['aspect_coverage'], 0.8)
        self.assertEqual(rows[0]['quality_provenance'], 'independent_human_review')
        summary = aggregate(rows)['variants']['memory_off']
        self.assertEqual(summary['reviewed_success_rate'], 1.0)
        self.assertEqual(summary['source_adequacy_pass_rate'], 1.0)
        self.assertEqual(summary['unsupported_claim_rate']['denominator'], 10)

    def test_unreviewed_is_null_not_success(self):
        enriched, stats = apply_reviews([self.row], [self.task], [])
        self.assertIsNone(enriched[0].get('reviewed_success'))
        self.assertIsNone(aggregate(enriched)['variants']['memory_off']['reviewed_success_rate'])
        self.assertEqual(stats['status'], 'PARTIAL')
        templates = make_templates([self.row], [self.task])
        self.assertTrue(templates[0]['template_only_not_reviewed'])
        self.assertTrue(all(x['covered'] is None for x in templates[0]['aspect_reviews']))
        with self.assertRaisesRegex(ValueError, 'not an actual review'):
            apply_reviews([self.row], [self.task], templates)

    def test_quality_failure_on_bad_coverage_and_sources(self):
        audit = copy.deepcopy(self.audit)
        audit['aspect_reviews'][3]['covered'] = False
        audit['source_reviews'][1]['independent_group'] = 'org-a'  # two domains, one independent owner
        enriched, _ = apply_reviews([self.row], [self.task], [audit])
        self.assertFalse(enriched[0]['reviewed_success'])
        self.assertFalse(enriched[0]['source_adequacy_pass'])
        self.assertEqual(enriched[0]['independent_sources_verified'], 1)

    def test_quality_hash_and_excerpt_must_match_report(self):
        altered = copy.deepcopy(self.audit)
        altered['report_sha256'] = 'x' * 64
        with self.assertRaisesRegex(ValueError, 'report hash'):
            apply_reviews([self.row], [self.task], [altered])
        altered = copy.deepcopy(self.audit)
        altered['aspect_reviews'][0]['evidence_excerpt'] = 'not in the report'
        with self.assertRaisesRegex(ValueError, 'exact excerpt'):
            apply_reviews([self.row], [self.task], [altered])
        self.report_path.write_text('a changed report', 'utf-8')
        with self.assertRaisesRegex(ValueError, 'hash does not match'):
            apply_reviews([self.row], [self.task], [self.audit])

    def test_quality_rejects_zero_denominator_and_incomplete_labels(self):
        audit = copy.deepcopy(self.audit)
        audit['citations_audited'] = 0
        audit['supported_citations'] = 0
        with self.assertRaisesRegex(ValueError, 'citations_audited'):
            apply_reviews([self.row], [self.task], [audit])
        audit = copy.deepcopy(self.audit)
        audit['aspect_reviews'][0]['covered'] = None
        with self.assertRaisesRegex(ValueError, 'bool'):
            apply_reviews([self.row], [self.task], [audit])
        with self.assertRaisesRegex(ValueError, 'duplicate review'):
            apply_reviews([self.row], [self.task], [self.audit, self.audit])

    def test_trial_keys_and_config_drift(self):
        rows = []
        for variant in ('memory_off', 'stage_plus_episodic'):
            for trial in ('1', '2', '3'):
                rows.append({'task_id': 'task-a', 'variant': variant, 'trial_id': trial,
                             'data_origin': 'fake_runtime', 'status': 'completed',
                             'config_sha256': 'hash-' + variant, 'task_query_sha256': 'q1',
                             'latency_seconds': 10 if variant == 'memory_off' else 8})
        self.assertEqual(aggregate(rows)['variants']['memory_off']['n_distinct_trials'], 3)
        self.assertEqual(paired_deltas(rows, 'memory_off', 'stage_plus_episodic')['matched_run_pairs'], 3)
        with self.assertRaisesRegex(ValueError, 'duplicate measurement'):
            validate(rows + [rows[0]])
        modified = copy.deepcopy(rows)
        modified[1]['config_sha256'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'config drift'):
            validate(modified)
        modified = copy.deepcopy(rows)
        modified[-1]['task_query_sha256'] = 'different task text'
        with self.assertRaisesRegex(ValueError, 'task query drift'):
            validate(modified)

    def test_bootstrap_deterministic_and_missing_pairs(self):
        rows = []
        for task in ('a', 'b', 'c', 'd'):
            for trial in ('1', '2', '3'):
                for variant in ('memory_off', 'stage_plus_episodic'):
                    rows.append({'task_id': task, 'variant': variant, 'trial_id': trial,
                                 'data_origin': 'fake_runtime', 'status': 'completed',
                                 'reviewed_success': None,  # MUST remain unknown without review
                                 'latency_seconds': 25 if variant == 'memory_off' else 20,
                                 'search_calls': 12 if variant == 'memory_off' else 10})
        first = paired_analysis(rows, 'memory_off', 'stage_plus_episodic', bootstrap=100)
        second = paired_analysis(rows, 'memory_off', 'stage_plus_episodic', bootstrap=100)
        self.assertEqual(first, second)
        self.assertEqual(first['matched_unique_tasks'], 4)
        self.assertEqual(first['matched_run_pairs'], 12)
        self.assertIsNone(first['metrics']['reviewed_success']['bootstrap_95pct_ci'])
        self.assertEqual(first['metrics']['latency_seconds']['task_equal_weight_candidate_minus_baseline'], -5)
        self.assertEqual(first['metrics']['latency_seconds']['bootstrap_95pct_ci'], [-5, -5])
        dropped = rows[:-1]  # one candidate is missing
        result = paired_analysis(dropped, 'memory_off', 'stage_plus_episodic', bootstrap=100)
        self.assertEqual(result['matched_run_pairs'], 11)
        self.assertEqual(result['unmatched_baseline_runs'], 1)
        with self.assertRaisesRegex(ValueError, 'missing config_sha256'):
            paired_analysis(rows, 'memory_off', 'stage_plus_episodic', bootstrap=100,
                            require_config_snapshots=True)

    def test_chroma_wiring_and_label_provenance(self):
        class Store:
            def __init__(self):
                self.calls = []
            def get_memory(self, ident):
                return {'id': ident} if ident == 'doc1' else None
            def search_memory(self, q, top_k):
                self.calls.append('search_memory')
                return [{'id': 'doc1', 'content': 'A query relevant',
                         'metadata': {'structured_status': 'complete', 'report_id': 'r1'}}]
            def search_keyword(self, q, top_k):
                self.calls.append('search_keyword')
                return [{'id': 'doc1', 'content': 'A query relevant',
                         'metadata': {'structured_status': 'complete', 'report_id': 'r1'}}]
            def count(self):
                return 1
        store = Store()
        summary, rows = score_queries(store, [{'id': 'q1', 'query': 'A query',
                                               'relevant_ids': ['doc1'], 'synthetic': True}], k=10)
        self.assertEqual(store.calls, ['search_memory', 'search_keyword'])
        self.assertEqual(summary['labels_origin'], 'synthetic_fixture')
        self.assertEqual(summary['metrics_by_variant']['chroma_hybrid_rrf']['recall_at_5'], 1.0)
        self.assertEqual(len(rows), 3)
        with self.assertRaisesRegex(ValueError, 'not present'):
            score_queries(store, [{'id': 'q1', 'query': 'A query',
                                   'relevant_ids': ['not-found'], 'synthetic': True}])
        with self.assertRaisesRegex(ValueError, 'mix'):
            labels_origin([{'synthetic': True}, {'label_origin': 'human_independent'}])

    def test_live_task_links_hash_and_artifact(self):
        task = {'id': 'task-a', 'query': 'the same stable question'}
        def mocked(url, payload=None, timeout=15):
            if url.endswith('/start'):
                return {'thread_id': 'thread-1'}
            if url.endswith('/status'):
                return {'status': 'completed'}
            if url.endswith('/report'):
                return {'final_report': '# Report\nAnswer'}
            raise AssertionError(url)
        with patch('benchmarks.run_live.request_json', side_effect=mocked):
            row = run_task('http://localhost', task, 'memory_off', 0.01, 1,
                           'fake', trial='4', reports_dir=Path(self.tmp.name),
                           config_sha256='abc')
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['trial_id'], '4')
        self.assertEqual(row['data_origin'], 'fake_runtime')
        self.assertTrue(Path(row['report_file']).exists())
        self.assertEqual(row['report_sha256'], hashlib.sha256('# Report\nAnswer'.encode()).hexdigest())
        self.assertIsNone(row['reviewed_success'])
        self.assertTrue(row['config_attestation'].startswith('operator_supplied'))


if __name__ == '__main__':
    unittest.main()
