"""V3 stdlib-only regression suite: run with ``python scripts/test_quality_gate_v3_offline.py``.

Tests production policy and graph-node bodies extracted from source (no fake
reimplementation of the policy). Full LangGraph/checkpoint integration requires
project dependencies; this suite deliberately does not claim to cover that.
"""

from __future__ import annotations

import ast
import asyncio
import itertools
import json
import os
from pathlib import Path
import sys
from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deep_research import quality_gate as q, research_seed as seed
from deep_research.evidence import (
    attribute_sources, checklist_fingerprint, collect_provider_sources,
    extract_search_sources, merge_evidence, normalize_url, parse_brief_plan,
)


class Command:
    def __init__(self, goto=None, update=None):
        self.goto, self.update = goto, update or {}

    @classmethod
    def __class_getitem__(cls, _):
        return cls


class HumanMessage:
    def __init__(self, content):
        self.content = content


class Supervisor:
    last_child = None

    async def ainvoke(self, child, config=None):
        self.last_child = child
        gen = child['research_generation']
        raw = search_messages(urls=['https://third.example.org/2026/risk'])
        records = attribute_sources(extract_search_sources(raw),
                                    aspect_id='aspect-01',
                                    valid_aspects=child['required_aspects'],
                                    generation=gen)
        return {'speculative_research': seed.build_speculative_payload(
            generation=gen, research_brief=child['research_brief'],
            notes=['supplemental finding'], evidence_records=records,
            checklist_fingerprint=child['checklist_fingerprint'],
        )}


def search_messages(urls=None):
    urls = urls or ['https://official.example.org/a', 'https://industry.example.com/b']
    sections = []
    for i, url in enumerate(urls, 1):
        sections.append(f'--- SOURCE {i}: Source {i} ---\nURL: {url}\n\n'
                        f'SUMMARY:\nPublished: 2026-10-01\nMeasured finding {i}.\n'
                        + '-' * 80)
    return [SimpleNamespace(name='tavily_search', type='tool', content='\n'.join(sections))]


def state(*, urls=None, risk=None, min_sources=2, generation=1, count=0, limit=2):
    aspects = [{'aspect_id': 'aspect-01', 'title': 'Unit economics',
                'min_sources': min_sources, 'max_age_days': None}]
    records = attribute_sources(extract_search_sources(search_messages(urls)),
                                aspect_id='aspect-01', valid_aspects=aspects,
                                generation=generation)
    fingerprint = checklist_fingerprint(aspects)
    brief = 'How unit economics affects profitability'
    candidate = seed.build_speculative_payload(
        generation=generation, research_brief=brief,
        notes=['observed research finding'], evidence_records=records,
        checklist_fingerprint=fingerprint,
    )
    return {
        'quality_gate_version': 3, 'research_generation': generation,
        'research_brief': brief, 'draft_report': 'Draft report about economics',
        'speculative_research': candidate, 'required_aspects': aspects,
        'checklist_status': 'proposed', 'checklist_fingerprint': fingerprint,
        'quality_risk_tags': risk or [],
        'quality_repair_count': count, 'quality_repair_limit': limit,
        'manual_research_count': 0, 'manual_research_limit': 2,
        'quality_repair_history': [],
        'quality_review': {'status': 'completed', 'generation': generation,
                           'need_more_research': False, 'issues': []},
        'red_team_review': None,
    }


def actual_functions(*names):
    filename = ROOT / 'deep_research/agent_builder.py'
    tree = ast.parse(filename.read_text(encoding='utf-8'))
    funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name in names]
    if len(funcs) != len(names):
        raise RuntimeError('missing source graph node bodies')
    supervisor = Supervisor()
    env = {
        '__name__': '__offline_v3_nodes__', 'Any': object,
        'AgentState': dict, 'RunnableConfig': dict,
        'Command': Command, 'HumanMessage': HumanMessage,
        'check_evidence_coverage': q.check_evidence_coverage,
        'checked_candidate_evidence': q.checked_candidate_evidence,
        'checked_candidate_notes': q.checked_candidate_notes,
        'choose_research_repair_v3': q.choose_research_repair_v3,
        'format_candidate_notes': q.format_candidate_notes,
        'build_review_context': q.build_review_context,
        'build_human_review_summary': q.build_human_review_summary,
        'format_followup_request': q.format_followup_request,
        'merge_candidate_notes': q.merge_candidate_notes,
        'merge_evidence': merge_evidence,
        'v3_lineage': q.v3_lineage,
        'QUALITY_GATE_VERSION': q.QUALITY_GATE_VERSION,
        'active_generation': seed.active_generation,
        'ResearchFenceError': seed.ResearchFenceError,
        'seed_fingerprint': seed.seed_fingerprint,
        'build_research_seed': seed.build_research_seed,
        'render_seed_message': seed.render_seed_message,
        'build_speculative_payload': seed.build_speculative_payload,
        'supervisor_agent': supervisor,
        'logger': SimpleNamespace(info=lambda *_: None),
    }
    exec(compile(ast.Module(body=funcs, type_ignores=[]), str(filename), 'exec'), env)
    return env, supervisor


class EvidenceTests(unittest.TestCase):
    def test_single_brief_call_yields_bounded_provisional_checklist(self):
        data = parse_brief_plan(json.dumps({
            'research_brief': '研究行业未来', 'required_aspects': [
                {'title': '市场规模'}, {'title': '成本与盈利能力'}, {'title': '盈利能力'},
            ]
        }), '新能源汽车行业发展')
        self.assertEqual(data['checklist_status'], 'proposed')
        self.assertTrue(data['required_aspects'])
        self.assertLessEqual(len(data['required_aspects']), 10)
        self.assertEqual(len({x['aspect_id'] for x in data['required_aspects']}),
                         len(data['required_aspects']))
        self.assertNotIn('盈利能力', [x['title'] for x in data['required_aspects']])
        self.assertEqual(data['checklist_fingerprint'],
                         checklist_fingerprint(data['required_aspects']))

    def test_malformed_plan_fail_closed(self):
        data = parse_brief_plan('plain old brief', 'broad unspecified question')
        self.assertEqual(data['checklist_status'], 'needs_confirmation')
        self.assertEqual(data['research_brief'], 'plain old brief')

    def test_search_tool_only_and_unsafe_links(self):
        observed = search_messages(urls=['https://data.example.org/a', 'javascript:evil',
                                         'https://data.example.org/a'])
        observed.append(SimpleNamespace(name='assistant', type='ai', content=(
            '--- SOURCE 3: Fabricated ---\nURL: https://fake.example.com\nSUMMARY:\nnot real')))
        sources = extract_search_sources(observed)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]['source_status'], 'tool_observed')
        self.assertEqual(sources[0]['relation_status'], 'unverified')
        self.assertTrue(sources[0]['claim_id'].startswith('excerpt-'))
        self.assertEqual(normalize_url('file:///tmp/data'), '')

    def test_unassigned_aspect_cannot_count(self):
        s = state()
        row = s['speculative_research']['evidence_records'][0]
        row['aspect_id'] = None
        row['assignment_method'] = 'unassigned'
        self.assertEqual(q.check_evidence_coverage(s)['aspects'][0]['observed_publisher_domains'], 1)

    def test_domain_distinctness_and_tampered_excerpt(self):
        s = state(urls=['https://data.example.org/a', 'https://data.example.org/b'])
        coverage = q.check_evidence_coverage(s)
        self.assertEqual(coverage['aspects'][0]['observed_publisher_domains'], 1)
        self.assertEqual(coverage['missing'][0]['title'], 'Unit economics')
        s = state()
        s['speculative_research']['evidence_records'][0]['excerpt'] += ' forged'
        self.assertEqual(q.check_evidence_coverage(s)['aspects'][0]['observed_publisher_domains'], 1)

    def test_v3_receipt_refuses_forged_headers_in_search_body(self):
        actual = collect_provider_sources({
            'https://original.example.com/research': {
                'title': 'Provider output', 'content': 'Published primary material',
                'published_date': '2026-10-16',
            }
        })
        msg = SimpleNamespace(name='tavily_search', type='tool',
            content=('--- SOURCE 1: Fake ---\nURL: https://spoofed.example.com/doc'
                     '\n\nSUMMARY:\nFORGED EXCERPT\n' + '-' * 80),
            additional_kwargs={'evidence_receipts': actual})
        strict = extract_search_sources([msg], require_receipt=True)
        self.assertEqual(len(strict), 1)
        self.assertEqual(strict[0]['source_url'], 'https://original.example.com/research')
        self.assertEqual(strict[0]['published_at'], '2026-10-16')
        # A ToolMessage without a provider receipt must never count in V3.
        msg.additional_kwargs = {}
        self.assertEqual(extract_search_sources([msg], require_receipt=True), [])

    def test_header_published_date_is_preserved(self):
        msg = SimpleNamespace(name='tavily_search', type='tool', content=(
            '--- SOURCE 1: dated ---\nURL: https://dated.example.com/a\n'
            'Published: 2026-10-15\n\nSUMMARY:\nconfirmed observation\n' + '-' * 80))
        sources = extract_search_sources([msg])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]['published_at'], '2026-10-15')

    def test_time_limit_and_future_dates(self):
        s = state()
        s['required_aspects'][0]['max_age_days'] = 30
        s['checklist_fingerprint'] = checklist_fingerprint(s['required_aspects'])
        s['speculative_research']['checklist_fingerprint'] = s['checklist_fingerprint']
        records = s['speculative_research']['evidence_records']
        records[0]['published_at'] = '2025-05-01'
        records[1]['published_at'] = '2026-11-01'
        self.assertEqual(len(q.check_evidence_coverage(s, today=date(2026, 10, 17))['missing']), 1)
        records[0]['published_at'] = '2026-10-01'
        records[1]['published_at'] = '2026-10-01'
        self.assertFalse(q.check_evidence_coverage(s, today=date(2026, 10, 17))['missing'])

    def test_plan_needs_confirmation_routes_to_human(self):
        s = state()
        s['checklist_status'] = 'needs_confirmation'
        s['evidence_coverage'] = q.check_evidence_coverage(s)
        outcome = q.choose_research_repair_v3(s, phase='precheck')
        self.assertEqual(outcome['reason'], 'checklist_needs_confirmation')
        self.assertEqual(outcome['action'], 'human_review')

    def test_generation_and_checklist_fences(self):
        s = state()
        s['speculative_research']['generation'] = 10
        with self.assertRaises(seed.ResearchFenceError):
            q.checked_candidate_evidence(s)
        s = state()
        s['required_aspects'][0]['title'] = 'changed on resume'
        with self.assertRaises(seed.ResearchFenceError):
            q.checked_candidate_evidence(s)
        s = state()
        s['speculative_research']['evidence_records'][0]['generation'] = 2
        with self.assertRaises(seed.ResearchFenceError):
            q.checked_candidate_evidence(s)

    def test_does_not_falsely_assert_factual_verification(self):
        s = state()
        report = q.check_evidence_coverage(s)
        self.assertEqual(report['aspects'][0]['status'], 'covered_structurally')
        self.assertIn('aspect_relevance_requires_semantic_review', report['limitations'])
        self.assertTrue(all(e['relation_status'] == 'unverified'
                            for e in q.checked_candidate_evidence(s)))


class PolicyTests(unittest.TestCase):
    def test_hard_gap_overrides_llm_pass(self):
        s = state(urls=['https://one.example.org/a'])
        s['evidence_coverage'] = q.check_evidence_coverage(s)
        decision = q.choose_research_repair_v3(s, phase='precheck')
        self.assertEqual(decision['action'], 'targeted_research')
        self.assertEqual(decision['topics'], ['Unit economics'])

    def test_full_coverage_skips_expensive_judges_in_precheck(self):
        s = state()
        s['evidence_coverage'] = q.check_evidence_coverage(s)
        self.assertEqual(q.choose_research_repair_v3(s, phase='precheck')['action'], 'evaluate_research')

    def test_red_team_conditional_and_skip(self):
        s = state()
        self.assertEqual(q.choose_research_repair_v3(s, phase='evaluator')['action'], 'human_review')
        s['quality_risk_tags'] = ['finance']
        self.assertEqual(q.choose_research_repair_v3(s, phase='evaluator')['action'], 'red_team_audit')

    def test_unknown_critical_gap_drives_specific_research(self):
        s = state()
        s['quality_review'].update(need_more_research=True, issues=[{
            'kind': 'hidden_omission', 'severity': 'critical',
            'research_topic': 'Independent cost evidence'
        }])
        decision = q.choose_research_repair_v3(s, phase='evaluator')
        self.assertEqual(decision['topics'], ['Independent cost evidence'])
        self.assertEqual(decision['action'], 'targeted_research')

    def test_red_team_counterevidence_repair(self):
        s = state(risk=['finance'])
        s['red_team_review'] = {
            'generation': 1, 'status': 'completed', 'passed': False,
            'issues': [{'kind': 'counterevidence', 'severity': 'high',
                        'research_topic': 'Look for counterexamples'}]
        }
        self.assertEqual(q.choose_research_repair_v3(s, phase='red_team')['action'], 'targeted_research')
        s['red_team_review']['issues'][0]['kind'] = 'logic_risk'
        self.assertEqual(q.choose_research_repair_v3(s, phase='red_team')['reason'],
                         'red_team_non_research_risk')

    def test_error_and_bad_verdict_require_human(self):
        s = state()
        s['quality_review']['status'] = 'error'
        self.assertEqual(q.choose_research_repair_v3(s, phase='evaluator')['reason'],
                         'evaluator_unavailable')
        s = state(risk=['finance'])
        s['red_team_review'] = {'status': 'error', 'generation': 1, 'passed': None}
        self.assertEqual(q.choose_research_repair_v3(s, phase='red_team')['action'], 'human_review')

    def test_budget_and_no_new_evidence(self):
        for limit in range(4):
            for used in range(limit + 1):
                s = state(urls=['https://one.example.org/a'], count=used, limit=limit)
                s['evidence_coverage'] = q.check_evidence_coverage(s)
                decision = q.choose_research_repair_v3(s, phase='precheck')
                if used < limit:
                    self.assertEqual(decision['action'], 'targeted_research')
                else:
                    self.assertEqual(decision['reason'], 'auto_budget_exhausted')
                s['quality_repair_history'] = [{'new_evidence_count': 0}]
                self.assertEqual(q.choose_research_repair_v3(s, phase='precheck')['reason'],
                                 'no_new_evidence')

    def test_enumerated_v3_policy_no_overspend_or_unsafe_pass(self):
        examined = 0
        for need, risk, eval_status, used, limit in itertools.product(
            (False, True), (False, True), ('completed', 'error'), (0, 1, 2, 3), (0, 1, 2, 3)
        ):
            if used > limit:
                continue
            s = state(count=used, limit=limit, risk=['finance'] if risk else [])
            s['quality_review']['status'] = eval_status
            if need:
                s['quality_review'].update(need_more_research=True, issues=[{
                    'kind': 'evidence_gap', 'severity': 'high',
                    'research_topic': 'Need additional evidence'}])
            choice = q.choose_research_repair_v3(s, phase='evaluator')
            if choice['action'] == 'targeted_research':
                self.assertLess(used, limit)
                self.assertEqual(eval_status, 'completed')
            if eval_status == 'error':
                self.assertEqual(choice['action'], 'human_review')
            if not risk and not need:
                self.assertNotEqual(choice['action'], 'red_team_audit')
            examined += 1
        self.assertEqual(examined, 80)


class SourceGraphTests(unittest.TestCase):
    def test_actual_precheck_command_and_fail_closed_auto_mode(self):
        env, _ = actual_functions('_v3_route_command', 'precheck_quality_gate')
        s = state(urls=['https://one.example.org/a'])
        result = asyncio.run(env['precheck_quality_gate'](s))
        self.assertEqual(result.goto, 'targeted_research')
        self.assertEqual(result.update['repair_request']['from_generation'], 1)
        self.assertNotIn('notes', result.update)
        s = state()
        self.assertEqual(asyncio.run(env['precheck_quality_gate'](s)).goto, 'evaluate_research')
        denied = env['_v3_route_command'](s, {'action': 'human_review', 'phase': 'precheck',
                                              'reason': 'checklist_needs_confirmation'}, with_hitl=False)
        self.assertEqual(denied.goto, 'quality_gate_failed')

    def test_actual_judge_route_skip_or_call_adversary(self):
        env, _ = actual_functions('_v3_route_command', 'route_quality_gate_v3')
        s = state()
        result = asyncio.run(env['route_quality_gate_v3'](s))
        self.assertEqual(result.goto, 'human_review')
        self.assertEqual(result.update['red_team_review']['status'], 'skipped')
        s = state(risk=['finance'])
        result = asyncio.run(env['route_quality_gate_v3'](s))
        self.assertEqual(result.goto, 'red_team_audit')

    def test_actual_targeted_merge_tracks_evidence_and_generation(self):
        env, fake = actual_functions('targeted_research')
        s = state(urls=['https://one.example.org/a'])
        s['repair_request'] = {'source': 'auto', 'topics': ['Unit economics'],
                               'from_generation': 1}
        result = asyncio.run(env['targeted_research'](s, {}))
        self.assertEqual(fake.last_child['research_generation'], 2)
        self.assertEqual(fake.last_child['quality_gate_version'], 3)
        self.assertEqual(result['quality_repair_count'], 1)
        self.assertEqual(result['research_generation'], 2)
        self.assertEqual(result['quality_repair_history'][-1]['new_evidence_count'], 1)
        merged = {e['source_url'] for e in result['speculative_research']['evidence_records']}
        self.assertEqual(merged, {'https://one.example.org/a', 'https://third.example.org/2026/risk'})
        resumed = {**s, **result}
        self.assertEqual(len(q.checked_candidate_evidence(resumed)), 2)
        self.assertEqual(result['quality_review'], None)

    def test_v3_graph_wiring_uses_precheck_and_conditional_route(self):
        # The offline builder stub requires names for the new V3 graph nodes.
        code = ROOT.joinpath('deep_research/agent_builder.py').read_text(encoding='utf8')
        tree = ast.parse(code)
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_create_builder')
        class FakeGraph:
            def __init__(self, *_a, **_kw):
                self.nodes, self.edges = {}, []
            def add_node(self, name, value):
                if name in self.nodes:
                    raise AssertionError(f'duplicate node {name}')
                self.nodes[name] = value
            def add_edge(self, a, b):
                self.edges.append((tuple(a) if isinstance(a, list) else a, b))
        def noop(*_a, **_k):
            return None
        names = ['write_research_brief', 'write_draft_report', 'supervisor_agent',
                 'claim_verification', 'final_report_generation', 'evaluate_research',
                 'red_team_review', 'human_review',
                 'targeted_research', 'precheck_quality_gate', 'route_quality_gate_v3',
                 'quality_gate_failed']
        scope = {name: noop for name in names}
        scope.update(StateGraph=FakeGraph, START='start', END='end',
                     AgentState=dict, AgentInputState=dict,
                     quality_gate_version_for_new_job=lambda: 3,
                     QUALITY_GATE_VERSION=3)
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<builder>', 'exec'), scope)
        graph = scope['_create_builder'](with_hitl=True, quality_gate_version=3)
        edges = set(graph.edges)
        self.assertIn((('write_draft_report', 'supervisor_subgraph'), 'precheck_quality_gate'), edges)
        self.assertIn(('targeted_research', 'precheck_quality_gate'), edges)
        self.assertIn(('evaluate_research', 'route_quality_gate_v3'), edges)
        self.assertIn(('red_team_audit', 'route_quality_gate_v3'), edges)
        self.assertNotIn(('evaluate_research', 'red_team_audit'), edges)
        self.assertNotIn(('red_team_audit', 'human_review'), edges)
        self.assertIn('human_review', graph.nodes)
        self.assertNotIn('regenerate_research', graph.nodes)
        graph2 = scope['_create_builder'](with_hitl=False, quality_gate_version=3)
        self.assertIn('quality_gate_failed', graph2.nodes)
        self.assertNotIn('human_review', graph2.nodes)



# Extra node-body tests are registered separately from the CLI suite so the
# initial __main__ guard above can stay at the very end after future edits.


class AgentNodeTests(unittest.TestCase):
    def test_researcher_compressor_collects_only_tool_observed_sources(self):
        path = ROOT / 'deep_research/agents/research_agent.py'
        tree = ast.parse(path.read_text(encoding='utf8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'compress_research')
        class Prompt:
            def format(self, **_):
                return 'prompt'
        env = {
            'ResearcherState': dict,
            'SystemMessage': HumanMessage, 'HumanMessage': HumanMessage,
            'COMPRESS_RESEARCH_SYSTEM_PROMPT': Prompt(),
            'COMPRESS_RESEARCH_HUMAN_PROMPT': Prompt(),
            'get_today_str': lambda: '2026-10-17',
            'compress_model': SimpleNamespace(invoke=lambda _: SimpleNamespace(content='summary')),
            'filter_messages': lambda messages, include_types: [x for x in messages if x.type in include_types],
            'extract_research_trace': lambda s, c: {'topic': 'offline'},
            'extract_search_sources': extract_search_sources,
            'logger': SimpleNamespace(info=lambda *_: None, debug=lambda *_: None),
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), env)
        observed = search_messages()
        # V3 counts out-of-band provider receipts, NOT formatted message text.
        valid_receipts = collect_provider_sources({
            'https://official.example.org/a': {'title': 'official', 'content': 'official fact'},
            'https://industry.example.com/b': {'title': 'industry', 'content': 'industry fact'},
        })
        observed[0].additional_kwargs = {'evidence_receipts': valid_receipts}
        observed.append(SimpleNamespace(type='ai', name='assistant', content=(
            '--- SOURCE 3: fabricated ---\nURL: https://fabricated.com\n\nSUMMARY:\nnot observed')))
        outcome = env['compress_research']({
            'researcher_messages': observed, 'research_topic': 'unit economics',
            'quality_gate_version': 3,
        })
        self.assertEqual(len(outcome['observed_sources']), 2)
        self.assertTrue(all('fabricated' not in x['source_url'] for x in outcome['observed_sources']))
        self.assertEqual(outcome['compressed_research'], 'summary')

    def test_tool_node_v3_uses_provider_receipt_side_channel(self):
        path = ROOT / 'deep_research/agents/research_agent.py'
        node = next(n for n in ast.parse(path.read_text(encoding='utf8')).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'tool_node')
        class ToolMessage:
            def __init__(self, content, name, tool_call_id, additional_kwargs):
                self.content, self.name, self.tool_call_id = content, name, tool_call_id
                self.additional_kwargs = additional_kwargs
                self.type = 'tool'
        receipts = collect_provider_sources({
            'https://trusted.example.org/paper': {'title': 'paper', 'content': 'original provider text'},
        })
        env = {
            'ResearcherState': dict, 'logger': SimpleNamespace(info=lambda *_: None, warning=lambda *_: None),
            'tavily_search_with_sources': lambda **kwargs: ('formatted tool output', receipts),
            'tools_by_name': {}, 'ToolMessage': ToolMessage,
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), env)
        tc = {'name': 'tavily_search', 'args': {'query': 'legitimate'}, 'id': 'call'}
        updates = env['tool_node']({'quality_gate_version': 3,
                                    'researcher_messages': [SimpleNamespace(tool_calls=[tc])]})
        msg = updates['researcher_messages'][0]
        self.assertEqual(msg.content, 'formatted tool output')
        self.assertEqual(msg.additional_kwargs['evidence_receipts'], receipts)
        self.assertEqual(extract_search_sources([msg], require_receipt=True), receipts)

    def test_supervisor_collects_subagent_provenance_and_quarantines(self):
        path = ROOT / 'deep_research/agents/supervisor.py'
        tree = ast.parse(path.read_text(encoding='utf8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'supervisor_tools')
        class ToolMessage:
            def __init__(self, content, name, tool_call_id):
                self.content, self.name, self.tool_call_id = content, name, tool_call_id
                self.type = 'tool'
        class FakeResearcher:
            async def ainvoke(self, _input):
                return {
                    'observed_sources': extract_search_sources(search_messages()),
                    'compressed_research': 'summarized research',
                    'raw_notes': ['raw findings'],
                    'episode_trace': {'tool_calls': 1},
                }
        env = {
            'SupervisorState': dict, 'Literal': object,
            'Command': Command, 'END': 'end',
            'max_researcher_iterations': 3, 'max_concurrent_researchers': 2,
            'get_notes_from_tool_calls': lambda msgs: [m.content for m in msgs if getattr(m, 'type', '') == 'tool'],
            'researcher_agent': FakeResearcher(),
            'ToolMessage': ToolMessage, 'HumanMessage': HumanMessage, 'SystemMessage': HumanMessage,
            '_think_tool': SimpleNamespace(invoke=lambda _: 'think'),
            'attribute_sources': attribute_sources,
            'quarantine_update': seed.quarantine_update,
            'asyncio': asyncio,
            'logger': SimpleNamespace(info=lambda *_args: None, warning=lambda *_args: None),
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), env)
        s = state()
        action = SimpleNamespace(tool_calls=[{'name': 'ConductResearch',
                                              'args': {'research_topic': 'unit economics', 'aspect_id': 'aspect-01'},
                                              'id': 'call-1'}])
        supervisor_input = {
            'supervisor_messages': [action], 'research_iterations': 1,
            'quality_gate_version': 3, 'research_generation': 1,
            'research_brief': s['research_brief'],
            'required_aspects': s['required_aspects'],
            'checklist_fingerprint': s['checklist_fingerprint'],
        }
        answer = asyncio.run(env['supervisor_tools'](supervisor_input))
        self.assertEqual(answer.goto, 'supervisor')
        ledger = answer.update['research_evidence']
        self.assertEqual(len(ledger), 2)
        self.assertTrue(all(r['aspect_id'] == 'aspect-01' for r in ledger))
        finished_input = {**supervisor_input,
                          'supervisor_messages': answer.update['supervisor_messages'] + [
                              SimpleNamespace(tool_calls=[{'name': 'ResearchComplete'}])],
                          'research_iterations': 3, 'research_evidence': ledger}
        result = asyncio.run(env['supervisor_tools'](finished_input))
        self.assertEqual(result.goto, 'end')
        payload = result.update['speculative_research']
        self.assertEqual(len(payload['evidence_records']), 2)
        s['speculative_research'] = payload
        self.assertFalse(q.check_evidence_coverage(s)['missing'])

    def test_brief_node_sets_pinned_v3_plan_and_fence(self):
        path = ROOT / 'deep_research/agent_builder.py'
        tree = ast.parse(path.read_text(encoding='utf8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'write_research_brief')
        class Prompt:
            def format(self, **kw):
                return str(kw)
        class FakeDraftModel:
            async def ainvoke(self, messages):
                return SimpleNamespace(content=json.dumps({
                    'research_brief': 'Research electric vehicles',
                    'required_aspects': [{'title': 'Market size'}, {'title': 'Battery costs'}],
                }))
        class DummyLogger:
            def info(self, *args, **kw):
                return None
        # A V3 import of its prompt must be valid in this isolated AST harness.
        env = {
            'AgentState': dict, 'Any': object,
            'memory_read_enabled': lambda: False,
            'get_chat_model_auto': lambda *_a, **_kw: FakeDraftModel(),
            'RESEARCH_BRIEF_V3_PROMPT': Prompt(),
            'get_today_str': lambda: '2026-10-17',
            'HumanMessage': HumanMessage,
            'quality_gate_version_for_new_job': lambda: 3,
            'QUALITY_GATE_VERSION': 3,
            'parse_brief_plan': parse_brief_plan,
            'risk_tags_for_query': q.risk_tags_for_query,
            'build_research_seed': seed.build_research_seed,
            'seed_metrics': seed.seed_metrics,
            'render_seed_message': seed.render_seed_message,
            'initial_repair_limits': q.initial_repair_limits,
            'logger': DummyLogger(),
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), env)
        outcome = asyncio.run(env['write_research_brief']({
            'messages': [HumanMessage('新能源汽车行业投资前景')]
        }))
        self.assertEqual(outcome['research_generation'], 1)
        self.assertEqual(outcome['quality_gate_version'], 3)
        self.assertEqual(outcome['checklist_status'], 'proposed')
        self.assertIn('finance', outcome['quality_risk_tags'])
        self.assertEqual(outcome['checklist_fingerprint'],
                         checklist_fingerprint(outcome['required_aspects']))
        self.assertNotIn('notes', outcome)


if __name__ == '__main__':
    unittest.main(verbosity=2)
