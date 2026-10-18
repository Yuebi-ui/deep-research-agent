"""Genuine Quality Gate V3 graph/Checkpoint integration with fake providers.

Run in an environment with requirements.txt installed:
    APP_ENV=test pytest -q tests/test_quality_gate_v3.py
No external API keys or paid LLM calls are needed.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from deep_research.testing.fake_llm import set_responder


@pytest.fixture
def v3_env(monkeypatch):
    monkeypatch.setenv('DR_MAX_AUTO_RESEARCH_ROUNDS', '0')
    monkeypatch.setenv('DR_MAX_MANUAL_RESEARCH_ROUNDS', '2')

    def draft(messages):
        text = str(messages[-1].content)
        if 'Quality Gate V3' in text:
            return AIMessage(content=json.dumps({
                'research_brief': 'Research two independent perspectives on database architecture',
                'required_aspects': [{'title': 'Evidence about database architecture'}],
            }))
        return AIMessage(content='Initial database architecture report outline')

    def supervisor(messages):
        if any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content='', tool_calls=[{
                'name': 'ResearchComplete', 'args': {}, 'id': 'complete',
            }])
        return AIMessage(content='', tool_calls=[{
            'name': 'ConductResearch', 'args': {
                'research_topic': 'Find independent evidence about database architecture',
                'aspect_id': 'aspect-01',
            }, 'id': 'research',
        }])

    def evaluator(_messages):
        return AIMessage(content=json.dumps({
            'comprehensiveness_score': 8, 'accuracy_score': 8, 'coherence_score': 8,
            'reason': 'Sources observed; semantic caveats remain',
            'missing_aspects': [], 'need_more_research': False, 'issues': [],
        }))

    set_responder('draft', draft)
    set_responder('supervisor', supervisor)
    set_responder('evaluator', evaluator)
    set_responder('red_team', lambda _: AIMessage(content=json.dumps({
        'passed': True, 'feedback': 'PASS', 'issues': [],
    })))


def graph(*, saver=None):
    from deep_research.agent_builder import _create_builder
    return _create_builder(with_hitl=True, quality_gate_version=3).compile(
        checkpointer=saver or InMemorySaver(),
    )


async def drain(g, config, entry):
    steps = []
    async for item in g.astream(entry, config=config, stream_mode='updates'):
        steps.extend(key for key in item if key != '__interrupt__')
    return steps, await g.aget_state(config)


@pytest.mark.asyncio
async def test_v3_one_evaluator_and_skipped_red_team(v3_env):
    g = graph()
    config = {'configurable': {'thread_id': 'v3-green'}, 'recursion_limit': 80}
    steps, paused = await drain(g, config, {
        'messages': [HumanMessage(content='Compare database architecture choices')],
    })
    assert tuple(paused.next) == ('human_review',)
    assert paused.values['quality_gate_version'] == 3
    assert steps.count('precheck_quality_gate') == 1
    assert steps.count('evaluate_research') == 1
    assert steps.count('red_team_audit') == 0
    assert paused.values['red_team_review']['status'] == 'skipped'
    assert paused.values['evidence_coverage']['missing'] == []
    assert len(paused.values['speculative_research']['evidence_records']) >= 2
    assert not paused.values.get('notes')  # quarantined until human approves
    _, finished = await drain(g, config, Command(resume={'action': 'approve'}))
    assert finished.values['notes'] == paused.values['speculative_research']['notes']
    assert finished.values.get('final_report')


@pytest.mark.asyncio
async def test_v3_high_risk_routes_to_red_team(v3_env):
    g = graph()
    cfg = {'configurable': {'thread_id': 'v3-high-risk'}, 'recursion_limit': 80}
    steps, paused = await drain(g, cfg, {
        'messages': [HumanMessage(content='Legal advice on database architecture decisions')],
    })
    assert tuple(paused.next) == ('human_review',)
    assert steps.count('evaluate_research') == 1
    assert steps.count('red_team_audit') == 1
    assert paused.values['red_team_review']['status'] == 'completed'
    assert 'legal' in paused.values['quality_risk_tags']


@pytest.mark.asyncio
async def test_v3_missing_hard_evidence_skips_llm_judges(v3_env):
    def supervisor_without_aspect(messages):
        if any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content='', tool_calls=[{
                'name': 'ResearchComplete', 'args': {}, 'id': 'complete',
            }])
        return AIMessage(content='', tool_calls=[{
            'name': 'ConductResearch', 'args': {
                'research_topic': 'No mapped aspect for this search', 'aspect_id': '',
            }, 'id': 'research',
        }])

    set_responder('supervisor', supervisor_without_aspect)
    set_responder('evaluator', lambda _: (_ for _ in ()).throw(
        AssertionError('Evaluator must not run on a hard coverage failure')))
    g = graph()
    cfg = {'configurable': {'thread_id': 'v3-hard-gap'}, 'recursion_limit': 80}
    steps, paused = await drain(g, cfg, {'messages': [HumanMessage(content='Compare databases')]})
    assert tuple(paused.next) == ('human_review',)
    assert paused.values['quality_gate_decision']['reason'] == 'auto_budget_exhausted'
    assert paused.values['evidence_coverage']['missing']
    assert 'evaluate_research' not in steps
    assert 'red_team_audit' not in steps
    assert not paused.values.get('notes')


@pytest.mark.asyncio
async def test_v3_checkpoint_resumes_after_env_flag_flip(v3_env, monkeypatch):
    saver = InMemorySaver()
    cfg = {'configurable': {'thread_id': 'v3-restored'}, 'recursion_limit': 80}
    initial = graph(saver=saver)
    _, paused = await drain(initial, cfg, {
        'messages': [HumanMessage(content='Compare database designs')],
    })
    assert tuple(paused.next) == ('human_review',)
    monkeypatch.setenv('DR_QUALITY_GATE_V3', 'off')  # retired switch, ignored
    assert paused.values['quality_gate_version'] == 3
    from deep_research.agent_builder import _create_builder
    resumed = _create_builder(with_hitl=True, quality_gate_version=3).compile(checkpointer=saver)
    _, finished = await drain(resumed, cfg, Command(resume={'action': 'approve'}))
    assert finished.values.get('final_report')
    assert finished.values['quality_gate_version'] == 3
