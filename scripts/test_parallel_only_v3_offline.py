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
from scripts.test_quality_gate_v2_offline import extract_actual_builder


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

    def test_only_parallel_graphs_for_all_versions_and_hitl_modes(self):
        build = extract_actual_builder()
        for version, hitl in product((0, 1, 2), (True, False)):
            with self.subTest(version=version, hitl=hitl):
                g = build(with_hitl=hitl, quality_gate_version=version)
                edges = set(g.edges)
                self.assertIn(('write_research_brief', 'write_draft_report'), edges)
                self.assertIn(('write_research_brief', 'supervisor_subgraph'), edges)
                self.assertNotIn(('write_draft_report', 'supervisor_subgraph'), edges)
                self.assertNotIn(('supervisor_subgraph', 'claim_verification'), edges)
                join = (('write_draft_report', 'supervisor_subgraph'),
                        'evaluate_research' if version >= 1 else ('human_review' if hitl else 'accept_research'))
                self.assertIn(join, edges)
                if version == 2:
                    self.assertIn(('targeted_research', 'evaluate_research'), edges)
                self.assertIn(('claim_verification', 'final_report_generation'), edges)
        with self.assertRaisesRegex(ValueError, 'serial graph was removed'):
            build(with_hitl=True, speculative=False, quality_gate_version=2)

    def test_brief_always_produces_parallel_seed_generation_one(self):
        fn = real_function('deep_research/agent_builder.py', 'write_research_brief', {
            'AgentState': dict,
            'Command': type('Command', (), {'__class_getitem__': classmethod(lambda cls, x: cls)}),
            'memory_read_enabled': lambda: False,
            'get_chat_model_auto': lambda *_args, **_kw: StubModel(),
            'RESEARCH_BRIEF_PROMPT': DummyPrompt(),
            'get_today_str': lambda: '2026-10-13',
            'HumanMessage': FakeHumanMessage,
            'build_research_seed': seed.build_research_seed,
            'seed_metrics': seed.seed_metrics,
            'render_seed_message': seed.render_seed_message,
            'quality_gate_version_for_new_job': gate.quality_gate_version_for_new_job,
            'initial_repair_limits': gate.initial_repair_limits,
            'logger': DummyLogger(),
        })
        for raw in ('on', 'off', '', 'random'):
            with self.subTest(raw=raw), patch.dict(os.environ, {
                seed.SEED_LITE_ENV: raw, 'DR_QUALITY_GATE_V2': 'on',
            }):
                state = {'messages': [FakeHumanMessage('topic')]}
                result = asyncio.run(fn(state))
                self.assertIsInstance(result, dict)
                self.assertEqual(result['research_generation'], 1)
                self.assertEqual(result['quality_gate_version'], 2)
                self.assertEqual(result['research_brief'], 'brief-v3')
                self.assertEqual(len(result['supervisor_messages']), 2)
                self.assertEqual(result['quality_repair_count'], 0)

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

    def test_runner_reads_parallel_versions_without_reclassification(self):
        method = real_runner_method('_quality_gate_from_checkpoint')
        async def run_one(values=None, exists=True):
            fake = types.SimpleNamespace(_checkpointer=Saver(values, exists=exists))
            return await method(fake, {})
        for version in (0, 1, 2):
            result = asyncio.run(run_one({'research_generation': 1, 'quality_gate_version': version}))
            self.assertEqual(result, version)
        self.assertIsNone(asyncio.run(run_one(exists=False)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
