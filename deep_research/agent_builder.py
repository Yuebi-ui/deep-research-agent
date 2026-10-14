"""Build the LangGraph research workflow and its task-facing nodes.

The graph owns research planning, HITL, verification and final report generation.
Long-term memory is accessed through ``deep_research.memory.runtime`` so the
memory subsystem does not depend back on this graph builder.
"""

import asyncio
import os
import sqlite3
import time
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from deep_research.utils import get_today_str
from deep_research.states import AgentState, AgentInputState
from deep_research.prompts import RESEARCH_BRIEF_PROMPT
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
from deep_research.agents.red_team_agent import review_research_bundle
from deep_research.quality_gate import (
    QUALITY_GATE_VERSION,
    checked_candidate_notes,
    format_candidate_notes,
    quality_gate_lineage,
    auto_research_lineage,
    quality_gate_version_for_new_job,
    initial_repair_limits,
    choose_research_repair,
    merge_candidate_notes,
    format_followup_request,
)
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

    prompt = RESEARCH_BRIEF_PROMPT.format(
        messages=messages,
        date=get_today_str()
    )

    # 注入记忆上下文
    if memory_context:
        prompt = memory_context + "\n\n" + prompt

    response = await draft_model.ainvoke([HumanMessage(content=prompt)])
    research_brief = response.content

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
        "quality_gate_version": quality_gate_version_for_new_job(),
        **initial_repair_limits(),
        "quality_repair_count": 0,
        "manual_research_count": 0,
        "quality_repair_history": [],
    }


# ===== HUMAN REVIEW (HITL) =====

async def human_review(state: AgentState) -> Command[str]:
    """HITL 节点：展示报告草稿，等待人工审查。

    用户可以通过 resume 传入 {'action': 'approve'} 或 {'action': 'revise', 'feedback': '...'}

    All supported lineages run research in parallel before HITL, with candidate
    findings quarantined until approval. V2 also supports draft revisions and
    explicit targeted research without re-running unrelated research.
    """
    draft = state.get("draft_report", "")
    research_brief = state.get("research_brief", "")
    quality_v1 = quality_gate_lineage(state)
    quality_v2 = auto_research_lineage(state)
    research_notes: list[str] = checked_candidate_notes(state) if quality_v1 else []
    if quality_v1:
        # Fail closed if a late/stale result overwrote the reviewed candidate.
        # This is a preview ONLY; it does not admit evidence to ``notes``.
        assessment = state.get("quality_review") or {}
        red_review = state.get("red_team_review") or {}
        preview = [note[:500] for note in research_notes[:3]]
    else:
        assessment, red_review, preview = {}, {}, []

    if quality_v2:
        manual_used = state.get("manual_research_count", 0)
        manual_limit = state.get("manual_research_limit", 2)
        options = ["approve", "revise_draft"]
        if manual_used < manual_limit:
            options.append("more_research")
    else:
        options = ["approve", "revise"]

    # 触发中断，等待人工输入
    review = interrupt({
        "message": "研究成果已完成自动审核，请审查" if quality_v1 else "报告草稿已生成，请审查",
        "draft_report_preview": draft[:1000] + ("..." if len(draft) > 1000 else ""),
        "research_brief": research_brief[:500],
        "options": options,
        "gate_decision": state.get("quality_gate_decision") if quality_v2 else None,
        "quality_repair_history": (state.get("quality_repair_history") or []) if quality_v2 else [],
        "auto_research_rounds": state.get("quality_repair_count", 0) if quality_v2 else None,
        "manual_research_rounds": state.get("manual_research_count", 0) if quality_v2 else None,
        "manual_research_limit": state.get("manual_research_limit", 0) if quality_v2 else None,
        "quality_review": assessment,
        "red_team_review": red_review,
        "research_notes_count": len(research_notes) if quality_v1 else None,
        "research_notes_preview": preview,
        # 观测字段（不参与决策）：审查对象属于哪一代研究
        "research_generation": active_generation(state),
    })

    action = review if isinstance(review, str) else (review.get("action") if isinstance(review, dict) else None)
    if action not in (("approve", "revise", "revise_draft", "more_research") if quality_v2
                      else ("approve", "revise")):
        raise ValueError(f"Unsupported HITL review action: {action!r}")

    if action == "more_research":
        feedback = str(review.get("feedback", "")).strip() if isinstance(review, dict) else ""
        if not feedback or len(feedback) > 500:
            raise ValueError("more_research requires feedback (1-500 characters)")
        if state.get("manual_research_count", 0) >= state.get("manual_research_limit", 2):
            # Stale clients can submit a button they saw before another round
            # exhausted the budget. Never fail the entire task for this.
            return Command(goto="human_review", update={
                "quality_gate_decision": {
                    "action": "human_review", "reason": "manual_budget_exhausted",
                    "generation": active_generation(state),
                },
            })
        return Command(goto="targeted_research", update={
            "repair_request": {"source": "human", "from_generation": active_generation(state),
                               "topics": [feedback]},
        })

    if action in ("revise", "revise_draft"):
        feedback = review.get("feedback", "") if isinstance(review, dict) else ""
        revision_prompt = f"请根据以下反馈修改报告草稿：\n{feedback}\n\n当前草稿：\n{draft}"
        response = await writer_model.ainvoke([HumanMessage(content=revision_prompt)])
        if quality_v2:
            # Text/structure edit does not invalidate independently collected
            # sources. Re-evaluate the same quarantined generation and return
            # to HITL; no paid Researcher calls for mere copy edits.
            return Command(goto="evaluate_research", update={"draft_report": response.content})
        # Legacy V0/V1 parallel checkpoints retain the regenerate-on-revision path.
        logger.info("HITL: revised generation %d; regenerating research", active_generation(state))
        return Command(goto="regenerate_research", update={"draft_report": response.content})
    else:
        # The only successful path admits generation-fenced findings into notes.
        # There is no serial approve -> Supervisor branch anymore.
        if quality_v1:
            checked_candidate_notes(state)
        updates = fenced_accept_update(state)
        logger.info(
            "HITL: accepted parallel research generation %d (%d notes admitted)",
            active_generation(state), len(updates["notes"]),
        )
        return Command(goto="claim_verification", update=updates)


# ===== REGENERATE RESEARCH (Phase 5B, speculative only) =====

async def regenerate_research(state: AgentState, config: RunnableConfig) -> dict:
    """REJECT/REGENERATE 回路：推进 generation，用干净输入重跑研究（隔离输出）。

    在节点内 inline 调用 supervisor 子图（它没有 checkpointer）：

    - 输入是**显式构造的干净 state**：新一代研究不继承上一代任何研究上下文
      （messages / iterations / quality_history / critiques 全部归零）；
    - 输出只回写 `research_generation` / `speculative_research`（以及研究途中
      refine_draft_report 改写过的草稿）；子图内部状态**不会**
      写回父图；
    - config 只继承观测侧（callbacks/metadata/tags），剥离 configurable ——
      子图不得触碰父图的 thread/checkpoint 配置；
    - 节点整体重放安全：seed 是 brief 的确定性投影，崩溃后重跑同一节点不会
      产生业务污染（只浪费一次研究成本）。

    spike 实测（scripts/experiments/phase5b_topology_spike.py）：节点内
    inline ainvoke 每次调用状态干净、内层节点事件出现在外层事件流（UI/观测保留）。
    """
    gen = active_generation(state) + 1
    brief = state.get("research_brief", "") or ""
    draft = state.get("draft_report", "") or ""
    seed = build_research_seed(brief)
    metrics = seed_metrics(seed)
    child_input = {
        "supervisor_messages": [render_seed_message(seed), brief],
        "research_brief": brief,
        "research_iterations": 0,
        "needs_quality_repair": False,
        "final_exit": False,
        "notes": [],
        "raw_notes": [],
        "quality_history": [],
        "active_critiques": [],
        "research_trace": [],
        "supervisor_memory_hint": "",
        # Preserve the revised draft for legacy V0/V1 parallel resume paths.
        # The inner refine_draft_report can still
        # 可以读取/改写它（实测触发率极低，但保持该路径可达）。
        "draft_report": draft,
        "research_generation": gen,
        "quality_gate_version": state.get("quality_gate_version", 0),
    }
    child_config: dict[str, Any] = {k: v for k, v in (config or {}).items() if k != "configurable"}
    child_config.setdefault("recursion_limit", 50)

    started = time.monotonic()
    result = await supervisor_agent.ainvoke(child_input, config=child_config)
    elapsed = time.monotonic() - started

    payload = result.get("speculative_research")
    if not payload or payload.get("generation") != gen:
        # 子图在投机 lineage 必须写出带 generation 的 payload；缺失 = wiring 被破坏。
        raise ResearchFenceError(
            f"regenerate_research: supervisor 未产出 generation {gen} 的 payload "
            f"(got {payload!r})"
        )

    updates: dict[str, Any] = {
        "research_generation": gen,
        "speculative_research": payload,
        "quality_review": None,  # clear assessments from previous generation
        "red_team_review": None,
    }
    if result.get("research_trace"):
        updates["research_trace"] = result["research_trace"]
    refined = result.get("draft_report") or ""
    if refined and refined != draft:
        # 研究途中 refine 改写过草稿：与串行语义一致，回写供后续审查/终稿使用。
        updates["draft_report"] = refined
        logger.info("regenerate_research: draft refined during research (gen %d)", gen)

    logger.info(
        "research regenerated: generation %d invalidated → generation %d produced "
        "(wall=%.1fs, notes=%d, seed=%s)",
        gen - 1, gen, elapsed, len(payload.get("notes") or []), metrics["seed_fingerprint"],
    )
    return updates


# ===== ACCEPT RESEARCH (Phase 5B, speculative without HITL) =====

async def accept_research(state: AgentState) -> dict:
    """无 HITL 构建下的 auto-accept：fence 校验后把投机结果并入 `notes`。

    生产路径始终带 HITL（runner `with_hitl=True`）；本节点只服务离线/测试
    构建（with_hitl=False + 投机）。语义与 human_review 的 ACCEPT 分支一致：
    只有 generation 匹配的结果可以进入下游，否则显式失败。
    """
    if quality_gate_lineage(state):
        checked_candidate_notes(state)
    updates = fenced_accept_update(state)
    logger.info(
        "auto-accept: research generation %d admitted downstream (%d notes)",
        active_generation(state), len(updates["notes"]),
    )
    return updates


# ===== PHASE 1: INDEPENDENT QUALITY GATE (speculative lineage only) =====

async def evaluate_research(state: AgentState) -> dict:
    """Read fenced candidate findings, grade readiness, never rewrite Draft.

    Low scores are shown to HITL; auto-research based on them is Phase 2.
    A judge outage is visible as an error rather than silently marked PASS.
    """
    notes = checked_candidate_notes(state)  # fence failure must propagate
    generation = active_generation(state)
    try:
        evaluation = await evaluate_research_readiness(
            research_brief=(state.get("research_brief", "") or "")[:12000],
            draft_report=(state.get("draft_report", "") or "")[:16000],
            research_notes=format_candidate_notes(notes),
        )
        scores = (
            evaluation.comprehensiveness_score,
            evaluation.accuracy_score,
            evaluation.coherence_score,
        )
        import math

        if any(not math.isfinite(v) or not (0 <= v <= 10) for v in scores):
            raise ValueError(f"invalid evaluator scores: {scores!r}")
        assessment = {
            "status": "completed",
            "generation": generation,
            "notes_count": len(notes),
            "comprehensiveness_score": scores[0],
            "accuracy_score": scores[1],
            "coherence_score": scores[2],
            "overall_score": round(sum(scores) / len(scores), 2),
            "reason": evaluation.reason,
            "missing_aspects": evaluation.missing_aspects,
            "need_more_research": evaluation.need_more_research,
        }
    except Exception as exc:  # judge outage must remain visible to human reviewer
        logger.warning("Quality Gate V1 evaluator unavailable: %s", exc)
        assessment = {
            "status": "error", "generation": generation,
            "notes_count": len(notes), "error": str(exc)[:240],
            "need_more_research": None,
        }
    return {"quality_review": assessment}


async def red_team_review(state: AgentState) -> dict:
    """Adversarial evidence review before HITL; no hidden auto-repair in V1."""
    notes = checked_candidate_notes(state)
    generation = active_generation(state)
    try:
        feedback = await review_research_bundle(
            research_brief=(state.get("research_brief", "") or "")[:12000],
            draft_report=(state.get("draft_report", "") or "")[:16000],
            research_notes=format_candidate_notes(notes),
        )
        if auto_research_lineage(state) and not feedback.strip():
            raise ValueError("Red Team returned an empty verdict")
        passed = feedback.strip().upper() == "PASS"
        review = {
            "status": "completed", "generation": generation,
            "notes_count": len(notes), "passed": passed,
            "feedback": "PASS" if passed else (feedback[:2400] or "Red Team 未返回明确结论"),
        }
    except Exception as exc:
        logger.warning("Quality Gate V1 red team unavailable: %s", exc)
        review = {
            "status": "error", "generation": generation,
            "notes_count": len(notes), "passed": None, "error": str(exc)[:240],
        }
    return {"red_team_review": review}


# ===== PHASE 2: BOUNDED RESEARCH FEEDBACK LOOP (V2 lineage only) =====

async def route_quality_gate(state: AgentState, *, with_hitl: bool = True) -> Command[str]:
    """Decide whether additional evidence is worth another paid research round.

    No static outgoing edges: Command owns exactly one route. The decision and
    budgets are checkpointed; the judge cannot silently change cost limits.
    """
    checked_candidate_notes(state)
    outcome = choose_research_repair(state)
    if outcome["action"] == "targeted_research":
        return Command(goto="targeted_research", update={
            "quality_gate_decision": outcome,
            "repair_request": {
                "source": "auto", "from_generation": active_generation(state),
                "topics": outcome["topics"],
            },
        })
    if not with_hitl and outcome["reason"] != "no_gap":
        # Model errors, exhausted budgets and unresolved evidence gaps require
        # a HUMAN decision; no-HITL mode must not silently approve them.
        return Command(goto="quality_gate_failed", update={"quality_gate_decision": outcome})
    return Command(
        goto="human_review" if with_hitl else "accept_research",
        update={"quality_gate_decision": outcome, "repair_request": None},
    )


async def targeted_research(state: AgentState, config: RunnableConfig) -> dict:
    """Run focused Supervisor+Researcher on missing evidence, not a full rewrite.

    The old candidate is read through the generation/brief fence, the child
    runs in fresh isolated state, and only *after successful completion* do we
    atomically checkpoint a NEW generation containing old+new notes. ``notes``
    is never written here; only HITL approval can commit it downstream.
    """
    if not auto_research_lineage(state):
        raise ResearchFenceError("targeted research requires a V2 checkpoint")
    previous = checked_candidate_notes(state)
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
        "needs_quality_repair": False, "final_exit": False,
        "notes": [], "raw_notes": [], "quality_history": [],
        "active_critiques": [], "research_trace": [], "supervisor_memory_hint": "",
        "research_generation": next_gen, "quality_gate_version": 2,
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
    history = list(state.get("quality_repair_history") or [])
    history.append({
        "source": source, "generation": next_gen, "requested_topics": list(topics),
        "before_notes_count": len(previous), "new_notes_count": added,
        "after_notes_count": len(merged), "round": count + 1,
    })
    update = {
        "research_generation": next_gen,
        "speculative_research": build_speculative_payload(
            generation=next_gen, research_brief=brief, notes=merged,
        ),
        count_key: count + 1,
        "quality_repair_history": history,
        "quality_review": None, "red_team_review": None,
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
        "V2 research quality requires human review: " + str(decision.get("reason", "unknown"))
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
    quality_gate: bool | None = None,
    quality_gate_version: int | None = None,
) -> StateGraph:
    """Create a parallel-only graph with versioned quality-review routes.

    ``speculative`` remains as a backwards-compatible test/caller parameter.
    False is explicitly refused; it must never silently restore a serial route.
    Existing parallel V0/V1 checkpoints can still resume through their versioned
    graph. Existing serial checkpoints cannot resume and must be drained on V2.
    """
    if speculative is False:
        raise ValueError("serial graph was removed; use a parallel V2 checkpoint")
    if quality_gate_version is None:
        version = (quality_gate_version_for_new_job() if quality_gate is None
                   else (QUALITY_GATE_VERSION if quality_gate else 0))
    else:
        version = quality_gate_version
    if type(version) is not int or version not in (0, 1, 2):
        raise ValueError(f"unsupported quality gate version: {version!r}")

    builder = StateGraph(AgentState, input_schema=AgentInputState)
    builder.add_node("write_research_brief", write_research_brief)
    builder.add_node("write_draft_report", write_draft_report)
    builder.add_node("supervisor_subgraph", supervisor_agent)
    builder.add_node("claim_verification", claim_verification)
    builder.add_node("final_report_generation", final_report_generation)
    builder.add_edge(START, "write_research_brief")

    # Direct parallel fan-out; an intermediary seed node would break overlap.
    builder.add_edge("write_research_brief", "write_draft_report")
    builder.add_edge("write_research_brief", "supervisor_subgraph")

    if version >= 1:
        builder.add_node("evaluate_research", evaluate_research)
        builder.add_node("red_team_audit", red_team_review)
        # This list-form edge is an AND-join, not separate OR triggers.
        builder.add_edge(["write_draft_report", "supervisor_subgraph"], "evaluate_research")
        builder.add_edge("evaluate_research", "red_team_audit")
        if version == 2:
            from functools import partial

            builder.add_node("route_quality_gate", partial(route_quality_gate, with_hitl=with_hitl))
            builder.add_node("targeted_research", targeted_research)
            builder.add_edge("red_team_audit", "route_quality_gate")
            builder.add_edge("targeted_research", "evaluate_research")
            if with_hitl:
                builder.add_node("human_review", human_review)
            else:
                builder.add_node("accept_research", accept_research)
                builder.add_node("quality_gate_failed", quality_gate_failed)
                builder.add_edge("accept_research", "claim_verification")
        elif with_hitl:
            # Parallel V1 checkpoints still expect this review/regenerate topology.
            builder.add_node("human_review", human_review)
            builder.add_node("regenerate_research", regenerate_research)
            builder.add_edge("red_team_audit", "human_review")
            builder.add_edge("regenerate_research", "evaluate_research")
        else:
            builder.add_node("accept_research", accept_research)
            builder.add_edge("red_team_audit", "accept_research")
            builder.add_edge("accept_research", "claim_verification")
    elif with_hitl:
        # Parallel V0 checkpoints are retained, but serial V0 is unsupported.
        builder.add_node("human_review", human_review)
        builder.add_node("regenerate_research", regenerate_research)
        builder.add_edge(["write_draft_report", "supervisor_subgraph"], "human_review")
        builder.add_edge("regenerate_research", "human_review")
    else:
        builder.add_node("accept_research", accept_research)
        builder.add_edge(["write_draft_report", "supervisor_subgraph"], "accept_research")
        builder.add_edge("accept_research", "claim_verification")

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
