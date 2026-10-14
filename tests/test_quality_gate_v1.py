"""V1 quality gate: pure fencing and offline actual graph tests.

Run with project's Fake LLM dependencies installed:
    pytest -q tests/test_quality_gate_v1.py
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from deep_research.quality_gate import (
    QUALITY_GATE_ENV,
    checked_candidate_notes,
    format_candidate_notes,
    quality_gate_enabled,
    quality_gate_lineage,
)
from deep_research.research_seed import (
    ResearchFenceError,
    build_speculative_payload,
)
from deep_research.testing.fake_llm import set_responder


@pytest.fixture
def quality_env(monkeypatch):
    monkeypatch.setenv("DR_SEED_LITE_SPECULATIVE", "on")
    monkeypatch.setenv(QUALITY_GATE_ENV, "on")


def _state(generation=1):
    return {
        "quality_gate_version": 1, "research_generation": generation,
        "research_brief": "compare databases",
        "speculative_research": build_speculative_payload(
            generation=generation, research_brief="compare databases", notes=["evidence A", "evidence B"],
        ),
    }


def test_candidate_requires_current_generation_and_matching_brief():
    good = _state()
    assert quality_gate_lineage(good)
    assert checked_candidate_notes(good) == ["evidence A", "evidence B"]
    assert "notes" not in good  # read before approval must not admit evidence

    for change in (
        {"research_generation": 2},
        {"research_brief": "different brief"},
        {"speculative_research": None},
        {"quality_gate_version": 0},
        {"research_generation": 0},
    ):
        altered = dict(good, **change)
        with pytest.raises(ResearchFenceError):
            checked_candidate_notes(altered)


def test_context_limits_and_empty_notes():
    assert "No research" in format_candidate_notes([])
    assert "omitted" in format_candidate_notes(["one"] * 5, max_items=2)
    assert "truncated" in format_candidate_notes(["x" * 500], max_chars=50)
    assert "omitted" in format_candidate_notes(["x"], max_items=0)


def test_quality_flag_is_opt_in(monkeypatch):
    monkeypatch.delenv(QUALITY_GATE_ENV, raising=False)
    assert quality_gate_enabled() is False
    monkeypatch.setenv(QUALITY_GATE_ENV, "on")
    assert quality_gate_enabled() is True
    monkeypatch.setenv(QUALITY_GATE_ENV, "off")
    assert quality_gate_enabled() is False


async def _drain(graph, cfg, inp):
    order = []
    async for chunk in graph.astream(inp, config=cfg, stream_mode="updates"):
        order.extend(k for k in chunk if k != "__interrupt__")
    return order, await graph.aget_state(cfg)


def _graph(*, with_hitl=True):
    from deep_research.agent_builder import _create_builder

    return _create_builder(
        with_hitl=with_hitl, speculative=True, quality_gate=True,
    ).compile(checkpointer=InMemorySaver() if with_hitl else None)


@pytest.mark.asyncio
async def test_quality_gate_join_review_then_approve(quality_env):
    graph = _graph()
    cfg = {"configurable": {"thread_id": "quality-approve"}, "recursion_limit": 80}
    order, pause = await _drain(graph, cfg, {"messages": [HumanMessage(content="compare databases")]})

    assert tuple(pause.next) == ("human_review",)
    assert order[:1] == ["write_research_brief"]
    assert {"write_draft_report", "supervisor_subgraph"}.issubset(order)
    assert order.index("evaluate_research") > max(order.index("write_draft_report"), order.index("supervisor_subgraph"))
    assert order.index("red_team_audit") > order.index("evaluate_research")
    assert pause.values["quality_review"]["status"] == "completed"
    assert pause.values["red_team_review"]["passed"] is True
    assert pause.values["quality_review"]["generation"] == 1
    assert pause.values.get("notes") in (None, [])  # no pre-approval commit
    assert "draft_report" in pause.values

    downstream, done = await _drain(graph, cfg, Command(resume={"action": "approve"}))
    assert "claim_verification" in downstream and "final_report_generation" in downstream
    assert done.values["notes"] == pause.values["speculative_research"]["notes"]
    assert done.values.get("final_report")


@pytest.mark.asyncio
async def test_quality_gate_revise_reruns_quality_and_fences_old_research(quality_env):
    graph = _graph()
    cfg = {"configurable": {"thread_id": "quality-revise"}, "recursion_limit": 80}
    _, first = await _drain(graph, cfg, {"messages": [HumanMessage(content="compare databases")]})
    old = first.values["speculative_research"]["notes"]
    order, second = await _drain(graph, cfg, Command(resume={"action": "revise", "feedback": "More comparison"}))
    assert tuple(second.next) == ("human_review",)
    assert second.values["research_generation"] == 2
    assert second.values["quality_review"]["generation"] == 2
    assert second.values["red_team_review"]["generation"] == 2
    assert order.count("evaluate_research") == 1
    assert order.count("red_team_audit") == 1
    assert order.count("regenerate_research") == 1
    assert second.values.get("notes") in (None, [])
    assert "draft_report" in second.values
    _, done = await _drain(graph, cfg, Command(resume={"action": "approve"}))
    assert done.values["notes"] == second.values["speculative_research"]["notes"]
    assert done.values["research_generation"] == 2


@pytest.mark.asyncio
async def test_nonpass_feedback_is_reviewed_not_auto_repaired(quality_env):
    def judge(_):
        return AIMessage(content=json.dumps({
            "comprehensiveness_score": 4,
            "accuracy_score": 2,
            "coherence_score": 6,
            "reason": "Insufficient source support",
            "missing_aspects": ["pricing evidence"],
            "need_more_research": True,
        }))

    set_responder("evaluator", judge)
    set_responder("red_team", lambda _: AIMessage(content="Need independent market evidence."))
    graph = _graph()
    cfg = {"configurable": {"thread_id": "quality-nonpass"}, "recursion_limit": 80}
    order, pause = await _drain(graph, cfg, {"messages": [HumanMessage(content="compare databases")]})
    assert tuple(pause.next) == ("human_review",)
    assert pause.values["quality_review"]["need_more_research"] is True
    assert pause.values["quality_review"]["overall_score"] == 4
    assert pause.values["red_team_review"]["passed"] is False
    assert "regenerate_research" not in order  # automatic correction is V2


@pytest.mark.asyncio
async def test_with_no_hitl_reviews_still_precede_acceptance(quality_env):
    graph = _graph(with_hitl=False)
    order = []
    final = {}
    async for chunk in graph.astream(
        {"messages": [HumanMessage(content="compare databases")]},
        config={"recursion_limit": 80}, stream_mode="updates",
    ):
        order.extend(chunk.keys())
        if "final_report_generation" in chunk:
            final = chunk["final_report_generation"]
    assert order.index("evaluate_research") < order.index("red_team_audit") < order.index("accept_research")
    assert order.count("accept_research") == 1
    assert final.get("final_report")


@pytest.mark.asyncio
async def test_stale_candidate_fails_before_human_approval(quality_env):
    graph = _graph()
    cfg = {"configurable": {"thread_id": "quality-stale"}, "recursion_limit": 80}
    _, pause = await _drain(graph, cfg, {"messages": [HumanMessage(content="compare databases")]})
    assert tuple(pause.next) == ("human_review",)
    stale = dict(pause.values["speculative_research"])
    stale["generation"] = 0
    await graph.aupdate_state(cfg, {"speculative_research": stale}, as_node="red_team_audit")
    with pytest.raises(ResearchFenceError):
        await _drain(graph, cfg, Command(resume={"action": "approve"}))
    snapshot = await graph.aget_state(cfg)
    assert snapshot.values.get("notes") in (None, [])
