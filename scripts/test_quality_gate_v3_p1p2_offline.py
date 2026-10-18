"""P1/P2 stdlib-only tests; run with python scripts/test_quality_gate_v3_p1p2_offline.py -q."""
from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deep_research import quality_gate as q
from deep_research.evidence import attribute_sources, collect_provider_sources, merge_evidence
from deep_research.research_seed import build_speculative_payload, ResearchFenceError
from benchmarks.quality_gate.evaluate import compare, load_jsonl, summarize


def receipt(url: str, excerpt: str, aspect: str = "aspect-01", generation: int = 1):
    rows = collect_provider_sources({url: {"content": excerpt, "title": "Provider result"}})
    return attribute_sources(rows, aspect_id=aspect,
                             valid_aspects=[{"aspect_id": aspect}], generation=generation)[0]


def state(aspects: int = 2, records: int = 3, notes: int = 30):
    dims = [{"aspect_id": f"aspect-{i:02d}", "title": f"Area {i}",
             "min_sources": 1, "max_age_days": None} for i in range(1, aspects + 1)]
    from deep_research.evidence import checklist_fingerprint
    digest = checklist_fingerprint(dims)
    entries = [receipt(f"https://source{i}.example.com/item", f"Excerpt {i} structured evidence.",
                       aspect=f"aspect-{i % aspects + 1:02d}") for i in range(records)]
    brief = "Detailed research brief"
    return {
        "quality_gate_version": 3, "research_generation": 1,
        "research_brief": brief, "required_aspects": dims,
        "checklist_fingerprint": digest, "checklist_status": "proposed",
        "speculative_research": build_speculative_payload(
            generation=1, research_brief=brief,
            notes=[f"Research finding {i}: " + "context " * 40 for i in range(notes)],
            evidence_records=entries, checklist_fingerprint=digest,
        ),
        "quality_repair_count": 2, "quality_repair_limit": 2,
        "manual_research_count": 1, "manual_research_limit": 3,
        "quality_repair_history": [{"source": "auto", "round": 1,
                                    "new_evidence_count": 0, "new_records_count": 2}],
        "quality_gate_decision": {"action": "human_review", "reason": "no_new_evidence"},
        "quality_review": {"status": "completed", "generation": 1,
                           "context_scope": {"notes_omitted": 18}, "issues": []},
        "red_team_review": {"status": "skipped", "reason": "no_high_risk_trigger",
                            "generation": 1, "passed": None},
    }


class MergeTests(unittest.TestCase):
    def test_same_publisher_another_url_new_content_is_not_new_coverage(self):
        old = [receipt("https://news.example.com/one", "Evidence A")]
        new = [receipt("https://news.example.com/two", "Evidence B")]
        merged, gain = merge_evidence(old, new)
        self.assertEqual(len(merged), 2)
        self.assertEqual(gain, 0)

    def test_changed_excerpt_from_same_url_retained_but_not_novel_publisher(self):
        old = [receipt("https://news.example.com/one", "Evidence A")]
        merged, gain = merge_evidence(old, [receipt("https://news.example.com/one", "Updated evidence B")])
        self.assertEqual(len(merged), 2)
        self.assertEqual(gain, 0)

    def test_space_and_unicode_only_rewrite_is_not_new_record(self):
        old = [receipt("https://data.example.com/a", "Data about price 9")]
        rewritten = receipt("https://data.example.com/b", "Data  about   price ９")
        merged, gain = merge_evidence(old, [rewritten])
        self.assertEqual(len(merged), 1)
        self.assertEqual(gain, 0)

    def test_new_domain_same_aspect_increments_coverage(self):
        old = [receipt("https://data.example.com/a", "A measured fact")]
        more = [receipt("https://journal.example.net/other", "Another measured fact")]
        merged, gain = merge_evidence(old, more)
        self.assertEqual((len(merged), gain), (2, 1))

    def test_unassigned_or_malformed_records_do_not_inflate_coverage(self):
        old = [receipt("https://data.example.com/a", "Evidence A")]
        bad = receipt("https://fake.example.net/a", "Evidence B")
        bad["source_status"] = "not_observed"
        unassigned = receipt("https://real.example.net/a", "Evidence C")
        unassigned["aspect_id"] = None
        unassigned["assignment_method"] = "unassigned"
        merged, gain = merge_evidence(old, [bad, unassigned])
        self.assertEqual(gain, 0)
        self.assertEqual(len(merged), 2)

    def test_out_of_order_generation_is_refused(self):
        old = [receipt("https://data.example.com/a", "Evidence A")]
        new = receipt("https://journal.example.net/a", "Evidence B", generation=10)
        merged, gain = merge_evidence(old, [new])
        # Payload generation is still protected by checked_candidate_evidence.
        self.assertEqual(gain, 1)
        s = state(aspects=1)
        s["speculative_research"]["evidence_records"] = merged
        with self.assertRaises(ResearchFenceError):
            q.checked_candidate_evidence(s)


class ScopeTests(unittest.TestCase):
    def test_all_dimensions_have_heading_and_excerpt_sample(self):
        s = state(aspects=6, records=60, notes=30)
        notes, manifest, scope = q.build_review_context(s)
        self.assertTrue(len(manifest) <= 8000)
        self.assertTrue(scope["manifest_limited"])
        self.assertEqual(scope["total_aspects"], 6)
        for dimension in s["required_aspects"]:
            self.assertIn(dimension["aspect_id"], manifest)
        self.assertEqual(scope["notes_in_context"], 12)
        self.assertIn("Finding 30/30", notes)
        self.assertIn("审查上下文局限", notes)
        self.assertEqual(scope["evidence_total"], 60)

    def test_empty_evidence_or_notes_is_explicit(self):
        s = state(aspects=3, records=0, notes=0)
        notes, manifest, scope = q.build_review_context(s)
        self.assertEqual(scope["aspect_ids_without_records"],
                         ["aspect-01", "aspect-02", "aspect-03"])
        self.assertIn("本轮无研究笔记", notes)
        self.assertEqual(scope["evidence_in_context"], 0)
        self.assertIn("无来源记录", manifest)

    def test_hard_check_is_full_ledger_not_context_sample(self):
        s = state(aspects=1, records=70, notes=50)
        _, _, scope = q.build_review_context(s)
        coverage = q.check_evidence_coverage(s)
        self.assertTrue(scope["evidence_omitted"] > 0)
        self.assertEqual(coverage["candidate_evidence_count"], 70)
        self.assertFalse(coverage["missing"])

    def test_small_manifest_budget_rejected_not_silent(self):
        with self.assertRaisesRegex(ValueError, "budget"):
            q.build_review_context(state(), manifest_budget=100)

    def test_review_summary_visible_and_no_fake_pass(self):
        s = state()
        s["evidence_coverage"] = q.check_evidence_coverage(s)
        summary = q.build_human_review_summary(s)
        self.assertTrue(summary["checklist_requires_confirmation"])
        self.assertEqual(summary["decision_reason"], "no_new_evidence")
        self.assertEqual(summary["red_team_status"], "skipped")
        self.assertIn("new_evidence_count", summary["latest_repairs"][-1])
        self.assertNotIn("excerpt", json.dumps(summary))
        self.assertIsNone(summary["reviewer_scope"]["red_team"])


class BenchmarkTests(unittest.TestCase):
    def test_example_comparison_is_explicitly_synthetic(self):
        d = ROOT / 'benchmarks/quality_gate/fixtures'
        result = compare(d/'gold.synthetic.jsonl', d/'v3.synthetic.jsonl', d/'baseline.synthetic.jsonl')
        self.assertEqual(result['status'], 'synthetic_smoke_only')
        self.assertEqual(result['candidate']['cases'], 5)
        self.assertEqual(result['candidate']['evidence_gap_recall'], 1.0)
        self.assertEqual(result['baseline']['evidence_gap_recall'], 0.5)
        self.assertEqual(result['candidate']['unnecessary_research_rate'], 0.0)
        self.assertIsNone(result['candidate']['average_cost_usd'])
        self.assertNotIn('average_cost_usd', result['delta_candidate_minus_baseline'])

    def test_mismatched_case_ids_and_duplicates_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a = root / 'a.jsonl'
            b = root / 'b.jsonl'
            a.write_text('{"case_id":"X","requires_new_evidence":true,"red_team_expected":false}\n')
            b.write_text('{"case_id":"Y","flagged_gap":true}\n')
            with self.assertRaisesRegex(ValueError, 'case_id mismatch'):
                compare(a, b)
            a.write_text('{"case_id":"X"}\n{"case_id":"X"}\n')
            with self.assertRaisesRegex(ValueError, 'duplicate case_id'):
                load_jsonl(a)

    def test_missing_cost_cannot_be_counted_as_zero(self):
        truth = {"x": {"case_id":"x", "requires_new_evidence":False,
                       "red_team_expected":False}}
        run = {"x": {"case_id":"x", "flagged_gap":False,
                     "targeted_research_called":False,
                     "red_team_called":False, "human_escalated":True}}
        result = summarize(truth, run)
        self.assertIsNone(result["average_cost_usd"])
        self.assertIsNone(result["evidence_gap_recall"])
        self.assertIsNone(result["red_team_trigger_recall"])

    def test_invalid_numeric_fields_and_missing_bool_rejected(self):
        truth = {"x": {"case_id":"x", "requires_new_evidence":False,
                       "red_team_expected":False}}
        run = {"x": {"case_id":"x", "flagged_gap":False,
                     "targeted_research_called":False,
                     "red_team_called":False, "human_escalated":True,
                     "cost_usd":-1}}
        with self.assertRaisesRegex(ValueError, 'cost_usd'):
            summarize(truth, run)
        del run["x"]["cost_usd"]
        del run["x"]["flagged_gap"]
        with self.assertRaisesRegex(ValueError, 'flagged_gap'):
            summarize(truth, run)


class RemovalTests(unittest.TestCase):
    def test_only_v3_graph_nodes_are_defined(self):
        module = ast.parse((ROOT / 'deep_research/agent_builder.py').read_text())
        functions = {node.name for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for deprecated in ('regenerate_research', 'accept_research', 'route_quality_gate'):
            self.assertNotIn(deprecated, functions)
        for current in ('precheck_quality_gate', 'route_quality_gate_v3', 'targeted_research'):
            self.assertIn(current, functions)

    def test_no_version_specific_legacy_nodes_or_version_flags(self):
        graph = (ROOT/'deep_research/agent_builder.py').read_text()
        self.assertNotIn('QUALITY_GATE_V2_VERSION', graph)
        self.assertNotIn('QUALITY_GATE_V3_ENV', graph)
        qpolicy = (ROOT/'deep_research/quality_gate.py').read_text()
        self.assertNotIn('DR_QUALITY_GATE_V1', qpolicy)
        self.assertNotIn('DR_QUALITY_GATE_V2', qpolicy)
        self.assertEqual(q.quality_gate_version_for_new_job(), 3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
