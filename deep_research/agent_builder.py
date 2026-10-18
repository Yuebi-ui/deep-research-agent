"""Build the LangGraph research workflow and its task-facing nodes.

The graph owns research planning, HITL, verification and final report generation.
Long-term memory is accessed through ``deep_research.memory.runtime`` so the
memory subsystem does not depend back on this graph builder.
"""

import asyncio
import json
import os
import sqlite3
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from deep_research.utils import get_today_str
from deep_research.states import AgentState, AgentInputState
from deep_research.prompts.research_brief import RESEARCH_BRIEF_V3_PROMPT
from deep_research.research_seed import (
    ResearchFenceError,
    active_generation,
    build_research_seed,
    fenced_accept_update,
    build_speculative_payload,
    seed_fingerprint,
    render_seed_message,
    seed_metrics,
)
from deep_research.agents import supervisor_agent
from deep_research.agents.draft_agent import write_draft_report
from deep_research.agents.evaluator_agent import evaluate_research_readiness
from deep_research.agents.red_team_agent import review_research_bundle_v3
from deep_research.quality_gate import (
    QUALITY_GATE_VERSION, v3_lineage, checked_candidate_notes,
    checked_candidate_evidence, check_evidence_coverage, build_review_context,
    build_human_review_summary, choose_research_repair_v3, risk_tags_for_query,
    initial_repair_limits, merge_candidate_notes, format_followup_request,
)
from deep_research.evidence import parse_brief_plan, merge_evidence
from deep_research.memory.runtime import get_memory_manager
from deep_research.memory.flags import memory_read_enabled, memory_write_enabled
from deep_research.writer_context import build_final_report_prompt, unique_notes
from deep_research.writer_validation import validate_report_citations
from deep_research.llm import get_chat_model, get_chat_model_auto, writer_thinking
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)

# ===== Config =====

# The final-writer reasoning policy is process-scoped and configurable through
# DR_WRITER_THINKING. The model is constructed at import time, so changing the
# setting requires a process restart. The current default follows
# ``writer_thinking()`` (off unless explicitly enabled).
writer_model = get_chat_model("writer", thinking=writer_thinking())


def _get_memory_manager():
    """Compatibility wrapper around the memory composition root.

    Older integrations imported this private helper from ``agent_builder``. New
    code should depend on :func:`deep_research.memory.runtime.get_memory_manager`
    directly so the memory layer never needs to import the graph builder.
    """
    return get_memory_manager()


def extract_user_query(messages: Any) -> str:
    """从 state.messages 提取用户 query。

    与 `final_report_generation` 历史上的取法逐字一致（取第一条，content 非
    字符串时退化为 str(...)），供 **post-completion 记忆落库**复用。
    """
    if not messages:
        return ""
    first = messages[0]
    content = getattr(first, "content", None)
    return content if isinstance(content, str) else str(first)


def store_report_memory(user_query: str, report: str) -> Any:
    """把完成报告写入派生记忆。

    默认 durable 模式由 ``MemoryOutboxProcessor`` 在任务完成之后调用；关闭
    ``DR_MEMORY_OUTBOX_ENABLED`` 时，runner 才会走旧的 best-effort fallback。
    无论哪条路径，最终报告本身都是任务事实源，记忆只属于派生数据。
    """
    if not memory_write_enabled() or not report:
        return None
    return _get_memory_manager().store_from_report(user_query, report)


# ===== RESEARCH BRIEF (with Memory Injection) =====

async def write_research_brief(state: AgentState) -> dict[str, Any]:
    """生成研究简报，同时注入历史记忆作为上下文参考。"""

    messages = state.get("messages", [])
    # 提取用户最新 query 用于记忆检索和智能路由
    user_query = messages[-1].content if messages else ""

    # 检索历史记忆（同步 chroma + embedding 调用 → 放线程，避免阻塞事件循环）
    memory_context = ""
    if memory_read_enabled():
        try:
            mgr = _get_memory_manager()
            memory_context = await asyncio.to_thread(mgr.retrieve_context, user_query)
            if memory_context:
                logger.info("Injected memory context into research_brief")
        except Exception as e:
            logger.warning("Memory retrieval failed: %s", e)

    # 智能路由选择模型
    draft_model = get_chat_model_auto("draft", query_text=user_query)

    prompt = RESEARCH_BRIEF_V3_PROMPT.format(
        messages=messages,
        date=get_today_str()
    )

    # 注入记忆上下文
    if memory_context:
        prompt = memory_context + "\n\n" + prompt

    response = await draft_model.ainvoke([HumanMessage(content=prompt)])
    plan = parse_brief_plan(str(response.content), str(user_query))
    research_brief = plan["research_brief"]

    # The production graph is parallel-only: the Brief seeds Supervisor and
    # fans out to Draft and Research in the same LangGraph superstep.
    seed = build_research_seed(research_brief)
    metrics = seed_metrics(seed)
    logger.info(
        "parallel research seed built: fingerprint=%s chars=%d generation=1",
        metrics["seed_fingerprint"], metrics["seed_message_chars"],
    )
    return {
        "research_brief": research_brief,
        "supervisor_messages": [render_seed_message(seed), research_brief],
        "research_generation": 1,
        "quality_gate_version": QUALITY_GATE_VERSION,
        **{k: plan[k] for k in ("required_aspects", "checklist_status", "checklist_fingerprint")},
        "quality_risk_tags": risk_tags_for_query(str(user_query)),
        **initial_repair_limits(),
        "quality_repair_count": 0,
        "manual_research_count": 0,
        "quality_repair_history": [],
    }


# ===== HUMAN REVIEW (HITL) — V3 only =====

async def human_review(state: AgentState) -> Command[str]:
    """Human-only acceptance of quarantined evidence; no auto-approval."""
    if not v3_lineage(state):
        raise ResearchFenceError("legacy checkpoint cannot resume in V3")
    research_notes = checked_candidate_notes(state)
    checked_candidate_evidence(state)
    draft = state.get("draft_report", "") or ""
    manual_used = state.get("manual_research_count", 0)
    manual_limit = state.get("manual_research_limit", 2)
    options = ["approve", "revise_draft"]
    if manual_used < manual_limit:
        options.append("more_research")
    summary = build_human_review_summary(state)
    review = interrupt({
        "message": "研究已到人工审查环节；结构检查不等于事实核验",
        "draft_report_preview": draft[:1000] + ("..." if len(draft) > 1000 else ""),
        "research_brief": str(state.get("research_brief", ""))[:500],
        "options": options,
        "review_summary": summary,
        "gate_decision": state.get("quality_gate_decision"),
        "evidence_coverage": state.get("evidence_coverage"),
        "required_aspects": state.get("required_aspects"),
        "checklist_status": state.get("checklist_status"),
        "quality_repair_history": state.get("quality_repair_history") or [],
        "auto_research_rounds": state.get("quality_repair_count", 0),
        "manual_research_rounds": manual_used,
        "manual_research_limit": manual_limit,
        "quality_review": state.get("quality_review"),
        "red_team_review": state.get("red_team_review"),
        "research_notes_count": len(research_notes),
        "research_notes_preview": [note[:500] for note in research_notes[:3]],
        "research_generation": active_generation(state),
    })
    action = review if isinstance(review, str) else (review.get("action") if isinstance(review, dict) else None)
    if action not in ("approve", "revise_draft", "more_research"):
        raise ValueError(f"Unsupported V3 HITL review action: {action!r}")
    if action == "more_research":
        feedback = str(review.get("feedback", "")).strip() if isinstance(review, dict) else ""
        if not 1 <= len(feedback) <= 500:
            raise ValueError("more_research requires feedback (1-500 characters)")
        if manual_used >= manual_limit:
            return Command(goto="human_review", update={
                "quality_gate_decision": {
                    "action": "human_review", "reason": "manual_budget_exhausted",
                    "generation": active_generation(state),
                },
            })
        return Command(goto="targeted_research", update={
            "repair_request": {
                "source": "human", "from_generation": active_generation(state),
                "topics": [feedback],
            },
        })
    if action == "revise_draft":
        feedback = str(review.get("feedback", "")) if isinstance(review, dict) else ""
        response = await writer_model.ainvoke([HumanMessage(content=(
            f"请根据以下反馈修改报告草稿：\n{feedback}\n\n当前草稿：\n{draft}"))])
        # Copy-edit does not invalidate independent research or spend a research call.
        return Command(goto="precheck_quality_gate", update={
            "draft_report": response.content, "quality_review": None, "red_team_review": None,
        })
    # Only explicit HITL approve admits speculative notes to authoritative state.
    updates = fenced_accept_update(state)
    logger.info("HITL accepted evidence generation %d (%d notes)",
                active_generation(state), len(updates["notes"]))
    return Command(goto="claim_verification", update=updates)


# ===== PHASE 1: INDEPENDENT QUALITY GATE (speculative lineage only) =====

async def evaluate_research(state: AgentState) -> dict:
    """V3 semantic analysis/unknown omissions, after complete-ledger precheck."""
    notes = checked_candidate_notes(state)
    generation = active_generation(state)
    scope: dict[str, Any] = {}
    try:
        note_context, manifest, scope = build_review_context(state)
        evaluation = await evaluate_research_readiness(
            research_brief=str(state.get("research_brief") or "")[:12000],
            draft_report=str(state.get("draft_report") or "")[:16000],
            research_notes=note_context,
            checklist=json.dumps(state.get("required_aspects") or [], ensure_ascii=False),
            evidence_manifest=manifest,
            version=3,
        )
        import math

        scores = (evaluation.comprehensiveness_score,
                  evaluation.accuracy_score, evaluation.coherence_score)
        if any(not math.isfinite(v) or not (0 <= v <= 10) for v in scores):
            raise ValueError(f"invalid evaluator scores: {scores!r}")
        if evaluation.need_more_research and not any(
            item.kind in ("evidence_gap", "hidden_omission", "counterevidence")
            and item.severity in ("high", "critical") and item.research_topic.strip()
            for item in evaluation.issues
        ):
            raise ValueError("evaluator requires research without actionable issue")
        observed = {item.get("source_id") for item in checked_candidate_evidence(state)}
        if any(ref not in observed for issue in evaluation.issues for ref in issue.evidence_refs):
            raise ValueError("evaluator cited a source outside fenced evidence ledger")
        assessment = {
            "status": "completed", "generation": generation,
            "notes_count": len(notes),
            "comprehensiveness_score": scores[0], "accuracy_score": scores[1],
            "coherence_score": scores[2], "overall_score": round(sum(scores) / len(scores), 2),
            "reason": evaluation.reason, "missing_aspects": evaluation.missing_aspects,
            "need_more_research": evaluation.need_more_research,
            "issues": [item.model_dump() for item in evaluation.issues],
            "context_scope": scope,
        }
    except Exception as exc:
        logger.warning("V3 evaluator unavailable: %s", exc)
        assessment = {
            "status": "error", "generation": generation,
            "notes_count": len(notes), "error": str(exc)[:240],
            "need_more_research": None, "context_scope": scope,
        }
    return {"quality_review": assessment}


async def red_team_review(state: AgentState) -> dict:
    """V3 selective adversarial test; no duplicate ordinary coverage grading."""
    notes = checked_candidate_notes(state)
    generation = active_generation(state)
    scope: dict[str, Any] = {}
    try:
        note_context, manifest, scope = build_review_context(state)
        verdict = await review_research_bundle_v3(
            research_brief=str(state.get("research_brief") or "")[:12000],
            draft_report=str(state.get("draft_report") or "")[:16000],
            research_notes=note_context,
            checklist=json.dumps(state.get("required_aspects") or [], ensure_ascii=False),
            evidence_manifest=manifest,
        )
        observed = {item.get("source_id") for item in checked_candidate_evidence(state)}
        if any(ref not in observed for issue in verdict["issues"] for ref in issue["evidence_refs"]):
            raise ValueError("Red Team cited source outside fenced evidence ledger")
        review = {"status": "completed", "generation": generation,
                  "notes_count": len(notes), **verdict, "context_scope": scope}
    except Exception as exc:
        logger.warning("V3 red team unavailable: %s", exc)
        review = {"status": "error", "generation": generation,
                  "notes_count": len(notes), "passed": None,
                  "error": str(exc)[:240], "context_scope": scope}
    return {"red_team_review": review}


# ===== QUALITY GATE V3: deterministic preflight and nonredundant routing =====

def _v3_route_command(state: AgentState, outcome: dict[str, Any], *, with_hitl: bool) -> Command[str]:
    """One command router for BOTH V3 gate stages; nothing writes authoritative notes."""
    action = outcome["action"]
    update: dict[str, Any] = {"quality_gate_decision": outcome}
    if action == "targeted_research":
        update["repair_request"] = {
            "source": "auto", "from_generation": active_generation(state),
            "topics": outcome["topics"],
        }
        return Command(goto="targeted_research", update=update)
    if action in ("evaluate_research", "red_team_audit"):
        if action == "red_team_audit":
            update["red_team_review"] = None
        return Command(goto=action, update=update)
    if action != "human_review":
        raise ResearchFenceError("unrecognized V3 gate outcome")
    update["repair_request"] = None
    # Optional red team must not be mistaken for a passing review.
    if outcome.get("phase") == "evaluator" and not state.get("quality_risk_tags"):
        update["red_team_review"] = {
            "status": "skipped", "generation": active_generation(state),
            "reason": "no_high_risk_trigger", "passed": None,
        }
    # New model-proposed checklists require HITL sign-off.  Even when no
    # checklist gaps remain, no-HITL test runs MUST NOT silently auto-accept.
    if not with_hitl:
        return Command(goto="quality_gate_failed", update=update)
    return Command(goto="human_review", update=update)


async def precheck_quality_gate(state: AgentState, *, with_hitl: bool = True) -> Command[str]:
    report = check_evidence_coverage(state)
    # The check output is deterministic; decide from an explicitly refreshed
    # report rather than an older checkpoint value.
    outcome = choose_research_repair_v3({**state, "evidence_coverage": report}, phase="precheck")
    command = _v3_route_command(state, outcome, with_hitl=with_hitl)
    command.update["evidence_coverage"] = report
    return command


async def route_quality_gate_v3(state: AgentState, *, with_hitl: bool = True) -> Command[str]:
    checked_candidate_evidence(state)
    red = state.get("red_team_review")
    phase = "red_team" if isinstance(red, dict) and (
        red.get("generation") == active_generation(state) and red.get("status") in ("completed", "error")
    ) else "evaluator"
    outcome = choose_research_repair_v3(state, phase=phase)
    return _v3_route_command(state, outcome, with_hitl=with_hitl)


async def targeted_research(state: AgentState, config: RunnableConfig) -> dict:
    """Run focused Supervisor+Researcher on missing evidence, not a full rewrite.

    The old candidate is read through the generation/brief fence, the child
    runs in fresh isolated state, and only *after successful completion* do we
    atomically checkpoint a NEW generation containing old+new notes. ``notes``
    is never written here; only HITL approval can commit it downstream.
    """
    if not v3_lineage(state):
        raise ResearchFenceError("targeted research requires a V3 checkpoint")
    previous = checked_candidate_notes(state)
    previous_evidence = checked_candidate_evidence(state)
    request = state.get("repair_request")
    gen = active_generation(state)
    if not isinstance(request, dict) or request.get("from_generation") != gen:
        raise ResearchFenceError("stale or missing supplemental research request")
    source = request.get("source")
    if source not in ("auto", "human"):
        raise ResearchFenceError("supplemental request must come from an authorized reviewer")
    topics = request.get("topics")
    brief = state.get("research_brief", "") or ""
    focus = format_followup_request(brief, topics, previous)
    count_key = "quality_repair_count" if source == "auto" else "manual_research_count"
    limit_key = "quality_repair_limit" if source == "auto" else "manual_research_limit"
    count = state.get(count_key, 0)
    limit = state.get(limit_key, 2)
    max_limit = 3 if source == "auto" else 5
    if type(count) is not int or type(limit) is not int or not 0 <= count < limit <= max_limit:
        raise ResearchFenceError(f"{source} supplemental research budget exhausted or invalid")

    next_gen = gen + 1
    seed = build_research_seed(brief)
    child_input = {
        "supervisor_messages": [render_seed_message(seed), HumanMessage(content=focus)],
        "research_brief": brief, "draft_report": "", "research_iterations": 0,
        "notes": [], "raw_notes": [],
        "research_trace": [], "supervisor_memory_hint": "",
        "research_generation": next_gen, "quality_gate_version": QUALITY_GATE_VERSION,
        "required_aspects": state.get("required_aspects") or [],
        "checklist_fingerprint": state.get("checklist_fingerprint"),
        "research_evidence": [],
    }
    # Child must not inherit parent's thread_id or checkpoint. Keep callbacks,
    # tracing/tags, and recursion budget only.
    child_config: dict[str, Any] = {k: v for k, v in (config or {}).items() if k != "configurable"}
    child_config["recursion_limit"] = max(50, int(child_config.get("recursion_limit", 50)))
    result = await supervisor_agent.ainvoke(child_input, config=child_config)
    payload = result.get("speculative_research")
    if not isinstance(payload, dict) or payload.get("generation") != next_gen or (
        payload.get("seed_fingerprint") != seed_fingerprint(seed)
    ):
        raise ResearchFenceError("supplemental Supervisor returned a stale/mismatched generation")
    merged, added = merge_candidate_notes(previous, payload.get("notes"))
    if payload.get("checklist_fingerprint") != state.get("checklist_fingerprint") or (
        not isinstance(payload.get("evidence_records"), list)
    ):
        raise ResearchFenceError("supplemental V3 evidence checklist mismatch")
    merged_evidence, evidence_added = merge_evidence(
        previous_evidence, payload["evidence_records"],
    )
    records_added = len(merged_evidence) - len(previous_evidence)
    history = list(state.get("quality_repair_history") or [])
    history.append({
        "source": source, "generation": next_gen, "requested_topics": list(topics),
        "before_notes_count": len(previous), "new_notes_count": added,
        "new_evidence_count": evidence_added,
        "new_records_count": records_added,
        "after_notes_count": len(merged), "round": count + 1,
    })
    update = {
        "research_generation": next_gen,
        "speculative_research": build_speculative_payload(
            generation=next_gen, research_brief=brief, notes=merged,
            evidence_records=merged_evidence,
            checklist_fingerprint=state.get("checklist_fingerprint"),
        ),
        count_key: count + 1,
        "quality_repair_history": history,
        "quality_review": None, "red_team_review": None,
        "evidence_coverage": None,
        "repair_request": None,
    }
    traces = result.get("research_trace")
    if isinstance(traces, list) and traces:
        update["research_trace"] = traces
    logger.info("targeted research %s gen %d -> %d, %d new/%d total notes",
                source, gen, next_gen, added, len(merged))
    return update


async def quality_gate_failed(state: AgentState) -> dict:
    """No-HITL runs cannot override unresolved, failed or exhausted audits."""
    decision = state.get("quality_gate_decision") or {}
    raise ResearchFenceError(
        "V3 research quality requires human review: " + str(decision.get("reason", "unknown"))
    )


# ===== CLAIM VERIFICATION =====

async def claim_verification(state: AgentState) -> dict:
    """报告草稿的事实核查：提取关键 Claim → 搜索验证 → 生成核查报告。

    完全容错：任何步骤失败都优雅跳过，不影响主流程。
    """
    try:
        from deep_research.verification.claim_extractor import ClaimExtractor
        from deep_research.verification.claim_verifier import ClaimVerifier
        from deep_research.verification.schemas import VerificationReport

        draft = state.get("draft_report", "")
        if not draft or len(draft) < 100:
            logger.info("Skipping claim verification: draft too short")
            return {"verification_report": None, "claim_verification_warning": ""}

        extractor = ClaimExtractor()
        verifier = ClaimVerifier()

        # 同步 LLM 调用 → 线程（不阻塞事件循环，保护 heartbeat）
        claims = await asyncio.to_thread(extractor.extract, draft)
        if not claims:
            logger.info("No claims extracted from draft")
            return {"verification_report": None, "claim_verification_warning": ""}

        verdicts = await verifier.verify(claims)
        report = VerificationReport.from_verdicts(verdicts)
        logger.info(
            "Verification complete: %d/%d supported, hallucination_rate=%.1f%%",
            report.supported, report.total_claims, report.hallucination_rate * 100,
        )

        warning = ""
        if report.unsupported > 0:
            unsupported_texts = [v.claim_text[:100] for v in verdicts if v.verdict == "UNSUPPORTED"]
            warning = f"\n[事实核查警告] 以下 {report.unsupported} 条断言缺乏证据支持，请在最终报告中标注或移除：\n" + \
                      "\n".join(f"- {t}" for t in unsupported_texts)

        return {
            "verification_report": report.model_dump() if report else None,
            "claim_verification_warning": warning,
        }
    except Exception as e:
        logger.warning("Claim verification failed (skipping): %s", e)
        return {"verification_report": None, "claim_verification_warning": ""}


# ===== FINAL REPORT GENERATION =====

async def final_report_generation(state: AgentState):
    """最终报告的生成: 用户query，研究简报，findings, 报告初稿 => 报告"""

    notes = state.get("notes", [])
    report_context, build_stats = build_final_report_prompt(
        research_brief=state.get("research_brief", "") or "",
        draft_report=state.get("draft_report", "") or "",
        notes=notes,
        date=get_today_str(),
        verification_warning=state.get("claim_verification_warning", "") or "",
    )
    logger.info("Writer context build: %s", build_stats)
    final_report_prompt = report_context

    # 流式生成：逐 token 输出，前端可以实时渲染
    report_chunks: list[str] = []
    async for chunk in writer_model.astream([HumanMessage(content=final_report_prompt)]):
        if chunk.content:
            report_chunks.append(chunk.content)
    report_content = "".join(report_chunks)

    citation_validation = validate_report_citations(
        report_content,
        "\n\n".join([
            state.get("research_brief", "") or "",
            state.get("draft_report", "") or "",
            *unique_notes(notes),
        ]),
    )
    if not citation_validation["ok"]:
        logger.warning("Final writer citation check: %s", citation_validation["issues"])

    # Phase 4C-lite：结构化记忆落库**不在这里做**。
    #
    # 它原先内联在此处（asyncio.to_thread + try/except），使 user-visible
    # completion 多等一次 LLM 抽取 + embedding + Chroma upsert（实测 11–20s）。
    # 现在由 runner 在任务置 completed / 发布 task.completed 之后调用
    # `store_report_memory`，并纳入 post-completion 后台任务生命周期。
    return {
        "final_report": report_content,
        "writer_validation": citation_validation,
        "messages": ["最终的报告: " + report_content],
    }


# ===== BUILD GRAPH =====

async def build_agent_async(with_hitl: bool = True, checkpoint_db: str | None = None) -> Any:
    """异步构建 Deep Research Agent（支持 astream_events）。

    流式场景使用 InMemorySaver（已验证兼容 astream_events），
    持久化由 build_agent() 同步版的 SqliteSaver 负责。

    Args:
        with_hitl: 是否启用 HITL 中断审查
        checkpoint_db: 忽略（保留参数兼容性），流式场景统一用 InMemorySaver

    Returns:
        编译后的 LangGraph agent（带 InMemorySaver）
    """
    builder = _create_builder(with_hitl)
    checkpointer = InMemorySaver()
    logger.info("Agent built with InMemorySaver for streaming")
    return builder.compile(checkpointer=checkpointer)


def build_agent(with_hitl: bool = True, checkpoint_db: str | None = None) -> Any:
    """同步构建 Agent（用于 get_graph 或非流式 ainvoke）。

    流式场景请使用 build_agent_async。
    """
    builder = _create_builder(with_hitl)

    checkpointer = None
    if checkpoint_db:
        os.makedirs(os.path.dirname(checkpoint_db) or ".", exist_ok=True)
        conn = sqlite3.connect(checkpoint_db, check_same_thread=False)
        checkpointer = SqliteSaver(conn)
        logger.info("Agent built with SqliteSaver at %s", checkpoint_db)
    else:
        logger.info("Agent built without persistent checkpointer")

    return builder.compile(checkpointer=checkpointer)


def _create_builder(
    with_hitl: bool = True,
    speculative: bool | None = None,
    quality_gate_version: int | None = None,
) -> StateGraph:
    """Create the sole supported parallel V3 graph.

    Legacy versions 0/1/2 are deliberately rejected, rather than silently
    resumed using a different topology. Old pending checkpoints need draining
    or archiving by an older deployment before this release.
    """
    from functools import partial

    if speculative is False:
        raise ValueError("serial graph removed; V3 requires parallel research")
    if quality_gate_version is not None and (
        type(quality_gate_version) is not int or quality_gate_version != QUALITY_GATE_VERSION
    ):
        raise ValueError("only Quality Gate V3 is supported; old checkpoints cannot resume")
    builder = StateGraph(AgentState, input_schema=AgentInputState)
    builder.add_node("write_research_brief", write_research_brief)
    builder.add_node("write_draft_report", write_draft_report)
    builder.add_node("supervisor_subgraph", supervisor_agent)
    builder.add_node("precheck_quality_gate", partial(precheck_quality_gate, with_hitl=with_hitl))
    builder.add_node("evaluate_research", evaluate_research)
    builder.add_node("red_team_audit", red_team_review)
    builder.add_node("route_quality_gate_v3", partial(route_quality_gate_v3, with_hitl=with_hitl))
    builder.add_node("targeted_research", targeted_research)
    builder.add_node("claim_verification", claim_verification)
    builder.add_node("final_report_generation", final_report_generation)
    builder.add_edge(START, "write_research_brief")
    builder.add_edge("write_research_brief", "write_draft_report")
    builder.add_edge("write_research_brief", "supervisor_subgraph")
    builder.add_edge(["write_draft_report", "supervisor_subgraph"], "precheck_quality_gate")
    builder.add_edge("evaluate_research", "route_quality_gate_v3")
    builder.add_edge("red_team_audit", "route_quality_gate_v3")
    builder.add_edge("targeted_research", "precheck_quality_gate")
    if with_hitl:
        builder.add_node("human_review", human_review)
    else:
        builder.add_node("quality_gate_failed", quality_gate_failed)
    builder.add_edge("claim_verification", "final_report_generation")
    builder.add_edge("final_report_generation", END)
    return builder


# ===== MODULE-LEVEL INSTANCES =====

# 流式兼容的模块级 agent（InMemorySaver，支持 astream_events）
_streaming_agent = _create_builder(with_hitl=True).compile(checkpointer=InMemorySaver())
agent = _streaming_agent

# 流式场景使用 agent_async（同一套 InMemorySaver 逻辑）
# 用法: agent = await build_agent_async(with_hitl=True)

# 兼容旧引用
deep_researcher_builder = _create_builder(with_hitl=True)
