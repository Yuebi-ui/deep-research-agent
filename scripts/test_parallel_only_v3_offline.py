"""stdlib-only checks of the actual parallel-only graph and routing bodies.

Does not require LangGraph or an LLM, and does not replace real integration tests.
"""
from __future__ import annotations

import ast
import asyncio
from itertools import product
import os
from pathlib import Path
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from deep_research import research_seed as seed
from deep_research import quality_gate as gate


class FakeHumanMessage:
    def __init__(self, content):
        self.content = content


class StubModel:
    async def ainvoke(self, *_args, **_kwargs):
        return types.SimpleNamespace(content="brief-v3")

    def invoke(self, *_args, **_kwargs):
        return types.SimpleNamespace(content="draft-v3")


class DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def debug(self, *args, **kwargs):
        return None


class DummyPrompt:
    def format(self, **kwargs):
        return "prompt"


def real_function(module_path: str, name: str, namespace: dict):
    filename = ROOT / module_path
    tree = ast.parse(filename.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    env = dict(namespace)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(filename), "exec"), env)
    return env[name]


def real_runner_method(name: str):
    filename = ROOT / 'backend/runtime/runner.py'
    tree = ast.parse(filename.read_text(encoding='utf-8'))
    parent = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'TaskRunner')
    method = next(n for n in parent.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    env = {'Any': object}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(filename), 'exec'), env)
    return env[name]


class Saver:
    def __init__(self, values=None, exists=True):
        self.values = values or {}
        self.exists = exists

    async def aget_tuple(self, _config):
        if not self.exists:
            return None
        return types.SimpleNamespace(checkpoint={'channel_values': self.values})


class ParallelOnlyTests(unittest.TestCase):
    def test_flag_cannot_reenable_serial(self):
        for raw in ('off', '0', 'false', 'on', '1', 'true', '', 'other'):
            with self.subTest(raw=raw), patch.dict(os.environ, {seed.SEED_LITE_ENV: raw}):
                self.assertTrue(seed.seed_lite_speculative_enabled())

    def test_v3_only_graph_rejects_old_versions(self):
        src = (ROOT / 'deep_research/agent_builder.py').read_text()
        tree = ast.parse(src)
        node = next(x for x in tree.body if isinstance(x, ast.FunctionDef)
                    and x.name == '_create_builder')
        class FakeGraph:
            def __init__(self, *_args, **_kwargs):
                self.edges, self.nodes = [], {}
            def add_edge(self, a, b):
                self.edges.append((tuple(a) if isinstance(a, list) else a, b))
            def add_node(self, key, fn):
                self.nodes[key] = fn
        names = ('write_research_brief', 'write_draft_report', 'supervisor_agent',
                 'precheck_quality_gate', 'evaluate_research', 'red_team_review',
                 'route_quality_gate_v3', 'targeted_research', 'human_review',
                 'claim_verification', 'final_report_generation', 'quality_gate_failed')
        space = {x: lambda *a, **k: None for x in names}
        space.update(StateGraph=FakeGraph, AgentState=dict, AgentInputState=dict,
                     START='start', END='end', QUALITY_GATE_VERSION=3)
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<v3>', 'exec'), space)
        build = space['_create_builder']
        for old in (0, 1, 2, False):
            with self.subTest(version=old), self.assertRaisesRegex(ValueError, 'only Quality Gate V3'):
                build(quality_gate_version=old)
        with self.assertRaisesRegex(ValueError, 'serial graph removed'):
            build(speculative=False)
        for hitl in (True, False):
            graph = build(with_hitl=hitl)
            edges = set(graph.edges)
            self.assertIn(('write_research_brief', 'write_draft_report'), edges)
            self.assertIn(('write_research_brief', 'supervisor_subgraph'), edges)
            self.assertIn((('write_draft_report', 'supervisor_subgraph'), 'precheck_quality_gate'), edges)
            self.assertIn(('targeted_research', 'precheck_quality_gate'), edges)
            self.assertNotIn('regenerate_research', graph.nodes)
            self.assertNotIn('accept_research', graph.nodes)

    def test_brief_unconditionally_generates_v3_checklist(self):
        import json
        from deep_research.evidence import parse_brief_plan
        class JsonModel(StubModel):
            async def ainvoke(self, *_a, **_k):
                return types.SimpleNamespace(content=json.dumps({
                    'research_brief': 'Analyze energy costs',
                    'required_aspects': [{'title': 'Energy cost evidence'}],
                }))
        fn = real_function('deep_research/agent_builder.py', 'write_research_brief', {
            'AgentState': dict,
            'memory_read_enabled': lambda: False,
            'get_chat_model_auto': lambda *_a, **_kw: JsonModel(),
            'RESEARCH_BRIEF_V3_PROMPT': DummyPrompt(),
            'get_today_str': lambda: '2026-10-17',
            'HumanMessage': FakeHumanMessage,
            'build_research_seed': seed.build_research_seed,
            'seed_metrics': seed.seed_metrics,
            'render_seed_message': seed.render_seed_message,
            'parse_brief_plan': parse_brief_plan,
            'risk_tags_for_query': gate.risk_tags_for_query,
            'QUALITY_GATE_VERSION': 3,
            'initial_repair_limits': gate.initial_repair_limits,
            'logger': DummyLogger(),
        })
        for raw in ('off', 'on', '', 'other'):
            with self.subTest(old_switch=raw), patch.dict(os.environ, {
                'DR_QUALITY_GATE_V1': raw, 'DR_QUALITY_GATE_V2': raw,
                'DR_QUALITY_GATE_V3': raw,
            }):
                result = asyncio.run(fn({'messages': [FakeHumanMessage('topic')]}))
                self.assertEqual(result['quality_gate_version'], 3)
                self.assertTrue(result['required_aspects'])
                self.assertEqual(result['research_generation'], 1)

    def test_draft_does_not_write_seed_channel_even_for_old_state(self):
        fn = real_function('deep_research/agents/draft_agent.py', 'write_draft_report', {
            'AgentState': dict,
            'Command': object,
            'logger': DummyLogger(),
            'DRAFT_REPORT_PROMPT': DummyPrompt(),
            'get_today_str': lambda: '2026-10-13',
            'HumanMessage': FakeHumanMessage,
            'draft_model': StubModel(),
        })
        for gen in (0, 1, 3):
            result = fn({'research_brief': 'topic', 'research_generation': gen})
            self.assertEqual(result, {'draft_report': 'draft-v3'})

    def test_notes_never_written_without_fenced_approval(self):
        with self.assertRaisesRegex(seed.ResearchFenceError, 'generation >= 1'):
            seed.quarantine_update({'research_generation': 0, 'research_brief': 'topic'}, ['one'])
        for gen in (1, 2):
            state = {'research_generation': gen, 'research_brief': 'topic'}
            payload = seed.quarantine_update(state, ['one', 'two'])
            self.assertNotIn('notes', payload)
            self.assertEqual(payload['speculative_research']['generation'], gen)
            self.assertEqual(seed.fenced_accept_update({**state, **payload})['notes'], ['one', 'two'])
            with self.assertRaises(seed.ResearchFenceError):
                seed.fenced_accept_update({**state, **payload, 'research_generation': gen + 1})

    def test_runner_fails_closed_on_legacy_serial_checkpoints(self):
        method = real_runner_method('_lineage_is_speculative')
        async def run_one(values=None, exists=True):
            fake = types.SimpleNamespace(_checkpointer=Saver(values, exists=exists))
            return await method(fake, {'configurable': {'thread_id': 'test'}})
        for values in ({'research_generation': 0}, {'research_generation': -1},
                       {'research_generation': '1'}, {'draft_report': 'draft'},
                       {'research_brief': 'brief'}):
            with self.subTest(values=values), self.assertRaises(seed.ResearchFenceError):
                asyncio.run(run_one(values))
        for values, expected in (({'research_generation': 1}, True),
                                 ({'research_generation': 2}, True),
                                 ({}, None)):
            self.assertIs(asyncio.run(run_one(values)), expected)
        self.assertIsNone(asyncio.run(run_one(exists=False)))

    def test_runner_rejects_old_checkpoint_versions(self):
        method = real_runner_method('_quality_gate_from_checkpoint')
        async def run_one(values=None, exists=True):
            fake = types.SimpleNamespace(_checkpointer=Saver(values, exists=exists))
            return await method(fake, {})
        for version in (0, 1, 2, None, False):
            with self.subTest(version=version), self.assertRaises(seed.ResearchFenceError):
                asyncio.run(run_one({'research_generation': 1, 'quality_gate_version': version}))
        self.assertEqual(asyncio.run(run_one({'research_generation': 1, 'quality_gate_version': 3})), 3)
        self.assertEqual(asyncio.run(run_one(exists=False)), 3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
