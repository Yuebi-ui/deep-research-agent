"""stdlib-only V2 policy / real node-body tests (no LangGraph dependencies).

Usage: python scripts/test_quality_gate_v2_offline.py

Loads the actual implementation of the three graph-node functions from the
source AST and runs them with lightweight stand-ins, so routing and fencing
logic can be tested even in a deliberately offline environment.
This is NOT a substitute for the genuine LangGraph checkpoint integration tests.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deep_research import quality_gate as q
from deep_research import research_seed as seed
from backend.schemas.requests import ReviewAction
from backend.domain.task import status_for_stage, TaskStatus


class Command:
    def __init__(self, goto=None, update=None):
        self.goto, self.update = goto, update or {}

    def __class_getitem__(cls, _):
        return cls


class HumanMessage:
    def __init__(self, content):
        self.content = content


class Logger:
    def info(self, *_args, **_kw):
        pass


class FakeSupervisor:
    def __init__(self):
        self.additional = []
        self.last_child = None
        self.force_wrong_gen = False

    async def ainvoke(self, child, config=None):
        self.last_child = (child, config)
        gen = child['research_generation']
        payload = seed.build_speculative_payload(
            generation=gen + int(self.force_wrong_gen),
            research_brief=child['research_brief'], notes=self.additional,
        )
        return {'speculative_research': payload, 'research_trace': [
            {'generation': gen, 'topic': 'focused'}
        ]}


class Writer:
    async def ainvoke(self, _):
        return types.SimpleNamespace(content='Updated Draft')


def extract_actual_nodes():
    filename = ROOT / 'deep_research/agent_builder.py'
    tree = ast.parse(filename.read_text(encoding='utf-8'))
    selected = {'route_quality_gate', 'targeted_research', 'human_review'}
    functions = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name in selected]
    assert len(functions) == 3
    fake_supervisor = FakeSupervisor()
    injected = {
        '__name__': '_offline_quality_gate_nodes',
        'AgentState': dict,
        'RunnableConfig': dict,
        'Any': object,
        'Command': Command,
        'HumanMessage': HumanMessage,
        'logger': Logger(),
        'checked_candidate_notes': q.checked_candidate_notes,
        'choose_research_repair': q.choose_research_repair,
        'active_generation': seed.active_generation,
        'auto_research_lineage': q.auto_research_lineage,
        'quality_gate_lineage': q.quality_gate_lineage,
        'speculative_lineage': seed.speculative_lineage,
        'ResearchFenceError': seed.ResearchFenceError,
        'merge_candidate_notes': q.merge_candidate_notes,
        'format_followup_request': q.format_followup_request,
        'build_research_seed': seed.build_research_seed,
        'seed_fingerprint': seed.seed_fingerprint,
        'render_seed_message': seed.render_seed_message,
        'build_speculative_payload': seed.build_speculative_payload,
        'fenced_accept_update': seed.fenced_accept_update,
        'supervisor_agent': fake_supervisor,
        'writer_model': Writer(),
        'interrupt': lambda _: {'action': 'approve'},
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(filename), 'exec'), injected)
    return injected, fake_supervisor


def state(*, needs=True, red=False, limit=2, used=0, gen=1):
    brief = 'DB evidence comparison'
    return {
        'quality_gate_version': 2, 'research_generation': gen,
        'research_brief': brief,
        'draft_report': 'A long draft to edit',
        'quality_repair_limit': limit, 'quality_repair_count': used,
        'manual_research_limit': 2, 'manual_research_count': 0,
        'quality_repair_history': [],
        'speculative_research': seed.build_speculative_payload(
            generation=gen, research_brief=brief, notes=['old evidence A', 'old evidence B']
        ),
        'quality_review': {
            'status': 'completed', 'generation': gen,
            'missing_aspects': ['pricing numbers', '  ', 'pricing numbers'],
            'need_more_research': needs,
        },
        'red_team_review': {
            'status': 'completed', 'generation': gen,
            'passed': not red,
            'feedback': 'Check contrary source on pricing' if red else 'PASS',
        },
    }


def extract_actual_builder():
    filename = ROOT / 'deep_research/agent_builder.py'
    tree = ast.parse(filename.read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_create_builder')

    class FakeStateGraph:
        def __init__(self, *_args, **_kw):
            self.nodes = {}
            self.edges = []

        def add_node(self, name, fn):
            self.nodes[name] = fn

        def add_edge(self, src, dst):
            self.edges.append((tuple(src) if isinstance(src, list) else src, dst))

    def noop(*_args, **_kwargs):
        return None

    names = (
        'write_research_brief', 'write_draft_report', 'supervisor_agent',
        'claim_verification', 'final_report_generation', 'evaluate_research',
        'red_team_review', 'human_review', 'regenerate_research', 'accept_research',
        'route_quality_gate', 'targeted_research', 'quality_gate_failed',
    )
    ns = {name: noop for name in names}
    ns.update({
        'StateGraph': FakeStateGraph, 'START': '__start__', 'END': '__end__',
        'AgentState': dict, 'AgentInputState': dict,
        'quality_gate_enabled': q.quality_gate_enabled,
        'quality_gate_version_for_new_job': q.quality_gate_version_for_new_job,
        'QUALITY_GATE_VERSION': q.QUALITY_GATE_VERSION,
    })
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(filename), 'exec'), ns)
    return ns['_create_builder']


class PurePolicyTests(unittest.TestCase):
    def test_enumerated_policy_never_overspends(self):
        from itertools import product
        examined = 0
        for limit in range(4):
            for used in range(limit + 1):
                for eval_status, red_status, wants, red_fails, stagnant in product(
                    ('completed', 'error'), ('completed', 'error'),
                    (False, True), (False, True), (False, True),
                ):
                    s = state(needs=wants, red=red_fails, limit=limit, used=used)
                    s['quality_review']['status'] = eval_status
                    s['red_team_review']['status'] = red_status
                    if stagnant:
                        s['quality_repair_history'] = [{'source': 'auto', 'new_notes_count': 0}]
                    result = q.choose_research_repair(s)
                    examined += 1
                    if result['action'] == 'targeted_research':
                        self.assertLess(used, limit)
                        self.assertEqual(eval_status, 'completed')
                        self.assertEqual(red_status, 'completed')
                        self.assertTrue(wants or red_fails)
                        self.assertFalse(stagnant)
                        self.assertTrue(1 <= len(result['topics']) <= 3)
                    else:
                        self.assertIn(result['reason'], {
                            'no_gap', 'no_actionable_topics', 'reviewer_unavailable',
                            'auto_budget_exhausted', 'no_new_evidence',
                        })
        self.assertGreater(examined, 0)

    def test_actual_builder_versioned_topologies(self):
        builder = extract_actual_builder()
        v2 = builder(with_hitl=True, speculative=True, quality_gate_version=2)
        self.assertIn('route_quality_gate', v2.nodes)
        self.assertIn('targeted_research', v2.nodes)
        self.assertNotIn('regenerate_research', v2.nodes)
        self.assertIn(('red_team_audit', 'route_quality_gate'), v2.edges)
        self.assertIn(('targeted_research', 'evaluate_research'), v2.edges)
        self.assertIn((('write_draft_report', 'supervisor_subgraph'), 'evaluate_research'), v2.edges)
        v2_auto = builder(with_hitl=False, speculative=True, quality_gate_version=2)
        self.assertIn('quality_gate_failed', v2_auto.nodes)
        self.assertNotIn('human_review', v2_auto.nodes)
        v1 = builder(with_hitl=True, speculative=True, quality_gate_version=1)
        self.assertIn('regenerate_research', v1.nodes)
        self.assertNotIn('targeted_research', v1.nodes)
        self.assertIn(('red_team_audit', 'human_review'), v1.edges)
        original = builder(with_hitl=True, speculative=True, quality_gate_version=0)
        self.assertNotIn('evaluate_research', original.nodes)
        # Removed serial graph must fail explicitly rather than silently reroute.
        with self.assertRaisesRegex(ValueError, 'serial graph was removed'):
            builder(with_hitl=True, speculative=False, quality_gate_version=2)
        self.assertNotIn(('write_draft_report', 'supervisor_subgraph'), v2.edges)

    def test_budget_lock_and_version_switch(self):
        with patch.dict(os.environ, {'DR_QUALITY_GATE_V2': 'on', 'DR_QUALITY_GATE_V1': 'off',
                                    'DR_MAX_AUTO_RESEARCH_ROUNDS': '999'}):
            self.assertEqual(q.quality_gate_version_for_new_job(), 2)
            self.assertEqual(q.initial_repair_limits()['quality_repair_limit'], 3)
        with patch.dict(os.environ, {'DR_QUALITY_GATE_V2': 'off', 'DR_QUALITY_GATE_V1': 'on'}):
            self.assertEqual(q.quality_gate_version_for_new_job(), 1)
        with patch.dict(os.environ, {'DR_QUALITY_GATE_V2': 'off', 'DR_QUALITY_GATE_V1': 'off'}):
            self.assertEqual(q.quality_gate_version_for_new_job(), 0)
        self.assertTrue(q.quality_gate_lineage(state()))
        self.assertTrue(q.auto_research_lineage(state()))

    def test_policy_stops_on_model_failure_and_v1(self):
        s = state()
        s['quality_review']['status'] = 'error'
        self.assertEqual(q.choose_research_repair(s)['reason'], 'reviewer_unavailable')
        self.assertEqual(q.choose_research_repair(s)['action'], 'human_review')
        s = state()
        s['quality_gate_version'] = 1
        with self.assertRaises(seed.ResearchFenceError):
            q.choose_research_repair(s)

    def test_policy_stops_on_budget_and_no_new_evidence(self):
        self.assertEqual(q.choose_research_repair(state(limit=0))['reason'], 'auto_budget_exhausted')
        self.assertEqual(q.choose_research_repair(state(limit=2, used=2))['reason'], 'auto_budget_exhausted')
        s = state(limit=2, used=1)
        s['quality_repair_history'] = [{'source': 'auto', 'new_notes_count': 0}]
        self.assertEqual(q.choose_research_repair(s)['reason'], 'no_new_evidence')
        self.assertEqual(q.choose_research_repair(state(needs=False, red=False))['reason'], 'no_gap')

    def test_specific_feedback_and_fences(self):
        result = q.choose_research_repair(state(red=True))
        self.assertEqual(result['action'], 'targeted_research')
        self.assertEqual(len(result['topics']), 2)
        self.assertEqual(result['topics'][0], 'pricing numbers')
        s = state()
        s['red_team_review']['generation'] = 7
        with self.assertRaises(seed.ResearchFenceError):
            q.choose_research_repair(s)
        s = state()
        s['speculative_research']['seed_fingerprint'] = 'wrong'
        with self.assertRaises(seed.ResearchFenceError):
            q.checked_candidate_notes(s)

    def test_merge_only_exact_normalized_duplicates(self):
        merged, added = q.merge_candidate_notes(
            ['ABC numbers', 'price = 10'], [' ABC   numbers ', 'price = 12', '', 'new source']
        )
        self.assertEqual(merged, ['ABC numbers', 'price = 10', 'price = 12', 'new source'])
        self.assertEqual(added, 2)
        with self.assertRaises(seed.ResearchFenceError):
            q.merge_candidate_notes(['ok'], [None])

    def test_schema_and_stage(self):
        for action in ('approve', 'revise', 'revise_draft', 'more_research'):
            feedback = 'price evidence' if action in ('revise_draft', 'more_research') else ''
            self.assertEqual(ReviewAction(action=action, feedback=feedback).action, action)
        for action in ('more_research', 'revise_draft'):
            with self.assertRaises(ValueError):
                ReviewAction(action=action, feedback='   ')
        for action in ('reject', 'DELETE', 'approve_now'):
            with self.assertRaises(ValueError):
                ReviewAction(action=action)
        self.assertEqual(status_for_stage('human_review_v2'), TaskStatus.WAITING_REVIEW)


class RealNodeBodyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns, self.fake_supervisor = extract_actual_nodes()

    async def test_gate_routes_to_targeted_and_then_hitl(self):
        s = state(red=True)
        command = await self.ns['route_quality_gate'](s, with_hitl=True)
        self.assertEqual(command.goto, 'targeted_research')
        self.assertEqual(command.update['repair_request']['from_generation'], 1)
        self.assertEqual(command.update['quality_gate_decision']['reason'], 'evidence_gap')
        s = state(needs=False)
        command = await self.ns['route_quality_gate'](s, with_hitl=True)
        self.assertEqual(command.goto, 'human_review')

    async def test_targeted_research_merges_fenced_evidence_only(self):
        s = state()
        s['repair_request'] = {'source': 'auto', 'from_generation': 1, 'topics': ['pricing numbers']}
        self.fake_supervisor.additional = ['old evidence B', 'new fact 55', 'new fact 55']
        update = await self.ns['targeted_research'](s, {})
        self.assertEqual(update['research_generation'], 2)
        self.assertEqual(update['quality_repair_count'], 1)
        self.assertEqual(update['quality_repair_history'][0]['new_notes_count'], 1)
        self.assertEqual(update['speculative_research']['notes'], ['old evidence A', 'old evidence B', 'new fact 55'])
        self.assertNotIn('notes', update)  # cannot commit before HITL
        self.assertEqual(self.fake_supervisor.last_child[0]['research_generation'], 2)
        self.assertEqual(self.fake_supervisor.last_child[0]['quality_gate_version'], 2)
        self.assertIn('pricing numbers', self.fake_supervisor.last_child[0]['supervisor_messages'][1].content)

    async def test_no_evidence_and_child_staleness(self):
        s = state(used=1)
        s['repair_request'] = {'source': 'auto', 'from_generation': 1, 'topics': ['price source']}
        self.fake_supervisor.additional = ['old evidence A']
        update = await self.ns['targeted_research'](s, {})
        self.assertEqual(update['quality_repair_history'][-1]['new_notes_count'], 0)
        merged = {**s, **update}
        merged['quality_review'] = {'generation': 2, 'status': 'completed',
                                    'need_more_research': True, 'missing_aspects': ['price source']}
        merged['red_team_review'] = {'generation': 2, 'status': 'completed', 'passed': True}
        self.assertEqual(q.choose_research_repair(merged)['reason'], 'no_new_evidence')
        self.fake_supervisor.force_wrong_gen = True
        with self.assertRaises(seed.ResearchFenceError):
            await self.ns['targeted_research'](s, {})

    async def test_user_research_budget_and_stale_request(self):
        s = state()
        s['repair_request'] = {'source': 'human', 'from_generation': 1, 'topics': ['europe pricing']}
        self.fake_supervisor.additional = ['new EU evidence']
        update = await self.ns['targeted_research'](s, {})
        self.assertEqual(update['manual_research_count'], 1)
        self.assertNotIn('quality_repair_count', update)
        self.assertEqual(update['quality_repair_history'][-1]['source'], 'human')
        s['repair_request']['from_generation'] = 0
        with self.assertRaises(seed.ResearchFenceError):
            await self.ns['targeted_research'](s, {})

    async def test_review_copyedit_and_explicit_research(self):
        s = state(needs=False)
        self.ns['interrupt'] = lambda _: {'action': 'revise_draft', 'feedback': 'change title'}
        decision = await self.ns['human_review'](s)
        self.assertEqual(decision.goto, 'evaluate_research')
        self.assertEqual(decision.update, {'draft_report': 'Updated Draft'})
        self.ns['interrupt'] = lambda _: {'action': 'more_research', 'feedback': 'find EU pricing'}
        decision = await self.ns['human_review'](s)
        self.assertEqual(decision.goto, 'targeted_research')
        self.assertEqual(decision.update['repair_request']['topics'], ['find EU pricing'])
        self.assertEqual(decision.update['repair_request']['source'], 'human')
        self.ns['interrupt'] = lambda _: {'action': 'approve'}
        decision = await self.ns['human_review'](s)
        self.assertEqual(decision.goto, 'claim_verification')
        self.assertEqual(decision.update['notes'], ['old evidence A', 'old evidence B'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
