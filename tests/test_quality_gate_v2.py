"""V2 genuine LangGraph integration tests with Fake LLM (no paid APIs).

Requires project dependencies. For dependency-free real-node-body coverage run:
    python scripts/test_quality_gate_v2_offline.py
"""
from __future__ import annotations

import json
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from deep_research.research_seed import ResearchFenceError
from deep_research.testing.fake_llm import set_responder


@pytest.fixture
def v2_env(monkeypatch):
    monkeypatch.setenv('DR_SEED_LITE_SPECULATIVE', 'on')
    monkeypatch.setenv('DR_QUALITY_GATE_V1', 'off')
    monkeypatch.setenv('DR_QUALITY_GATE_V2', 'on')
    monkeypatch.setenv('DR_MAX_AUTO_RESEARCH_ROUNDS', '2')
    monkeypatch.setenv('DR_MAX_MANUAL_RESEARCH_ROUNDS', '2')


def _graph(*, hitl=True):
    from deep_research.agent_builder import _create_builder
    return _create_builder(
        with_hitl=hitl, speculative=True, quality_gate_version=2,
    ).compile(checkpointer=InMemorySaver() if hitl else None)


async def _drain(graph, cfg, entry):
    steps = []
    async for chunk in graph.astream(entry, config=cfg, stream_mode='updates'):
        steps.extend(k for k in chunk if k != '__interrupt__')
    return steps, await graph.aget_state(cfg)


def _judge_response(*, need, missing=()):
    return AIMessage(content=json.dumps({
        'comprehensiveness_score': 4 if need else 8,
        'accuracy_score': 4 if need else 8,
        'coherence_score': 7,
        'reason': 'missing sources' if need else 'sufficient evidence',
        'missing_aspects': list(missing),
        'need_more_research': need,
    }))


@pytest.mark.asyncio
async def test_v2_auto_feedback_reruns_evaluator_before_hitl(v2_env):
    calls = {'n': 0}

    def judge(_messages):
        calls['n'] += 1
        return _judge_response(need=(calls['n'] == 1), missing=['independent source'] if calls['n'] == 1 else [])

    set_responder('evaluator', judge)
    graph = _graph()
    cfg = {'configurable': {'thread_id': 'v2-feedback'}, 'recursion_limit': 80}
    steps, checkpoint = await _drain(graph, cfg, {'messages': [HumanMessage(content='compare databases')]})

    assert tuple(checkpoint.next) == ('human_review',)
    assert steps.count('targeted_research') == 1
    assert steps.count('evaluate_research') == 2
    assert steps.count('red_team_audit') == 2
    assert steps.index('targeted_research') > steps.index('red_team_audit')
    assert calls['n'] == 2
    assert checkpoint.values['quality_repair_count'] == 1
    assert checkpoint.values['research_generation'] == 2
    assert checkpoint.values['quality_review']['generation'] == 2
    assert checkpoint.values['red_team_review']['generation'] == 2
    assert not checkpoint.values.get('notes')
    assert checkpoint.values['quality_repair_history'][0]['source'] == 'auto'

    _, final = await _drain(graph, cfg, Command(resume={'action': 'approve'}))
    assert final.values['notes'] == checkpoint.values['speculative_research']['notes']
    assert final.values.get('final_report')


@pytest.mark.asyncio
async def test_v2_stops_after_no_new_sources(v2_env):
    set_responder('evaluator', lambda _: _judge_response(need=True, missing=['price source']))
    graph = _graph()
    cfg = {'configurable': {'thread_id': 'v2-stagnated'}, 'recursion_limit': 80}
    steps, paused = await _drain(graph, cfg, {'messages': [HumanMessage(content='compare databases')]})
    assert steps.count('targeted_research') == 1  # fake compressor emits the same notes
    assert paused.values['quality_repair_history'][-1]['new_notes_count'] == 0
    assert paused.values['quality_gate_decision']['reason'] == 'no_new_evidence'
    assert tuple(paused.next) == ('human_review',)
    assert not paused.values.get('notes')


@pytest.mark.asyncio
async def test_v2_revise_draft_does_not_research_again(v2_env):
    graph = _graph()
    cfg = {'configurable': {'thread_id': 'v2-edit'}, 'recursion_limit': 80}
    _, first = await _drain(graph, cfg, {'messages': [HumanMessage(content='compare databases')]})
    before = first.values['speculative_research']
    steps, edited = await _drain(
        graph, cfg, Command(resume={'action': 'revise_draft', 'feedback': 'Reorder the outline'})
    )
    assert tuple(edited.next) == ('human_review',)
    assert steps.count('targeted_research') == 0
    assert steps.count('supervisor_subgraph') == 0
    assert steps.count('evaluate_research') == 1
    assert edited.values['research_generation'] == first.values['research_generation']
    assert edited.values['speculative_research'] == before
    assert edited.values['draft_report']


@pytest.mark.asyncio
async def test_v2_manual_research_is_targeted_and_capped(v2_env):
    graph = _graph()
    cfg = {'configurable': {'thread_id': 'v2-manual'}, 'recursion_limit': 80}
    _, first = await _drain(graph, cfg, {'messages': [HumanMessage(content='compare databases')]})
    assert first.values['manual_research_count'] == 0
    for round_number in (1, 2):
        steps, current = await _drain(
            graph, cfg, Command(resume={
                'action': 'more_research', 'feedback': f'Find newer source {round_number}',
            }),
        )
        assert tuple(current.next) == ('human_review',)
        assert steps.count('targeted_research') == 1
        assert current.values['manual_research_count'] == round_number
        assert current.values['quality_repair_history'][-1]['source'] == 'human'
        assert not current.values.get('notes')
    assert current.values['research_generation'] == 3
    assert current.values['quality_repair_count'] == 0
    _, finished = await _drain(graph, cfg, Command(resume={'action': 'approve'}))
    assert finished.values['notes'] == current.values['speculative_research']['notes']


@pytest.mark.asyncio
async def test_v2_reviewers_unavailable_go_to_hitl_without_auto_spend(v2_env):
    def outage(_messages):
        raise RuntimeError('fake judge outage')
    set_responder('evaluator', outage)
    graph = _graph()
    cfg = {'configurable': {'thread_id': 'v2-judge-outage'}, 'recursion_limit': 80}
    steps, checkpoint = await _drain(graph, cfg, {'messages': [HumanMessage(content='compare databases')]})
    assert tuple(checkpoint.next) == ('human_review',)
    assert checkpoint.values['quality_review']['status'] == 'error'
    assert checkpoint.values['quality_gate_decision']['reason'] == 'reviewer_unavailable'
    assert 'targeted_research' not in steps
    assert checkpoint.values['quality_repair_count'] == 0


@pytest.mark.asyncio
async def test_v2_no_hitl_refuses_unresolved_quality(v2_env, monkeypatch):
    monkeypatch.setenv('DR_MAX_AUTO_RESEARCH_ROUNDS', '0')
    set_responder('evaluator', lambda _: _judge_response(need=True, missing=['verified source']))
    graph = _graph(hitl=False)
    with pytest.raises(ResearchFenceError, match='requires human review'):
        await graph.ainvoke({'messages': [HumanMessage(content='compare databases')]},
                            config={'recursion_limit': 80})


@pytest.mark.asyncio
async def test_v2_resume_uses_checkpointed_version_after_flag_flip(v2_env, monkeypatch):
    from deep_research.agent_builder import _create_builder

    saver = InMemorySaver()
    cfg = {'configurable': {'thread_id': 'v2-flag-flip'}, 'recursion_limit': 80}
    graph = _create_builder(with_hitl=True, speculative=True, quality_gate_version=2).compile(checkpointer=saver)
    _, paused = await _drain(graph, cfg, {'messages': [HumanMessage(content='compare databases')]})
    assert paused.values['quality_gate_version'] == 2

    monkeypatch.setenv('DR_QUALITY_GATE_V2', 'off')
    monkeypatch.setenv('DR_QUALITY_GATE_V1', 'on')
    recovered = _create_builder(with_hitl=True, speculative=True,
                                quality_gate_version=paused.values['quality_gate_version']).compile(checkpointer=saver)
    _, finished = await _drain(recovered, cfg, Command(resume={'action': 'approve'}))
    assert finished.values['final_report']
    assert finished.values['quality_gate_version'] == 2
